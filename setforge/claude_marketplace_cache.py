"""Marketplace clone/cache/discovery helpers for Claude plugin sources.

Owns the on-disk path resolution from a :class:`MarketplaceSource`
to a usable ``cache_dir`` — clone-on-demand, refresh-by-source-shape,
collision-handling. All git invocations honor the locked subprocess
hygiene (list argv, ``check=True``, ``text=True``,
``capture_output=True``, explicit ``timeout=``). Cache directory
layout is rooted at :data:`MARKETPLACE_CACHE_ROOT`; each marketplace
mirrors into ``MARKETPLACE_CACHE_ROOT / <repo-basename>``.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Final

import platformdirs

from setforge import marketplace_cache_wizard
from setforge.binaries import stderr_of
from setforge.config import (
    ClaudeInstallMode,
    Config,
    MarketplaceSource,
    MarketplaceSourceKind,
    ResolvedProfile,
)
from setforge.errors import ConfigError, MarketplaceCacheMiss
from setforge.marketplace_cache_wizard import CollisionAction

LOGGER: logging.Logger = logging.getLogger(__name__)

_TIMEOUT_S = 30
_CLONE_TIMEOUT_S = 120

#: Default root for ``LOCAL_CLONE`` marketplace mirrors. Each marketplace
#: clones into ``MARKETPLACE_CACHE_ROOT / <marketplace-name>``. Tests
#: monkeypatch this module attribute to redirect into ``tmp_path``.
MARKETPLACE_CACHE_ROOT: Final[Path] = (
    Path(platformdirs.user_cache_dir("setforge")) / "marketplaces"
)

__all__ = [
    "MARKETPLACE_CACHE_ROOT",
    "resolve_marketplace_source",
    "sync_marketplace_cache",
]


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
    (e.g. ``FakeGit``) keep working without extra wiring.
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
        # by resolve_marketplace_source for GITHUB sources
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

    Used by :func:`resolve_marketplace_source` to detect a config-side
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


def resolve_marketplace_source(
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
    on-disk cache for the GitHub source (basename of ``source.repo``),
    lazily cloning if the cache is absent. PATH sources passthrough
    regardless of mode. ``mp_name`` and ``auto`` thread through to the
    cache-collision path — see :func:`_resolve_existing_cache` for the
    URL-drift dispatch and the ``auto=True`` non-interactive contract.
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
        return _resolve_existing_cache(
            source, cache_dir, root, mp_name=mp_name, auto=auto
        )
    _clone_marketplace(source, cache_dir)
    return MarketplaceSource(source=MarketplaceSourceKind.PATH, path=cache_dir)


def _resolve_existing_cache(
    source: MarketplaceSource,
    cache_dir: Path,
    cache_root: Path,
    *,
    mp_name: str | None,
    auto: bool,
) -> MarketplaceSource:
    """Return the PATH source for a cache hit, dispatching URL drift.

    Probes the cache's ``origin`` remote; when it drifted from
    ``source.repo`` (modulo :func:`_urls_equivalent` normalization)
    the collision wizard decides via :func:`_resolve_cache_collision`.
    A failed probe (``None``) or a matching origin reuses ``cache_dir``
    as-is. ``mp_name`` (the YAML-side marketplace key) is threaded only
    for the wizard's prompt text; when ``None`` we fall back to
    ``source.repo`` for the prompt label — callers in reconcile / sync
    paths supply it explicitly. ``auto=True`` (e.g. from a ``--auto``
    CLI flag) suppresses the interactive wizard and raises
    :class:`MarketplaceCacheMiss` instead, per
    :mod:`setforge.marketplace_cache_wizard`'s spec-locked safe
    default.
    """
    # narrows MarketplaceSource.repo (str | None) for mypy; upstream-guarded
    # by resolve_marketplace_source for GITHUB sources
    repo = source.repo or ""
    current = _cache_origin_url(cache_dir)
    if current is not None and current != repo and not _urls_equivalent(current, repo):
        return _resolve_cache_collision(
            source=source,
            cache_dir=cache_dir,
            cache_root=cache_root,
            existing_origin=current,
            mp_name=mp_name or repo,
            auto=auto,
        )
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

    Pulled out of :func:`resolve_marketplace_source` so the swap-site
    keeps its single-screen shape. The wizard returns a closed-set
    :class:`CollisionAction`; each action maps to its own
    ``_collision_*`` helper below. ``ABORT`` raises
    :class:`typer.Abort` inside the wizard and never reaches here.
    The wizard call stays module-qualified so tests can monkeypatch
    ``setforge.marketplace_cache_wizard.resolve_collision``.
    """
    resolution = marketplace_cache_wizard.resolve_collision(
        mp_name=mp_name,
        cache_dir=cache_dir,
        cache_root=cache_root,
        existing_origin=existing_origin,
        # narrows MarketplaceSource.repo (str | None) for mypy; upstream-guarded
        # by resolve_marketplace_source for GITHUB sources
        new_repo=source.repo or "",
        auto=auto,
    )
    if resolution.action is CollisionAction.KEEP:
        return _collision_keep(source, cache_dir, mp_name)
    if resolution.action is CollisionAction.UPDATE:
        return _collision_update(source, cache_dir)
    return _collision_both(source, cache_dir, resolution.new_cache_dir)


def _collision_keep(
    source: MarketplaceSource, cache_dir: Path, mp_name: str
) -> MarketplaceSource:
    """``KEEP``: return the existing cache_dir as-is.

    Emits a clear info log noting the new ``source.repo`` was NOT
    applied.
    """
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


def _collision_update(source: MarketplaceSource, cache_dir: Path) -> MarketplaceSource:
    """``UPDATE``: rmtree the existing cache and re-clone.

    Today's pre-wizard behavior, now opt-in.
    """
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


def _collision_both(
    source: MarketplaceSource, cache_dir: Path, new_dir: Path | None
) -> MarketplaceSource:
    """``BOTH``: clone into the wizard-supplied ``new_dir``.

    Leaves the existing ``cache_dir`` untouched.
    """
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
    cannot enumerate.
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
