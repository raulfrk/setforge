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

import json
import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

import platformdirs

from setforge import atomicio, marketplace_cache_wizard, reconcile_adapter
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

#: A plugin pin is a 40-hex git commit, never a moving ref; validated fail-closed.
_SHA_RE: Final = re.compile(r"^[0-9a-f]{40}$")

#: Default root for ``LOCAL_CLONE`` marketplace mirrors. Each marketplace
#: clones into ``MARKETPLACE_CACHE_ROOT / <repo-basename>`` (the segment
#: after the final ``/`` of ``MarketplaceSource.repo``). Tests monkeypatch
#: this module attribute to redirect into ``tmp_path``.
MARKETPLACE_CACHE_ROOT: Final[Path] = (
    Path(platformdirs.user_cache_dir("setforge")) / "marketplaces"
)

#: Sidecar filename (under the cache root) recording ``owner/repo -> cache
#: subdir`` aliases. Written by the ``BOTH`` collision outcome when a repo
#: is cloned into a non-basename subdir (e.g. ``plug-v2``); consulted by the
#: declared-identity computation so the next reconcile resolves the
#: marketplace to that subdir instead of recomputing ``cache_root/<basename>``.
#: Keyed by the full ``owner/repo`` slug because the basename collides by
#: definition. Absent/legacy sidecar degrades to plain basename behavior.
MARKETPLACE_ALIAS_SIDECAR: Final[str] = ".aliases.json"

__all__ = [
    "MARKETPLACE_ALIAS_SIDECAR",
    "MARKETPLACE_CACHE_ROOT",
    "MarketplaceSourcePlan",
    "apply_marketplace_source_plan",
    "checkout_marketplace_at",
    "plan_marketplace_source",
    "read_cache_aliases",
    "resolve_marketplace_source",
    "sync_marketplace_cache",
    "validate_marketplace_source_plan",
]


class MarketplaceSourceAction(StrEnum):
    """Closed set of mutations selected while resolving a marketplace source."""

    NONE = "none"
    CLONE = "clone"
    UPDATE = "update"
    BOTH = "both"


@dataclass(frozen=True, slots=True)
class MarketplaceSourcePlan:
    """Read-only marketplace source decision consumed by install apply."""

    source_json: str
    effective_source_json: str
    action: MarketplaceSourceAction
    cache_root: Path
    cache_dir: Path | None = None
    expected_cache_exists: bool = False
    expected_origin: str | None = None
    both_dir: Path | None = None
    expected_both_exists: bool = False

    @property
    def source(self) -> MarketplaceSource:
        """Return a detached copy of the declared source snapshot."""
        return MarketplaceSource.model_validate_json(self.source_json)

    @property
    def effective_source(self) -> MarketplaceSource:
        """Return a detached copy of the selected effective source."""
        return MarketplaceSource.model_validate_json(self.effective_source_json)


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
    Maps :class:`subprocess.CalledProcessError`,
    :class:`subprocess.TimeoutExpired`, and :class:`OSError` (exec
    failure — git resolved on PATH but could not be launched) to a
    generic :class:`MarketplaceCacheMiss` carrying ``stderr_of(exc)``;
    callers
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
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        OSError,
    ) as exc:
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


