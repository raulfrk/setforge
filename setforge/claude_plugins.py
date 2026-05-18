"""Claude plugin & marketplace reconcile, driven by the ``claude`` CLI.

All subprocess invocations honor the locked hygiene rules: the ``claude``
binary is resolved via :func:`setforge.binaries.resolve_binary` (which
walks CLI flag → env var → host-local config → PATH), raising
:class:`PluginToolMissing` if every layer comes up empty.
``subprocess.run`` always uses ``check=True, text=True,
capture_output=True, timeout=30``, and args are always a list with no
``shell=True``.

Implements a three-way reconcile per spec Δ2: plugins can be ``enabled``,
``disabled``, or ``absent``.  The reconcile computes:

- ``to_install`` — declared but absent (genuinely missing).
- ``to_enable``  — declared but disabled (cheap re-activation).
- ``to_disable`` — enabled but not declared (PRUNE only).

Marketplaces are always-on: declared marketplaces that are not installed
trigger ``marketplace_add``; stale marketplaces are never auto-evicted.
"""

from __future__ import annotations

import functools
import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import platformdirs

# ruamel.yaml ships py.typed without resolvable annotations; no stub pkg on PyPI.
from ruamel.yaml import YAML  # type: ignore[import-not-found]
from ruamel.yaml.comments import (  # type: ignore[import-not-found]
    CommentedMap,
    CommentedSeq,
)

from setforge.binaries import load_host_local_config, resolve_binary, stderr_of
from setforge.config import (
    ClaudeInstallMode,
    Config,
    MarketplaceSource,
    MarketplaceSourceKind,
    ReconcilePolicy,
    ResolvedProfile,
    load_config,
)
from setforge.errors import (
    ConfigError,
    MarketplaceCacheMiss,
    PluginToolMissing,
    ProfileNotFound,
)

LOGGER: logging.Logger = logging.getLogger(__name__)

_CLAUDE_BIN_NAME = "claude"
_TIMEOUT_S = 30
_CLONE_TIMEOUT_S = 120

#: Default root for ``LOCAL_CLONE`` marketplace mirrors. Each marketplace
#: clones into ``MARKETPLACE_CACHE_ROOT / <marketplace-name>``. Tests
#: monkeypatch this module attribute to redirect into ``tmp_path``.
MARKETPLACE_CACHE_ROOT: Final[Path] = (
    Path(platformdirs.user_cache_dir("setforge")) / "marketplaces"
)


@functools.lru_cache(maxsize=1)
def _get_claude_bin() -> Path:
    """Resolve the ``claude`` binary via :func:`resolve_binary` or raise.

    The result is cached for the process lifetime via
    :func:`functools.lru_cache`. Tests that change the resolved path
    between cases must call ``_get_claude_bin.cache_clear()``.
    Raises :class:`PluginToolMissing` when the resolved path is ``None``
    (binary not found at any layer). Non-executable paths surface as
    :class:`BinaryOverrideInvalid` propagated from :func:`resolve_binary`.
    """
    path = resolve_binary(_CLAUDE_BIN_NAME)
    if path is None:
        raise PluginToolMissing(
            "claude binary not found; install Claude CLI or set "
            "--claude-bin / SETFORGE_CLAUDE_BIN / local.yaml"
        )
    return path