def read_cache_aliases(cache_root: Path) -> dict[str, str]:
    """Return the ``owner/repo -> cache subdir name`` alias map for ``cache_root``.

    Reads the :data:`MARKETPLACE_ALIAS_SIDECAR` JSON file. A missing
    sidecar (the common / legacy case) returns an empty map, so callers
    degrade to plain ``cache_root/<basename>`` resolution. A malformed or
    unreadable sidecar is logged and treated as empty rather than raising —
    a corrupt alias file must not wedge every reconcile; the worst case is
    one re-fired collision wizard, the same as no sidecar at all. Only
    ``str -> str`` entries are kept (defensive against hand-edited files),
    and the value is further validated as a safe single-segment subdir name
    (same predicate as :func:`_safe_cache_dir`) — a corrupt or hand-edited
    sidecar must not feed an unchecked path segment into the cache-dir
    computation; offending entries are dropped with a warning.
    """
    sidecar = cache_root / MARKETPLACE_ALIAS_SIDECAR
    try:
        raw = json.loads(sidecar.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning(
            "ignoring unreadable marketplace alias sidecar %s: %s", sidecar, exc
        )
        return {}
    if not isinstance(raw, dict):
        LOGGER.warning(
            "ignoring malformed marketplace alias sidecar %s (not an object)",
            sidecar,
        )
        return {}
    aliases: dict[str, str] = {}
    for k, v in raw.items():
        if not (isinstance(k, str) and isinstance(v, str)):
            continue
        if v in ("", ".", "..") or "/" in v or "\\" in v:
            LOGGER.warning(
                "ignoring unsafe marketplace alias subdir %r for %r in %s",
                v,
                k,
                sidecar,
            )
            continue
        aliases[k] = v
    return aliases


def _record_cache_alias(cache_root: Path, repo: str, cache_dir: Path) -> None:
    """Persist ``repo -> cache_dir.name`` into the cache-root alias sidecar.

    Read-modify-write of the JSON map: each individual *write* is torn-free
    (:func:`setforge.atomicio.atomic_write_text` swaps the whole file into
    place). SetForge CLI writers hold :func:`setforge.locking.install_resources_lock`
    across this read-modify-write and related cache mutations. Direct helper
    callers must provide equivalent serialization. The subdir *name* (not the
    absolute path) is stored so
    the sidecar stays portable if the cache root moves; the declared-identity
    computation rejoins it onto its own ``cache_root``.
    """
    aliases = read_cache_aliases(cache_root)
    aliases[repo] = cache_dir.name
    atomicio.atomic_write_text(
        cache_root / MARKETPLACE_ALIAS_SIDECAR,
        json.dumps(aliases, indent=2, sort_keys=True) + "\n",
    )


class _GitFailureKind(StrEnum):
    """Category of a failed ``git clone`` / ``git fetch`` for remediation.

    Replaces the historical blanket "likely offline" label, which misreported
    an online-but-unfound repo (renamed / moved / private) as a connectivity
    problem. Classified from git's stderr; ``UNKNOWN`` is the honest fallback
    for stderr matching no known signature.
    """

    OFFLINE = "offline"
    REPO_NOT_FOUND = "repo-not-found"
    AUTH = "auth"
    UNKNOWN = "unknown"


#: stderr substrings (lowercased) identifying each failure category. The check
#: ORDER in :func:`_classify_git_failure` is load-bearing: an explicit auth
#: signature must win over the broad "not found" match, because GitHub returns
#: "Repository not found" for private repos you cannot authenticate to.
_OFFLINE_SIGNATURES: Final[tuple[str, ...]] = (
    "could not resolve host",
    "could not connect",
    "couldn't connect",
    "failed to connect",
    "connection timed out",
    "connection refused",
    "network is unreachable",
    "temporary failure in name resolution",
)
_AUTH_SIGNATURES: Final[tuple[str, ...]] = (
    "authentication failed",
    "could not read username",
    "could not read password",
    "permission denied",
    "403 forbidden",
    "invalid username or password",
)
_NOT_FOUND_SIGNATURES: Final[tuple[str, ...]] = (
    "repository not found",
    "not found",
    "does not appear to be a git repository",
    "does not exist",
)


def _classify_git_failure(stderr: str) -> _GitFailureKind:
    """Map a git clone/fetch ``stderr`` to a :class:`_GitFailureKind`.

    Offline first (a DNS/connection signature is unambiguous), then auth (an
    explicit auth message must beat the broad not-found match), then
    not-found, else ``UNKNOWN``.
    """
    s = stderr.lower()
    if any(sig in s for sig in _OFFLINE_SIGNATURES):
        return _GitFailureKind.OFFLINE
    if any(sig in s for sig in _AUTH_SIGNATURES):
        return _GitFailureKind.AUTH
    if any(sig in s for sig in _NOT_FOUND_SIGNATURES):
        return _GitFailureKind.REPO_NOT_FOUND
    return _GitFailureKind.UNKNOWN


def _github_clone_url(repo: str) -> str:
    """Expand a bare ``owner/repo`` GitHub shorthand to a clonable HTTPS URL.

    ``MarketplaceSource.repo`` for a GITHUB source is typically the short
    ``owner/repo`` form that ``claude`` / ``gh`` accept — but raw ``git
    clone`` does NOT understand it (it treats ``owner/repo`` as a local path
    and fails with "does not appear to be a git repository" even when online).
    Passing the shorthand verbatim to ``git clone`` was the root cause of the
    "fails while online" bug.

    Idempotent: an already-qualified value (an explicit ``scheme://``, an
    ``scp``-style ``git@host:owner/repo``, or a filesystem path) is returned
    unchanged, so full URLs, SSH remotes, and local-bare-repo test fixtures
    keep working. Only a bare ``owner/repo`` gets the ``https://github.com/``
    prefix. Detection assumes a github ``owner/repo`` or a fully-qualified
    remote — a user-less scp shorthand (``host:owner/repo``) is NOT recognised,
    but a GITHUB-kind ``MarketplaceSource.repo`` is ``owner/repo`` by contract,
    so that shape never reaches here.
    """
    if (
        "://" in repo
        or repo.startswith("git@")
        or repo.startswith(("/", "./", "../", "~"))
    ):
        return repo
    return f"https://github.com/{repo}"


def _marketplace_clone_failure_message(repo: str | None, stderr: str) -> str:
    """Build a category-aware remediation for a failed marketplace clone."""
    base = f"marketplace {repo!r} not in local cache and `git clone` failed"
    match _classify_git_failure(stderr):
        case _GitFailureKind.OFFLINE:
            return (
                f"{base} — network unreachable ({stderr}). Check your "
                f"connection, then run `setforge plugin sync-cache "
                f"--profile=<name>` while online."
            )
        case _GitFailureKind.REPO_NOT_FOUND:
            return (
                f"{base} — repository not found ({stderr}). The marketplace "
                f"repo may have been renamed, moved, or made private; verify "
                f"its URL in setforge.yaml, then re-run `setforge plugin "
                f"sync-cache --profile=<name>`."
            )
        case _GitFailureKind.AUTH:
            return (
                f"{base} — authentication failed ({stderr}). The repository "
                f"may be private; configure git credentials for this host, "
                f"then re-run `setforge plugin sync-cache --profile=<name>`."
            )
        case _GitFailureKind.UNKNOWN:
            return (
                f"{base}: {stderr}. Check the marketplace repo URL in "
                f"setforge.yaml and your network connection."
            )


def _marketplace_refresh_failure_message(
    repo: str | None, detail: str, cache_dir: Path
) -> str:
    """Build a category-aware remediation for a failed marketplace refresh."""
    base = f"marketplace {repo!r}: refresh failed"
    match _classify_git_failure(detail):
        case _GitFailureKind.OFFLINE:
            return (
                f"{base} — network unreachable ({detail}). Re-run `setforge "
                f"plugin sync-cache --profile=<name>` while online."
            )
        case _GitFailureKind.REPO_NOT_FOUND:
            return (
                f"{base} — repository not found ({detail}). The repo may have "
                f"been renamed, moved, or made private; verify its URL in "
                f"setforge.yaml, then delete {cache_dir} and re-run sync-cache."
            )
        case _GitFailureKind.AUTH:
            return (
                f"{base} — authentication failed ({detail}). The repository "
                f"may be private; configure git credentials, then re-run "
                f"sync-cache."
            )
        case _GitFailureKind.UNKNOWN:
            return (
                f"{base} ({detail}). Delete {cache_dir} and re-run `setforge "
                f"plugin sync-cache --profile=<name>` while online."
            )


def _clone_marketplace(source: MarketplaceSource, dest_path: Path) -> None:
    """Clone ``source.repo`` into ``dest_path`` via ``git clone``.

    Network is required. Resolves ``git`` via :func:`_resolve_git_or_raise`
    (raises :class:`MarketplaceCacheMiss` when missing). A bare ``owner/repo``
    shorthand is expanded to a full HTTPS URL via :func:`_github_clone_url`
    before cloning; argv carries the ``--`` separator before that URL per
    CRITICAL-2 (flag-injection defense). On non-zero / timeout ``git clone``
    exit, raises :class:`MarketplaceCacheMiss` with a category-aware
    remediation built by :func:`_marketplace_clone_failure_message`
    (offline / repository-not-found / auth / generic).
    """
    git = _resolve_git_or_raise()
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    # A bare `owner/repo` shorthand must be expanded to a full HTTPS URL —
    # raw `git clone` does not understand the shorthand and fails as if
    # offline. Full URLs / SSH remotes / local paths pass through unchanged.
    clone_url = _github_clone_url(source.repo or "")
    try:
        # `--` separates options from positional args. Defends against
        # argv flag injection if the clone target ever starts with `-`
        # (e.g. `-upload-pack=...`), which git would otherwise interpret
        # as a flag rather than as the repo positional. CLAUDE.md
        # subprocess hygiene already mandates list-form argv and no
        # shell=True; this is the defense-in-depth completion of that.
        result = subprocess.run(
            [str(git), "clone", "--", clone_url, str(dest_path)],
            check=True,
            text=True,
            capture_output=True,
            timeout=_CLONE_TIMEOUT_S,
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        OSError,
    ) as exc:
        LOGGER.debug("git clone %r stderr: %s", clone_url, stderr_of(exc))
        raise MarketplaceCacheMiss(
            _marketplace_clone_failure_message(source.repo, stderr_of(exc))
        ) from exc
    _debug_git_output(f"git clone {clone_url!r}", result)


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
            _marketplace_refresh_failure_message(source.repo, str(exc), cache_dir)
        ) from exc