# ---------------------------------------------------------------------------
# Report dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """Summary of what reconcile did (or would do for REPORT / dry_run).

    ``to_install`` is a list of ``(plugin_name, marketplace_name)`` tuples
    for plugins that are genuinely absent.  ``to_enable`` and ``to_disable``
    are lists of plugin IDs in ``name@marketplace`` form.
    ``marketplaces_added`` lists marketplace names that were (or would be)
    added.  ``dry_run`` is ``True`` whenever no write commands were run.
    """

    to_install: list[tuple[str, str]]
    to_enable: list[str]
    to_disable: list[str]
    marketplaces_added: list[str]
    dry_run: bool
    failed: list[tuple[str, str]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(
            self.to_install
            or self.to_enable
            or self.to_disable
            or self.marketplaces_added
        )


# ---------------------------------------------------------------------------
# Low-level subprocess wrappers
# ---------------------------------------------------------------------------


def list_marketplaces() -> dict[str, dict]:
    """Return installed marketplaces as ``{name: entry_dict}``.

    Calls ``claude plugin marketplace list --json`` and parses the JSON
    array.  Each element must have a ``name`` key; the whole element is
    kept as the value so callers can inspect ``source``, ``repo``, etc.
    """
    claude = str(_get_claude_bin())
    try:
        result = subprocess.run(
            [claude, "plugin", "marketplace", "list", "--json"],
            check=True,
            text=True,
            capture_output=True,
            timeout=_TIMEOUT_S,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise PluginToolMissing(
            f"`claude plugin marketplace list` failed: {stderr_of(exc)}"
        ) from exc
    entries: list[dict] = json.loads(result.stdout)
    return {e["name"]: e for e in entries if "name" in e}


def list_installed() -> dict[str, dict]:
    """Return installed plugins as ``{id: entry_dict}`` where ``id`` is
    ``"<name>@<marketplace>"``.

    Calls ``claude plugin list --json`` and parses the JSON array.
    The ``enabled`` field (``bool``) is preserved on each entry.
    """
    claude = str(_get_claude_bin())
    try:
        result = subprocess.run(
            [claude, "plugin", "list", "--json"],
            check=True,
            text=True,
            capture_output=True,
            timeout=_TIMEOUT_S,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise PluginToolMissing(
            f"`claude plugin list` failed: {stderr_of(exc)}"
        ) from exc
    entries: list[dict] = json.loads(result.stdout)
    return {e["id"]: e for e in entries if "id" in e}


def marketplace_add(name: str, source: MarketplaceSource) -> None:
    """Register a marketplace via ``claude plugin marketplace add <source>``.

    The source argument is the repo path (``owner/repo``) for GitHub
    sources, or the absolute file-system path for local sources.
    """
    claude = str(_get_claude_bin())
    if source.source is MarketplaceSourceKind.GITHUB:
        # narrows MarketplaceSource.repo (str | None) for mypy; upstream-guarded
        # by _resolve_marketplace_source for GITHUB sources
        source_arg = source.repo or ""
    else:
        source_arg = str(source.path or "")
    subprocess.run(
        [claude, "plugin", "marketplace", "add", source_arg],
        check=True,
        text=True,
        capture_output=True,
        timeout=_TIMEOUT_S,
    )


def marketplace_remove(name: str) -> None:
    """Remove a marketplace via ``claude plugin marketplace remove <name>``."""
    claude = str(_get_claude_bin())
    subprocess.run(
        [claude, "plugin", "marketplace", "remove", name],
        check=True,
        text=True,
        capture_output=True,
        timeout=_TIMEOUT_S,
    )


def marketplace_update(name: str) -> None:
    """Update a marketplace via ``claude plugin marketplace update <name>``."""
    claude = str(_get_claude_bin())
    subprocess.run(
        [claude, "plugin", "marketplace", "update", name],
        check=True,
        text=True,
        capture_output=True,
        timeout=_TIMEOUT_S,
    )


def plugin_install(name: str, marketplace: str) -> None:
    """Install a plugin via ``claude plugin install <name>@<marketplace> --scope=user``.

    Always passes ``--scope=user`` per spec § Locked decisions row 8.
    """
    claude = str(_get_claude_bin())
    subprocess.run(
        [claude, "plugin", "install", f"{name}@{marketplace}", "--scope=user"],
        check=True,
        text=True,
        capture_output=True,
        timeout=_TIMEOUT_S,
    )


def plugin_uninstall(plugin_id: str) -> None:
    """Uninstall a plugin via ``claude plugin uninstall <id>``.

    ``plugin_id`` should be in ``"<name>@<marketplace>"`` form. Used by
    :func:`setforge.cli.revert` as the inverse of :func:`plugin_install`
    when reversing a transition's ``PluginDelta.installed`` list.
    """
    claude = str(_get_claude_bin())
    subprocess.run(
        [claude, "plugin", "uninstall", plugin_id],
        check=True,
        text=True,
        capture_output=True,
        timeout=_TIMEOUT_S,
    )


def plugin_enable(plugin_id: str) -> None:
    """Re-activate a disabled plugin via ``claude plugin enable <id>``.

    This is a cheap re-activation — no re-download happens.  ``plugin_id``
    should be in ``"<name>@<marketplace>"`` form.
    """
    claude = str(_get_claude_bin())
    subprocess.run(
        [claude, "plugin", "enable", plugin_id],
        check=True,
        text=True,
        capture_output=True,
        timeout=_TIMEOUT_S,
    )


def plugin_disable(plugin_id: str) -> None:
    """Disable a plugin via ``claude plugin disable <id>``.

    ``plugin_id`` should be in ``"<name>@<marketplace>"`` form.
    """
    claude = str(_get_claude_bin())
    subprocess.run(
        [claude, "plugin", "disable", plugin_id],
        check=True,
        text=True,
        capture_output=True,
        timeout=_TIMEOUT_S,
    )


# ---------------------------------------------------------------------------
# Local-clone install mode — marketplace cache helpers
# ---------------------------------------------------------------------------


def _resolve_git_or_raise() -> Path:
    """Return the resolved ``git`` binary path or raise :class:`MarketplaceCacheMiss`.

    Centralizes the git-on-PATH check and its remediation message.
    Both :func:`_clone_marketplace` and :func:`_refresh_marketplace_cache`
    rely on it; consolidating prevents message drift between call sites.
    Returns a :class:`Path` for callers that want to pass it to
    :func:`subprocess.run` (callers can ``str()`` if they need a string).
    """
    git = shutil.which("git")
    if git is None:
        raise MarketplaceCacheMiss(
            "'git' not on PATH; install git or set "
            "claude.install_mode: regular in "
            "~/.config/setforge/local.yaml"
        )
    return Path(git)


def _debug_git_output(prefix: str, result: subprocess.CompletedProcess[str]) -> None:
    """Emit DEBUG logs for a git invocation's success-path stdout/stderr doublet.

    Skips empty streams. ``prefix`` is the caller-formatted invocation
    label; the stream name and payload pass as ``%s`` args per LOGGER
    lazy-formatting convention.
    """
    if result.stdout:
        LOGGER.debug("%s stdout: %s", prefix, result.stdout)
    if result.stderr:
        LOGGER.debug("%s stderr: %s", prefix, result.stderr)


def _run_git(
    *args: str,
    cwd: Path | None = None,
    timeout: int = _CLONE_TIMEOUT_S,
) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` with the project's locked subprocess hygiene.

    ``check=True, text=True, capture_output=True``. ``cwd`` is passed
    via ``git -C <cwd>`` rather than the subprocess ``cwd=`` kwarg so
    monkeypatched ``subprocess.run`` fakes that key off argv shape
    (e.g. :class:`FakeGit`) keep working without extra wiring.
    Maps :class:`subprocess.CalledProcessError` and
    :class:`subprocess.TimeoutExpired` to a generic
    :class:`MarketplaceCacheMiss` carrying ``stderr_of(exc)``; callers
    that want a more specific remediation message should catch and
    re-raise with their own context. Raises before any subprocess
    call if ``git`` is not on PATH (via :func:`_resolve_git_or_raise`).
    """
    git = _resolve_git_or_raise()
    argv: list[str] = [str(git)]
    if cwd is not None:
        argv.extend(["-C", str(cwd)])
    argv.extend(args)
    try:
        result = subprocess.run(
            argv,
            check=True,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        LOGGER.debug("git %s stderr: %s", args, stderr_of(exc))
        raise MarketplaceCacheMiss(
            f"`git {' '.join(args)}` failed: {stderr_of(exc)}"
        ) from exc
    _debug_git_output(f"git {args}", result)
    return result


def _safe_cache_dir(cache_root: Path, subdir_name: str) -> Path:
    """Return ``cache_root / subdir_name`` after rejecting path-traversal.

    ``subdir_name`` is derived from YAML-controlled
    ``MarketplaceSource.repo`` via ``rsplit("/", 1)[-1]`` at the
    call site. A repo string of shape ``"owner/repo/.."`` yields
    basename ``".."``, which would escape ``cache_root``; a value
    containing path separators or empty would similarly evade the
    intended layout. Reject both shapes at the source and double-check
    the resolved path stays inside ``cache_root``. Raises
    :class:`MarketplaceCacheMiss` with a remediation message keyed to
    the security concern, so the caller's existing
    ``MarketplaceCacheMiss`` handler surfaces it consistently with
    every other cache-side failure mode.
    """
    if subdir_name in ("", ".", "..") or "/" in subdir_name or "\\" in subdir_name:
        raise MarketplaceCacheMiss(
            f"invalid marketplace cache subdir name {subdir_name!r}; "
            f"repo identifier must not be empty or contain path separators"
        )
    cache_dir = cache_root / subdir_name
    if cache_dir.resolve().parent != cache_root.resolve():
        raise MarketplaceCacheMiss(
            f"cache_dir {cache_dir!r} escapes cache_root {cache_root!r}"
        )
    return cache_dir


def _clone_marketplace(source: MarketplaceSource, dest_path: Path) -> None:
    """Clone ``source.repo`` into ``dest_path`` via ``git clone``.

    Network is required. Resolves ``git`` via :func:`_resolve_git_or_raise`
    (raises :class:`MarketplaceCacheMiss` when missing). Argv carries the
    ``--`` separator before ``source.repo`` per CRITICAL-2 (flag-injection
    defense). On non-zero / timeout ``git clone`` exit, raises
    :class:`MarketplaceCacheMiss` with the spec-locked remediation
    message ("...likely offline; run sync-cache while online first").
    """
    git = _resolve_git_or_raise()
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # `--` separates options from positional args. Defends against
        # argv flag injection if source.repo ever starts with `-`
        # (e.g. `-upload-pack=...`), which git would otherwise interpret
        # as a flag rather than as the repo positional. CLAUDE.md
        # subprocess hygiene already mandates list-form argv and no
        # shell=True; this is the defense-in-depth completion of that.
        # narrows MarketplaceSource.repo (str | None) for mypy; upstream-guarded
        # by _resolve_marketplace_source for GITHUB sources
        result = subprocess.run(
            [str(git), "clone", "--", source.repo or "", str(dest_path)],
            check=True,
            text=True,
            capture_output=True,
            timeout=_CLONE_TIMEOUT_S,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        LOGGER.debug("git clone %r stderr: %s", source.repo, stderr_of(exc))
        raise MarketplaceCacheMiss(
            f"marketplace {source.repo!r} not in local cache and `git clone` "
            f"failed (likely offline): {stderr_of(exc)}. "
            f"Run `setforge plugin sync-cache --profile=<name>` while online "
            f"first."
        ) from exc
    _debug_git_output(f"git clone {source.repo!r}", result)


def _refresh_marketplace_cache(source: MarketplaceSource, cache_dir: Path) -> None:
    """Refresh an existing marketplace cache via ``git fetch`` + hard reset.

    Preconditions: ``cache_dir`` already exists and contains a git repo
    whose ``origin`` matches ``source.repo``. The caller is responsible
    for the URL-mismatch detect-and-reclone path; this helper assumes
    the remote is correct and unconditionally fetches + resets to
    ``origin/HEAD``. Hard reset (not ``git pull``) is intentional: the
    cache must never carry local merges.

    Raises :class:`MarketplaceCacheMiss` on git failure (network down,
    cache corrupted) with the same remediation as :func:`_clone_marketplace`.
    """
    try:
        _run_git("fetch", "origin", cwd=cache_dir, timeout=_CLONE_TIMEOUT_S)
        _run_git("reset", "--hard", "origin/HEAD", cwd=cache_dir, timeout=_TIMEOUT_S)
    except MarketplaceCacheMiss as exc:
        raise MarketplaceCacheMiss(
            f"marketplace {source.repo!r}: refresh failed ({exc}). "
            f"Delete {cache_dir} and re-run sync-cache while online."
        ) from exc


def _cache_origin_url(cache_dir: Path) -> str | None:
    """Return the cache's ``origin`` remote URL, or ``None`` on git failure.

    Used by :func:`_resolve_marketplace_source` to detect a config-side
    repo URL change after the cache was first created. A best-effort
    probe — any git failure (no remote, dirty checkout, missing git
    binary) yields ``None`` so the caller can fall through to a
    re-clone instead of raising.

    Silent-on-failure for callers (returns ``None`` instead of
    raising), but emits the captured stdout/stderr at ``DEBUG`` level
    for ``setforge -v`` tracing. Per the m81/cqf convention: every
    git invocation in this module logs its output at DEBUG level,
    even silent-probe paths.
    """
    git = shutil.which("git")
    if git is None:
        return None
    try:
        result = subprocess.run(
            [git, "-C", str(cache_dir), "remote", "get-url", "origin"],
            check=True,
            text=True,
            capture_output=True,
            timeout=_TIMEOUT_S,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        LOGGER.debug(
            "git remote get-url origin (cache %s) stderr: %s",
            cache_dir,
            stderr_of(exc),
        )
        return None
    _debug_git_output(f"git remote get-url origin (cache {cache_dir})", result)
    return result.stdout.strip()


def _resolve_marketplace_source(
    source: MarketplaceSource,
    mode: ClaudeInstallMode,
    *,
    cache_root: Path | None = None,
    mp_name: str | None = None,
    auto: bool = False,
) -> MarketplaceSource:
    """Return the :class:`MarketplaceSource` that ``marketplace_add`` will see.

    Pure transform (modulo the network-touching :func:`_clone_marketplace`
    fallback): under :data:`ClaudeInstallMode.REGULAR`, returns
    ``source`` unchanged. Under :data:`ClaudeInstallMode.LOCAL_CLONE`,
    returns a PATH-kind :class:`MarketplaceSource` pointing at the
    on-disk cache for the GitHub source, lazily cloning if the cache
    is absent. PATH sources passthrough regardless of mode.

    The cache directory derives its basename from ``source.repo``;
    ``mp_name`` (the YAML-side marketplace key) is threaded only
    for the cache-collision wizard's prompt text. When ``mp_name`` is
    ``None`` we fall back to ``source.repo`` for the prompt label —
    callers in reconcile / sync paths supply it explicitly.

    ``auto=True`` (e.g. from a ``--auto`` CLI flag) suppresses the
    interactive cache-collision wizard and raises
    :class:`MarketplaceCacheMiss` instead, per
    :mod:`setforge.marketplace_cache_wizard`'s spec-locked safe
    default. Cache collision arises only on URL drift; the
    happy-path (cache hit with matching origin, cache miss) is
    unaffected.
    """
    root = cache_root if cache_root is not None else MARKETPLACE_CACHE_ROOT
    if mode is ClaudeInstallMode.REGULAR:
        return source
    if source.source is MarketplaceSourceKind.PATH:
        return source
    if not source.repo:
        raise MarketplaceCacheMiss(
            "GITHUB marketplace source missing 'repo' field; cannot resolve "
            "local-clone path"
        )
    cache_dir = _safe_cache_dir(root, source.repo.rsplit("/", 1)[-1])
    if cache_dir.exists():
        current = _cache_origin_url(cache_dir)
        if (
            current is not None
            and current != source.repo
            and not _urls_equivalent(current, source.repo)
        ):
            return _resolve_cache_collision(
                source=source,
                cache_dir=cache_dir,
                cache_root=root,
                existing_origin=current,
                mp_name=mp_name or source.repo,
                auto=auto,
            )
    else:
        _clone_marketplace(source, cache_dir)
    return MarketplaceSource(
        source=MarketplaceSourceKind.PATH,
        path=cache_dir,
    )


def _resolve_cache_collision(
    *,
    source: MarketplaceSource,
    cache_dir: Path,
    cache_root: Path,
    existing_origin: str,
    mp_name: str,
    auto: bool,
) -> MarketplaceSource:
    """Dispatch a URL-drift collision to the wizard and apply the choice.

    Pulled out of :func:`_resolve_marketplace_source` so the swap-site
    keeps its single-screen shape. The wizard returns a closed-set
    :class:`CollisionAction`; this function maps each action to its
    side effects:

    - ``KEEP`` — return the existing cache_dir as-is and emit a clear
      info log noting the new ``source.repo`` was NOT applied.
    - ``UPDATE`` — rmtree the existing cache and re-clone (today's
      pre-wizard behavior, now opt-in).
    - ``BOTH`` — clone the new source into the wizard-supplied
      ``new_cache_dir``; leave the existing cache_dir untouched.
    - ``ABORT`` — the wizard raises :class:`typer.Abort` directly;
      never reached here.
    """
    from setforge.marketplace_cache_wizard import (
        CollisionAction,
        resolve_collision,
    )

    resolution = resolve_collision(
        mp_name=mp_name,
        cache_dir=cache_dir,
        cache_root=cache_root,
        existing_origin=existing_origin,
        # narrows MarketplaceSource.repo (str | None) for mypy; upstream-guarded
        # by _resolve_marketplace_source for GITHUB sources
        new_repo=source.repo or "",
        auto=auto,
    )
    if resolution.action is CollisionAction.KEEP:
        LOGGER.info(
            "cache-collision: using existing cache %r for marketplace %r; "
            "new source.repo %r NOT applied",
            cache_dir,
            mp_name,
            source.repo,
        )
        return MarketplaceSource(
            source=MarketplaceSourceKind.PATH,
            path=cache_dir,
        )
    if resolution.action is CollisionAction.UPDATE:
        LOGGER.info(
            "cache-collision: re-cloning %r over existing %r",
            source.repo,
            cache_dir,
        )
        shutil.rmtree(cache_dir)
        _clone_marketplace(source, cache_dir)
        return MarketplaceSource(
            source=MarketplaceSourceKind.PATH,
            path=cache_dir,
        )
    # action is BOTH
    new_dir = resolution.new_cache_dir
    assert new_dir is not None, "wizard contract: BOTH carries new_cache_dir"
    LOGGER.info(
        "cache-collision: cloning %r into new cache %r (existing %r kept)",
        source.repo,
        new_dir,
        cache_dir,
    )
    _clone_marketplace(source, new_dir)
    return MarketplaceSource(
        source=MarketplaceSourceKind.PATH,
        path=new_dir,
    )


def _urls_equivalent(observed: str, declared: str) -> bool:
    """Compare a git remote URL to a declared ``owner/repo`` ref.

    ``declared`` is the ``MarketplaceSource.repo`` value — typically the
    short ``owner/repo`` form Claude's marketplace accepts. ``observed``
    is whatever the cache's ``git remote get-url origin`` reported,
    which can be the full HTTPS URL git rewrote ``owner/repo`` into
    (e.g. ``https://github.com/owner/repo.git``). The comparison
    normalizes both to ``owner/repo`` form before checking equality so a
    cache cloned via the shorthand isn't treated as URL-changed every
    time.

    Scope is intentionally github-only (YAGNI): every
    :class:`MarketplaceSourceKind` the project currently ships resolves
    to a github.com URL, so the hardcoded prefix list below covers the
    full clone-rewrite surface in practice. When non-github
    ``MarketplaceSourceKind`` support lands, the preferred fix is to
    record the canonical URL into a sidecar file at clone time and
    reduce this helper to a single byte-equal comparison — that path
    also picks up SSH variants and arbitrary hosts the prefix list
    cannot enumerate. Follow-up issue: ``setforge-msc``.
    """

    def _normalize(url: str) -> str:
        stripped = url.removesuffix(".git").rstrip("/")
        for prefix in (
            "https://github.com/",
            "git@github.com:",
            "ssh://git@github.com/",
        ):
            if stripped.startswith(prefix):
                return stripped[len(prefix) :]
        return stripped

    return _normalize(observed) == _normalize(declared)


def sync_marketplace_cache(
    cfg: Config,
    profile: ResolvedProfile,
    *,
    cache_root: Path | None = None,
) -> list[str]:
    """Refresh every GitHub marketplace declared by ``profile``.

    For each declared marketplace whose source kind is GITHUB: clone
    if absent, otherwise fetch + reset to ``origin/HEAD``. Returns the
    list of marketplace *names* (YAML-side keys) that were refreshed
    (or freshly cloned). PATH sources are skipped silently.

    Profile-scoped (not config-scoped) because the spec mandates
    ``--profile=<name>`` to match setforge CLI convention; declared
    marketplaces in non-active profiles are left alone. Raises
    :class:`MarketplaceCacheMiss` on git failure for any marketplace.
    """
    root = cache_root if cache_root is not None else MARKETPLACE_CACHE_ROOT
    refreshed: list[str] = []
    # The marketplaces referenced by a profile are those needed by its
    # claude_plugins entries. Resolve plugin names -> marketplace names
    # via the top-level claude_plugins registry, mirroring reconcile's
    # logic.
    referenced: set[str] = set()
    for bare_name in profile.claude_plugins:
        ref = cfg.claude_plugins.get(bare_name)
        if ref is None:
            raise ConfigError(
                f"profile references undeclared plugin: {bare_name!r} "
                f"(add it to top-level claude_plugins:)"
            )
        referenced.add(ref.marketplace)
    for mp_name in sorted(referenced):
        source = cfg.marketplaces.get(mp_name)
        if source is None:
            raise ConfigError(f"plugin references undeclared marketplace: {mp_name!r}")
        if source.source is MarketplaceSourceKind.PATH:
            LOGGER.info("sync-cache: %s is a PATH source, skipping", mp_name)
            continue
        if not source.repo:
            raise ConfigError(f"marketplace {mp_name!r}: GITHUB source missing 'repo'")
        cache_dir = _safe_cache_dir(root, source.repo.rsplit("/", 1)[-1])
        if cache_dir.exists():
            LOGGER.info("sync-cache: refreshing %s at %s", mp_name, cache_dir)
            _refresh_marketplace_cache(source, cache_dir)
        else:
            LOGGER.info("sync-cache: cloning %s into %s", mp_name, cache_dir)
            _clone_marketplace(source, cache_dir)
        refreshed.append(mp_name)
    return refreshed


# ---------------------------------------------------------------------------
# Reconcile algorithm — three-way per spec Δ2
# ---------------------------------------------------------------------------


def _split_id(pid: str) -> tuple[str, str]:
    """Split ``"name@marketplace"`` into ``(name, marketplace)``."""
    name, mp = pid.split("@", 1)
    return name, mp


def _add_declared_marketplaces(
    cfg: Config,
    mps_to_add: list[str],
    install_mode: ClaudeInstallMode,
    cache_root: Path,
    failed: list[tuple[str, str]],
    *,
    auto: bool = False,
) -> None:
    """Run install-mode dispatch + ``marketplace_add`` for each name in ``mps_to_add``.

    Side-effects ``failed`` in place on every per-marketplace failure
    (cache miss or ``claude``-side subprocess failure). Extracted from
    :func:`reconcile` so the host-local install-mode swap site is
    isolated from the larger plugin-state reconcile loop. Pure
    w.r.t. anything outside ``failed`` — the caller still owns the
    surrounding state machine.

    ``auto`` propagates to :func:`_resolve_marketplace_source` and
    governs the cache-collision wizard. Default is interactive (the
    wizard fires on URL drift); pass ``auto=True`` from a non-
    interactive CLI path to refuse silent auto-resolution.
    """
    for mp_name in mps_to_add:
        LOGGER.info("adding marketplace: %s", mp_name)
        try:
            effective_source = _resolve_marketplace_source(
                cfg.marketplaces[mp_name],
                install_mode,
                cache_root=cache_root,
                mp_name=mp_name,
                auto=auto,
            )
            marketplace_add(mp_name, effective_source)
        except MarketplaceCacheMiss as exc:
            LOGGER.warning("marketplace_add failed for %s: %s", mp_name, exc)
            failed.append((mp_name, str(exc)))
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            msg = stderr_of(exc)
            LOGGER.warning("marketplace_add failed for %s: %s", mp_name, msg)
            failed.append((mp_name, msg))


def reconcile(
    cfg: Config,
    profile: ResolvedProfile,
    *,
    dry_run: bool = False,
) -> ReconcileReport:
    """Three-way reconcile per spec § Δ2.

    States:
    - ``to_install`` = declared - (enabled union disabled)   # genuinely absent
    - ``to_enable``  = declared intersect disabled                # present but off
    - ``to_disable`` = enabled - declared  (PRUNE only)

    Marketplaces (always-on, regardless of policy): each declared
    marketplace not in ``list_marketplaces()`` gets ``marketplace_add``
    called (except under ``REPORT`` policy or ``dry_run=True``, where it
    is listed but not executed).  Stale marketplaces are never evicted.

    ``dry_run=True`` logs intended actions and returns without running any
    write subprocess. ``REPORT`` policy behaves identically to
    ``dry_run=True`` for write suppression.

    Bare profile names (e.g. ``"superpowers"``) are resolved to
    ``"<name>@<marketplace>"`` form via the top-level
    :attr:`Config.claude_plugins` registry before any subprocess work.
    A name not present in the registry raises :class:`ConfigError`.
    """
    declared: set[str] = set()
    for bare_name in profile.claude_plugins:
        ref = cfg.claude_plugins.get(bare_name)
        if ref is None:
            raise ConfigError(
                f"profile references undeclared plugin: {bare_name!r} "
                f"(add it to top-level claude_plugins:)"
            )
        declared.add(f"{bare_name}@{ref.marketplace}")

    installed = list_installed()
    enabled = {pid for pid, p in installed.items() if p.get("enabled", True)}
    disabled = {pid for pid, p in installed.items() if not p.get("enabled", True)}

    to_install = sorted(declared - (enabled | disabled))
    to_enable = sorted(declared & disabled)
    # Compute to_disable for PRUNE and REPORT (both need the diff);
    # only ADDITIVE suppresses the diff entirely.
    if profile.plugins_reconcile is not ReconcilePolicy.ADDITIVE:
        to_disable = sorted(enabled - declared)
    else:
        to_disable = []

    # Marketplaces: always-on regardless of policy
    have_mps = set(list_marketplaces())
    declared_mps = set(cfg.marketplaces)
    mps_to_add = sorted(declared_mps - have_mps)

    is_read_only = dry_run or profile.plugins_reconcile is ReconcilePolicy.REPORT

    if is_read_only:
        LOGGER.info(
            "reconcile (read-only): to_install=%s to_enable=%s to_disable=%s "
            "marketplaces_to_add=%s",
            to_install,
            to_enable,
            to_disable,
            mps_to_add,
        )
        return ReconcileReport(
            to_install=[_split_id(pid) for pid in to_install],
            to_enable=to_enable,
            to_disable=to_disable,
            marketplaces_added=mps_to_add,
            dry_run=True,
        )

    failed: list[tuple[str, str]] = []

    # Host-local install mode dispatch: under LOCAL_CLONE, swap each
    # GitHub-backed MarketplaceSource for a PATH source pointing at the
    # on-disk cache (cloning on first encounter). Under REGULAR, the
    # transform is a no-op and today's behavior is unchanged.
    install_mode = load_host_local_config().claude.install_mode
    _add_declared_marketplaces(
        cfg, mps_to_add, install_mode, MARKETPLACE_CACHE_ROOT, failed
    )

    _reconcile_install(to_install, to_enable, failed)
    if profile.plugins_reconcile is ReconcilePolicy.PRUNE:
        _reconcile_remove(to_disable, failed)

    return ReconcileReport(
        to_install=[_split_id(pid) for pid in to_install],
        to_enable=to_enable,
        to_disable=to_disable,
        marketplaces_added=mps_to_add,
        dry_run=False,
        failed=failed,
    )


def _reconcile_install(
    to_install: list[str],
    to_enable: list[str],
    failed: list[tuple[str, str]],
) -> None:
    """Run the install + enable loop, appending subprocess failures to ``failed``.

    Per spec § Algorithm β2 (setforge-l37): freshly-installed plugins
    land disabled in ``installed_plugins.json`` — ``claude plugin install``
    never touches ``enabledPlugins``. To make a single reconcile run land
    each plugin active, successful installs are routed through the enable
    loop via a separate working list, leaving the report's ``to_enable``
    field semantically clean (the original ``declared intersect disabled``
    set, NOT freshly-installed plugins). Failed installs are NOT enabled.
    """
    runtime_to_enable: list[str] = list(to_enable)
    for pid in to_install:
        name, mp = _split_id(pid)
        LOGGER.info("installing plugin: %s @ %s", name, mp)
        try:
            plugin_install(name, mp)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            msg = stderr_of(exc)
            LOGGER.warning("plugin_install failed for %s: %s", pid, msg)
            failed.append((pid, msg))
        else:
            runtime_to_enable.append(pid)

    for pid in runtime_to_enable:
        LOGGER.info("enabling plugin: %s", pid)
        try:
            plugin_enable(pid)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            msg = stderr_of(exc)
            LOGGER.warning("plugin_enable failed for %s: %s", pid, msg)
            failed.append((pid, msg))


def _reconcile_remove(
    to_disable: list[str],
    failed: list[tuple[str, str]],
) -> None:
    """Run the disable loop for ``PRUNE`` policy, recording failures."""
    for pid in to_disable:
        LOGGER.info("disabling plugin: %s", pid)
        try:
            plugin_disable(pid)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            msg = stderr_of(exc)
            LOGGER.warning("plugin_disable failed for %s: %s", pid, msg)
            failed.append((pid, msg))


# ---------------------------------------------------------------------------
# YAML editing helpers — preserve comments via ruamel.yaml round-trip
# ---------------------------------------------------------------------------


def _load_yaml_doc(config_path: Path):
    """Load ``config_path`` in ruamel.yaml round-trip mode.

    Returns ``(yaml_instance, doc)`` so the caller can modify ``doc``
    and write it back via ``yaml_instance.dump(doc, fh)``.
    Raises :class:`ConfigError` when the file does not exist.
    """
    if not config_path.exists():
        raise ConfigError(f"config file not found: {config_path}")
    yaml = YAML(typ="rt")
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.preserve_quotes = True
    with config_path.open("r", encoding="utf-8") as fh:
        return yaml, yaml.load(fh)


def _ensure_top_level_block(doc: CommentedMap, key: str) -> CommentedMap:
    """Return ``doc[key]``, creating an empty mapping if absent."""
    if key not in doc:
        doc[key] = CommentedMap()
    return doc[key]


def _ensure_list(block: CommentedMap, key: str) -> CommentedSeq:
    """Return ``block[key]`` as a sequence, creating it if absent."""
    if key not in block:
        block[key] = CommentedSeq()
    return block[key]


def yaml_add_marketplace(
    config_path: Path,
    name: str,
    source: MarketplaceSource,
) -> bool:
    """Append a marketplace entry to the top-level ``marketplaces:`` block.

    Idempotent: returns ``False`` if ``name`` is already present.
    Comments and key order in the YAML document are preserved via
    ruamel.yaml round-trip mode.
    """
    cfg = load_config(config_path)
    if name in cfg.marketplaces:
        return False

    yaml, doc = _load_yaml_doc(config_path)
    mps = _ensure_top_level_block(doc, "marketplaces")
    entry = CommentedMap()
    entry["source"] = source.source.value
    if source.source is MarketplaceSourceKind.GITHUB:
        entry["repo"] = source.repo or ""
    else:
        entry["path"] = str(source.path or "")
    mps[name] = entry
    with config_path.open("w", encoding="utf-8") as fh:
        yaml.dump(doc, fh)
    return True


def yaml_remove_marketplace(config_path: Path, name: str) -> bool:
    """Remove a marketplace from the top-level ``marketplaces:`` block.

    Idempotent: returns ``False`` if ``name`` is not present.
    """
    cfg = load_config(config_path)
    if name not in cfg.marketplaces:
        return False

    yaml, doc = _load_yaml_doc(config_path)
    mps = doc.get("marketplaces")
    if mps and name in mps:
        del mps[name]
    with config_path.open("w", encoding="utf-8") as fh:
        yaml.dump(doc, fh)
    return True


def yaml_add_plugin(
    config_path: Path,
    plugin_name: str,
    marketplace: str,
) -> bool:
    """Declare a plugin in the top-level ``claude_plugins:`` block.

    Idempotent: returns ``False`` if ``plugin_name`` is already present.
    Does NOT add it to any profile's ``claude_plugins:`` list — the CLI
    caller is responsible for that via :func:`yaml_add_plugin_to_profile`.
    """
    cfg = load_config(config_path)
    if plugin_name in cfg.claude_plugins:
        return False

    yaml, doc = _load_yaml_doc(config_path)
    plugins_block = _ensure_top_level_block(doc, "claude_plugins")
    entry = CommentedMap()
    entry["marketplace"] = marketplace
    plugins_block[plugin_name] = entry
    with config_path.open("w", encoding="utf-8") as fh:
        yaml.dump(doc, fh)
    return True


def yaml_add_plugin_to_profile(
    config_path: Path,
    profile_name: str,
    plugin_ref: str,
) -> bool:
    """Append ``plugin_ref`` to ``profiles.<profile>.claude_plugins``.

    Idempotent: returns ``False`` if already present.
    Raises :class:`ProfileNotFound` when the profile does not exist.
    """
    cfg = load_config(config_path)
    if profile_name not in cfg.profiles:
        raise ProfileNotFound(f"profile not found: {profile_name}")
    if plugin_ref in cfg.profiles[profile_name].claude_plugins:
        return False

    yaml, doc = _load_yaml_doc(config_path)
    profiles = doc.get("profiles", {})
    if profile_name not in profiles:
        raise ProfileNotFound(f"profile not found: {profile_name}")
    profile_block = profiles[profile_name]
    cp_list = _ensure_list(profile_block, "claude_plugins")
    cp_list.append(plugin_ref)
    with config_path.open("w", encoding="utf-8") as fh:
        yaml.dump(doc, fh)
    return True


def yaml_remove_plugin_from_profile(
    config_path: Path,
    profile_name: str,
    plugin_ref: str,
) -> bool:
    """Remove ``plugin_ref`` from ``profiles.<profile>.claude_plugins``.

    Idempotent: returns ``False`` if not present.
    Raises :class:`ProfileNotFound` when the profile does not exist.
    """
    cfg = load_config(config_path)
    if profile_name not in cfg.profiles:
        raise ProfileNotFound(f"profile not found: {profile_name}")
    if plugin_ref not in cfg.profiles[profile_name].claude_plugins:
        return False

    yaml, doc = _load_yaml_doc(config_path)
    profiles = doc.get("profiles", {})
    if profile_name not in profiles:
        return False
    profile_block = profiles[profile_name]
    cp_list = profile_block.get("claude_plugins", [])
    if plugin_ref in cp_list:
        cp_list.remove(plugin_ref)
    with config_path.open("w", encoding="utf-8") as fh:
        yaml.dump(doc, fh)
    return True