def checkout_marketplace_at(cache_dir: Path, sha: str) -> None:
    """Hard-reset the marketplace cache at ``cache_dir`` to the pinned ``sha``.

    Raises :class:`MarketplaceCacheMiss` if ``sha`` is not a 40-hex commit
    (a plugin pin must never be a moving ref), and propagates it from
    :func:`_run_git` on ``git fetch``/``git reset`` failure.
    """
    # Hard-resets to the PINNED sha, NOT origin/HEAD (otherwise the pin is defeated).
    if not _SHA_RE.match(sha):
        raise MarketplaceCacheMiss(
            f"refusing to pin marketplace cache {cache_dir} to non-SHA ref "
            f"{sha!r}; a plugin pin must be a 40-hex git commit"
        )
    _run_git("fetch", "origin", cwd=cache_dir, timeout=_CLONE_TIMEOUT_S)
    _run_git("reset", "--hard", sha, "--", cwd=cache_dir, timeout=_TIMEOUT_S)


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
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        OSError,
    ) as exc:
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
    cache-collision path — planning records the wizard choice before apply
    and preserves the ``auto=True`` non-interactive contract.
    """
    if mode is ClaudeInstallMode.REGULAR or source.source is MarketplaceSourceKind.PATH:
        return source
    plan = plan_marketplace_source(
        source,
        mode,
        cache_root=cache_root,
        mp_name=mp_name,
        auto=auto,
    )
    return apply_marketplace_source_plan(plan)


def plan_marketplace_source(
    source: MarketplaceSource,
    mode: ClaudeInstallMode,
    *,
    cache_root: Path | None = None,
    mp_name: str | None = None,
    auto: bool = False,
) -> MarketplaceSourcePlan:
    """Select source/cache actions without mutating the marketplace cache."""
    root = cache_root if cache_root is not None else MARKETPLACE_CACHE_ROOT
    frozen_source = source.model_copy(deep=True)
    if mode is ClaudeInstallMode.REGULAR or source.source is MarketplaceSourceKind.PATH:
        return MarketplaceSourcePlan(
            source_json=frozen_source.model_dump_json(),
            effective_source_json=frozen_source.model_dump_json(),
            action=MarketplaceSourceAction.NONE,
            cache_root=root,
        )
    if not source.repo:
        raise MarketplaceCacheMiss(
            "GITHUB marketplace source missing 'repo' field; cannot resolve "
            "local-clone path"
        )
    cache_dir = _safe_cache_dir(root, source.repo.rsplit("/", 1)[-1])
    effective = MarketplaceSource(source=MarketplaceSourceKind.PATH, path=cache_dir)
    if not cache_dir.exists():
        return MarketplaceSourcePlan(
            source_json=frozen_source.model_dump_json(),
            effective_source_json=effective.model_dump_json(),
            action=MarketplaceSourceAction.CLONE,
            cache_root=root,
            cache_dir=cache_dir,
        )

    origin = _cache_origin_url(cache_dir)
    if origin is None or origin == source.repo or _urls_equivalent(origin, source.repo):
        return MarketplaceSourcePlan(
            source_json=frozen_source.model_dump_json(),
            effective_source_json=effective.model_dump_json(),
            action=MarketplaceSourceAction.NONE,
            cache_root=root,
            cache_dir=cache_dir,
            expected_cache_exists=True,
            expected_origin=origin,
        )

    resolution = marketplace_cache_wizard.resolve_collision(
        mp_name=mp_name or source.repo,
        cache_dir=cache_dir,
        cache_root=root,
        existing_origin=origin,
        new_repo=source.repo,
        auto=auto,
    )
    if resolution.action is CollisionAction.KEEP:
        LOGGER.info(
            "cache-collision: using existing cache %r for marketplace %r; "
            "new source.repo %r NOT applied",
            cache_dir,
            mp_name or source.repo,
            source.repo,
        )
        action = MarketplaceSourceAction.NONE
        both_dir = None
    elif resolution.action is CollisionAction.UPDATE:
        action = MarketplaceSourceAction.UPDATE
        both_dir = None
    else:
        action = MarketplaceSourceAction.BOTH
        both_dir = resolution.new_cache_dir
        assert both_dir is not None, "wizard contract: BOTH carries new_cache_dir"
        effective = MarketplaceSource(source=MarketplaceSourceKind.PATH, path=both_dir)
    return MarketplaceSourcePlan(
        source_json=frozen_source.model_dump_json(),
        effective_source_json=effective.model_dump_json(),
        action=action,
        cache_root=root,
        cache_dir=cache_dir,
        expected_cache_exists=True,
        expected_origin=origin,
        both_dir=both_dir,
        expected_both_exists=both_dir.exists() if both_dir is not None else False,
    )


def validate_marketplace_source_plan(plan: MarketplaceSourcePlan) -> None:
    """Refuse when cache inputs changed after marketplace planning."""
    if plan.cache_dir is None:
        return
    if plan.cache_dir.exists() is not plan.expected_cache_exists:
        raise MarketplaceCacheMiss("marketplace cache changed after planning; retry")
    if (
        plan.expected_cache_exists
        and _cache_origin_url(plan.cache_dir) != plan.expected_origin
    ):
        raise MarketplaceCacheMiss(
            "marketplace cache origin changed after planning; retry"
        )
    if (
        plan.both_dir is not None
        and plan.both_dir.exists() is not plan.expected_both_exists
    ):
        raise MarketplaceCacheMiss(
            "marketplace cache target changed after planning; retry"
        )


def apply_marketplace_source_plan(plan: MarketplaceSourcePlan) -> MarketplaceSource:
    """Validate and apply exactly the cache action selected by ``plan``."""
    validate_marketplace_source_plan(plan)
    if plan.action is MarketplaceSourceAction.CLONE:
        assert plan.cache_dir is not None
        _clone_marketplace(plan.source, plan.cache_dir)
    elif plan.action is MarketplaceSourceAction.UPDATE:
        assert plan.cache_dir is not None
        _collision_update(plan.source, plan.cache_dir)
    elif plan.action is MarketplaceSourceAction.BOTH:
        assert plan.cache_dir is not None
        _collision_both(
            plan.source,
            plan.cache_dir,
            plan.both_dir,
            plan.cache_root,
        )
    return plan.effective_source


def _collision_update(source: MarketplaceSource, cache_dir: Path) -> MarketplaceSource:
    """``UPDATE``: re-clone ``source`` over the existing cache, clone-safe.

    Stages the new clone in a UNIQUE temp dir created via
    :func:`tempfile.mkdtemp` alongside ``cache_dir`` (same parent, hence
    same filesystem, so the final swap stays a same-device atomic
    rename), and only swaps it into place once the clone succeeds. The
    unique name — rather than a deterministic ``<name>.tmp`` sibling —
    means two concurrent UPDATEs resolving the same colliding repo
    cannot stomp each other's in-flight staging dir. A failed clone
    (offline, auth, bad repo) leaves the existing cache untouched —
    critical because in LOCAL_CLONE mode that cache is the offline source
    of truth and the UPDATE path is reached precisely when the network
    may be down; the staging dir is always discarded via a ``finally``
    cleanup on any failure or interruption, so a uniquely-named dir is
    never orphaned. The final swap (``rmtree`` then ``rename``) is not crash-atomic
    — an interruption between the two leaves the cache absent — but that
    window is bounded and the clone-failure path above is the one that
    matters for the offline-fallback guarantee.
    """
    LOGGER.info(
        "cache-collision: re-cloning %r over existing %r",
        source.repo,
        cache_dir,
    )
    # A unique staging dir (vs a shared ``<name>.tmp``) so concurrent
    # UPDATEs of the same colliding repo cannot delete each other's
    # in-flight clone. Sited in ``cache_dir.parent`` to keep the final
    # rename onto ``cache_dir`` on the same device (atomic).
    staging = Path(
        tempfile.mkdtemp(dir=cache_dir.parent, prefix=cache_dir.name + ".tmp.")
    )
    try:
        # mkdtemp already created ``staging`` empty; ``_clone_marketplace``
        # clones into it (git clones fine into an existing empty dir).
        _clone_marketplace(source, staging)
        # Reached only after a successful clone, so a failed clone leaves
        # ``cache_dir`` intact — the offline-fallback guarantee.
        shutil.rmtree(cache_dir)
        staging.replace(cache_dir)
    finally:
        # Discard any leftover uniquely-named staging dir — clone failure,
        # KeyboardInterrupt, or a swap that never completed. On success
        # ``os.replace`` already moved it, so this is a no-op. Unlike the
        # old deterministic ``<name>.tmp`` sibling, a unique staging dir is
        # never reclaimed by a later run, so it must be cleaned here.
        shutil.rmtree(staging, ignore_errors=True)
    return MarketplaceSource(
        source=MarketplaceSourceKind.PATH,
        path=cache_dir,
    )


def _collision_both(
    source: MarketplaceSource,
    cache_dir: Path,
    new_dir: Path | None,
    cache_root: Path,
) -> MarketplaceSource:
    """``BOTH``: clone into the wizard-supplied ``new_dir`` and persist the alias.

    Leaves the existing ``cache_dir`` untouched. Because the new repo is
    cloned into a non-basename subdir (e.g. ``plug-v2``), the declared
    identity computed by :func:`setforge.claude_plugins._source_identity`
    would otherwise recompute as ``cache_root/<basename>`` (the ORIGINAL
    colliding dir) on the next reconcile — making the marketplace look
    unregistered and re-firing this wizard on every install. To keep the
    outcome idempotent, record an ``owner/repo -> <new_dir name>`` alias in
    the cache-root sidecar (:func:`_record_cache_alias`); the identity
    computation consults it and resolves the declared marketplace to
    ``new_dir`` thereafter. The alias is written only after the clone
    succeeds, so a failed clone leaves no dangling mapping.
    """
    assert new_dir is not None, "wizard contract: BOTH carries new_cache_dir"
    LOGGER.info(
        "cache-collision: cloning %r into new cache %r (existing %r kept)",
        source.repo,
        new_dir,
        cache_dir,
    )
    _clone_marketplace(source, new_dir)
    if source.repo:
        _record_cache_alias(cache_root, source.repo, new_dir)
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
    full clone-rewrite surface in practice.
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

    # GitHub owner/repo identifiers are case-insensitive, so a case-variant
    # slug must not read as URL-changed — that would re-fire the collision
    # wizard / raise MarketplaceCacheMiss on every sync (INV-4 idempotency).
    return _normalize(observed).casefold() == _normalize(declared).casefold()


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
    for bare_name in reconcile_adapter.plugin_bare_names(cfg, profile):
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
        # Honor a BOTH-collision alias: if this repo was cloned into a
        # non-basename subdir (recorded owner/repo -> subdir in the cache
        # sidecar), refresh THAT dir. Mirrors _source_identity so an
        # aliased marketplace stays refreshable instead of colliding on the
        # basename dir and raising MarketplaceCacheMiss forever. Absent/
        # legacy sidecar degrades to plain basename resolution.
        alias = read_cache_aliases(root).get(source.repo)
        subdir = alias if alias is not None else source.repo.rsplit("/", 1)[-1]
        cache_dir = _safe_cache_dir(root, subdir)
        # This exists()-then-act is protected by the caller-held global resource
        # lock in every CLI mutation path. Direct helper callers must provide
        # equivalent serialization around cache discovery and mutation.
        if cache_dir.exists():
            # The cache dir is keyed by repo basename, so two marketplaces
            # from different owners sharing a repo name (alice/tools vs
            # bob/tools) map to the same dir. Verify the existing clone's
            # origin matches THIS marketplace's repo before refreshing —
            # otherwise `git fetch` + `git reset --hard origin/HEAD` would
            # silently reset the colliding repo's checkout against the wrong
            # remote, serving wrong content with no warning.
            origin = _cache_origin_url(cache_dir)
            if (
                origin is not None
                and origin != source.repo
                and not _urls_equivalent(origin, source.repo)
            ):
                raise MarketplaceCacheMiss(
                    f"marketplace {mp_name!r}: cache dir {cache_dir} already "
                    f"holds a clone of {origin!r}, but this marketplace "
                    f"declares repo {source.repo!r}. These repos share a "
                    f"basename and collide in the cache layout. Rename one "
                    f"marketplace's repo basename or remove {cache_dir} and "
                    f"re-run."
                )
            LOGGER.info("sync-cache: refreshing %s at %s", mp_name, cache_dir)
            _refresh_marketplace_cache(source, cache_dir)
        else:
            LOGGER.info("sync-cache: cloning %s into %s", mp_name, cache_dir)
            _clone_marketplace(source, cache_dir)
        refreshed.append(mp_name)
    return refreshed
