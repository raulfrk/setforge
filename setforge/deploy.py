"""Atomic file deploy primitive.

The deploy primitive is dotdrop's role reimplemented in stdlib + ruamel.yaml.
It writes a tracked file's content to its live destination atomically (via
``os.replace``) and keeps a single ``.bak`` rotation per file. Sub-file
reconciliation is owned by the unified per-unit reconcile engine
(:mod:`setforge.reconcile`); this primitive deploys tracked content verbatim
and the reconcile layer overrides the resolved content before the write.
"""

import contextlib
import logging
import os
import stat
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from setforge import atomicio
from setforge.config import Config, ResolvedProfile, TrackedFile, resolve_symlink_target
from setforge.errors import MissingTrackedFile, SetforgeError

LOGGER: logging.Logger = logging.getLogger(__name__)


class DeployAction(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    NOOP = "noop"
    REMOVED = "removed"


@dataclass(frozen=True, slots=True)
class DeployResult:
    """Outcome of one deploy write."""

    dst: Path
    action: DeployAction
    backup_path: Path | None


@dataclass(slots=True, frozen=True)
class ResolvedDeploy:
    """The fully-computed, not-yet-written outcome of a deploy resolution.

    Produced by :func:`resolve_deploy` (pure read) and consumed by
    :func:`write_resolved_deploy` (the only writer). Carries everything the
    write step needs: the post-merge ``content`` bytes, the symlink-resolved
    ``real_dst`` plus its ``dst_existed`` probe, and the ``effective_mode`` to
    apply. Holding these records in memory lets an orchestrator resolve EVERY
    file first and only then start writing (refuse-before-write).
    """

    real_dst: Path
    dst_existed: bool
    effective_mode: int
    content: bytes


def read_text_exact(path: Path) -> str:
    """Read ``path`` as text without altering a single byte.

    Line endings are kept (no universal-newline translation) and bytes that are
    not valid UTF-8 ride through as lone surrogates (``surrogateescape``).
    """
    return path.read_bytes().decode("utf-8", "surrogateescape")


def resolve_deploy(
    src: Path,
    dst: Path,
    *,
    mode: int | None = None,
) -> ResolvedDeploy:
    """Compute a verbatim deploy's content WITHOUT writing anything.

    The read half of a deploy: resolves ``dst`` through any
    pre-existing symlink, probes existence and effective mode, and reads the
    tracked source verbatim into memory. No directory is created and no file
    is touched — the returned :class:`ResolvedDeploy` is handed to
    :func:`write_resolved_deploy` when (and if) the caller decides to write.

    Sub-file reconciliation is owned by the per-unit reconcile engine
    (:mod:`setforge.reconcile`): the caller overrides
    :attr:`ResolvedDeploy.content` with the reconciled bytes before the write,
    so this function deploys ``src`` verbatim.

    Host-local content is owned by the reconcile engine (the marker-injection
    path was retired with the user-section markers), so this pass does not take
    any host-local overlay.

    ``mode`` is the POSIX file-mode bits to apply to ``dst`` via
    ``os.fchmod`` on the temp fd BEFORE ``os.replace`` (closes the
    TOCTOU symlink-swap window and bypasses umask). When ``None``, the source
    mode is captured now via :func:`stat.S_IMODE`; apply never stats mutable
    source metadata after the plan boundary.

    Raises :class:`MissingTrackedFile` when ``src`` does not exist.
    """
    src = Path(src)
    dst = Path(str(dst)).expanduser()

    if not src.exists():
        raise MissingTrackedFile(f"tracked source not found: {src}")

    real_dst = _resolve_for_copy(dst)
    dst_existed = real_dst.exists()

    return ResolvedDeploy(
        real_dst=real_dst,
        dst_existed=dst_existed,
        effective_mode=(mode if mode is not None else stat.S_IMODE(src.stat().st_mode)),
        content=src.read_bytes(),
    )


def classify_write(resolved: ResolvedDeploy) -> DeployAction:
    """Classify what writing ``resolved`` does to the live file as it is now.

    The one rule shared by the install preview and the write: an existing file
    whose bytes or mode differ from the resolved ones is UPDATED.
    """
    if not resolved.dst_existed:
        return DeployAction.CREATED
    real_dst = resolved.real_dst
    if (
        real_dst.read_bytes() != resolved.content
        or stat.S_IMODE(real_dst.stat().st_mode) != resolved.effective_mode
    ):
        return DeployAction.UPDATED
    return DeployAction.NOOP


def write_resolved_deploy(
    resolved: ResolvedDeploy, *, backup: bool = True
) -> DeployResult:
    """Write a :class:`ResolvedDeploy` to disk: the only deploy write step.

    Creates the destination's parent directories, classifies the write with
    :func:`classify_write`, then fixes a mode-only difference in place or
    writes the content through :func:`_atomic_write`.

    **Inter-resolve/write staleness assumption.** ``resolved`` snapshots the
    live file at :func:`resolve_deploy` time; an external edit to the live
    file between the resolve and this write is silently overwritten by the
    resolved content. setforge is a single-process CLI whose deploys are
    serialized under the profile lock, so the window is accepted and NOT
    re-checked here — the same single-setforge-process model documented for
    the symlink ordering window on :func:`deploy_symlinked_file`.
    """
    real_dst, mode = resolved.real_dst, resolved.effective_mode
    real_dst.parent.mkdir(parents=True, exist_ok=True)
    action = classify_write(resolved)
    if action is DeployAction.NOOP:
        return DeployResult(dst=real_dst, action=action, backup_path=None)

    if resolved.dst_existed and real_dst.read_bytes() == resolved.content:
        # Mode-only drift: a path-based chmod is safe (no content swap to
        # race, real_dst already symlink-resolved).
        real_dst.chmod(mode)
        return DeployResult(dst=real_dst, action=action, backup_path=None)
    backup_path = _atomic_write(
        resolved.content, real_dst, resolved.dst_existed, backup, mode
    )
    return DeployResult(dst=real_dst, action=action, backup_path=backup_path)


def _resolve_for_copy(dst: Path) -> Path:
    """Resolve ``dst`` through any pre-existing symlink for legacy nolink copy.

    Mirrors the legacy ``link_tracked_file_default: nolink`` behavior:
    when ``dst`` is itself a symlink, write to its target (so the link
    survives the deploy). When :func:`Path.resolve` fails — broken
    link, dangling component, or :class:`RuntimeError` from cpython's
    symlink-loop detection — the original ``dst`` is returned and the
    caller treats it as a fresh write.

    ``strict=False`` is mandatory: ``Path.resolve(strict=True)`` raises
    :class:`OSError` on missing targets; ``strict=False`` swallows
    every :class:`OSError` EXCEPT the rare symlink-loop case (CPython
    bug #109187), which surfaces as :class:`RuntimeError`. The
    ``except (OSError, RuntimeError)`` covers both shapes so a hostile
    symlink layout can't crash deploy.
    """
    if not dst.is_symlink():
        return dst
    try:
        return dst.resolve(strict=False)
    except (OSError, RuntimeError):
        return dst


def _atomic_write(
    content: bytes, dst: Path, dst_existed: bool, backup: bool, mode: int
) -> Path | None:
    """Atomically write ``content`` to ``dst`` with explicit mode bits.

    Thin wrapper over :func:`setforge.atomicio.atomic_write_bytes`,
    which owns the tempfile + fchmod-on-fd + ``.bak``-rotation +
    ``os.replace`` dance (and pins fchmod-before-replace so the TOCTOU
    symlink-swap window stays closed). The backup is gated on
    ``dst_existed`` so a fresh deploy never tries to snapshot an absent
    destination. ``fsync=False`` is load-bearing: deploy has never fsynced
    its writes (only flushed), and byte-identical behavior means not adding
    durability silently.
    """
    return atomicio.atomic_write_bytes(
        dst, content, fsync=False, mode=mode, backup=backup and dst_existed
    )


def deploy_symlinked_file(
    dst: Path,
    tracked_file: TrackedFile,
    *,
    source_content: bytes,
    source_mode: int,
    backup: bool = True,
) -> DeployResult:
    """Deploy a tracked_file that declares ``symlink:``.

    Two-phase write:

    1. Render the tracked content to the declared target path via
       :func:`_atomic_write`. Relative targets are anchored at ``dst.parent``,
       matching filesystem symlink resolution.
    2. Create a symbolic link at ``dst`` pointing at the *raw user
       string* (``tracked_file.symlink``, NOT expanded) so cross-host
       portability survives. The link itself is staged at a sibling
       tempfile and ``os.replace``-d into place — the same atomic
       pattern :func:`_atomic_write` uses for regular files, closing
       the TOCTOU window between ``unlink`` and ``symlink``.

    ``source_content`` and ``source_mode`` are the immutable source snapshot
    the plan captured before the first write.

    Raises :class:`AssertionError` when ``tracked_file.symlink`` is None —
    a caller-contract violation (this function must only be called for a
    tracked_file that declares ``symlink:``), not a runtime/config
    condition.

    Refusal contract: if ``dst`` already exists as a *regular file* or a
    *directory* (anything that is not a symlink), this function raises
    :class:`SetforgeError` — with a message distinguishing the two cases.
    The caller should treat that as drift requiring user intervention
    rather than silently clobbering local content. A pre-existing
    symlink at ``dst`` — regardless of where it points — is replaced
    atomically by :func:`os.replace`.

    Returns a :class:`DeployResult` mirroring :func:`write_resolved_deploy`'s
    contract. ``backup_path`` is None for symlink deployments: the
    target-side write produces its own ``.bak`` for the byte content,
    and a link itself carries no rotateable state.

    Ordering window: target write precedes the link swap, so a
    concurrent reader following the *old* link (or the new link, if
    the dst path is racing with a sibling process) may briefly observe
    the new target bytes via the OLD link's path before this function
    swings the dst link onto its new target. Not exploitable in a
    security sense — the caller controls both paths — but worth
    knowing if a setforge install races with another tool reading the
    same tracked symlinks. Same-host SetForge writers are serialized; install
    additionally supplies frozen source content so checkout edits after its
    plan boundary cannot change the deployed bytes.
    """
    if tracked_file.symlink is None:
        raise AssertionError(
            "deploy_symlinked_file called with tracked_file.symlink == None"
        )

    target = resolve_symlink_target(dst, tracked_file.symlink)
    target.parent.mkdir(parents=True, exist_ok=True)
    dst.parent.mkdir(parents=True, exist_ok=True)

    if dst.exists() and not dst.is_symlink():
        # Distinguish directory-at-dst from regular-file-at-dst so the
        # overlay-fields symlink_target overlay surfaces the
        # "directory in the way" case with a targeted message — silently
        # clobbering or recursing into a real directory layout is
        # almost certainly a config mistake.
        if dst.is_dir():
            raise SetforgeError(
                f"refusing to deploy symlink at {dst}: a directory is "
                f"already present. Move or remove it before deploying "
                f"tracked_file with symlink: {tracked_file.symlink!r}."
            )
        raise SetforgeError(
            f"refusing to deploy symlink at {dst}: a regular file is "
            f"already present. Move it aside or remove it before "
            f"deploying tracked_file with symlink: {tracked_file.symlink!r}."
        )

    _atomic_write(source_content, target, target.exists(), backup, source_mode)
    action = _replace_symlink_atomic(dst, tracked_file.symlink)
    return DeployResult(dst=dst, action=action, backup_path=None)


def _replace_symlink_atomic(dst: Path, raw_target: str) -> DeployAction:
    """Place a symlink at ``dst`` pointing at ``raw_target`` via tmp+replace.

    ``raw_target`` is the *unexpanded* user string (e.g. ``~/foo``);
    :func:`os.symlink` writes it verbatim into the link's metadata so
    a subsequent :func:`os.readlink` returns exactly that string —
    cross-host portability invariant. ``os.replace`` atomically swaps
    the staged link over any pre-existing link at ``dst`` (the
    regular-file case is refused by the caller).

    Fast-path: when ``dst`` is already a symlink with ``raw_target``
    verbatim, skip the tmp+replace dance entirely and return
    :attr:`DeployAction.NOOP` — a re-install of an already-correct
    link should not show ``UPDATED`` in the install summary nor
    spend an :func:`os.symlink` + :func:`os.replace` syscall pair.

    Returns :attr:`DeployAction.CREATED` when ``dst`` had no prior
    symlink, :attr:`DeployAction.NOOP` when the prior symlink already
    pointed at ``raw_target``, otherwise :attr:`DeployAction.UPDATED`.
    """
    # readlink() returns a Path; str() restores the verbatim link string so the
    # NOOP fast-path compares like-for-like against the raw_target string.
    if dst.is_symlink() and str(dst.readlink()) == raw_target:
        return DeployAction.NOOP
    dst_was_link = dst.is_symlink()
    # Stage the link at a UNIQUE temp name (mkstemp, matching the regular
    # atomic-write path in atomicio) so a stale leftover of a fixed name — a
    # directory or foreign file from a crashed run — cannot wedge the swap.
    # mkstemp creates a placeholder regular file; remove it, then symlink onto
    # the now-free unique path before the atomic replace onto dst.
    fd, tmp_name = tempfile.mkstemp(
        dir=str(dst.parent), prefix=f".{dst.name}.", suffix=".setforge-symlink-tmp"
    )
    os.close(fd)
    tmp_link = Path(tmp_name)
    try:
        tmp_link.unlink()
        # symlink_to flips arg order: link.symlink_to(t) == os.symlink(t, link).
        tmp_link.symlink_to(raw_target)
        tmp_link.replace(dst)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp_link.unlink()
        raise
    return DeployAction.UPDATED if dst_was_link else DeployAction.CREATED


def bootstrap_local(paths: Sequence[Path]) -> None:
    """Ensure each host-local file exists with parent directories.

    Used for ``~/.claude/header.md``, ``~/.claude/additional-content.md``,
    and any other never-tracked-but-referenced file. Creates an empty
    file if missing; a no-op if present.
    """
    for raw in paths:
        path = Path(str(raw)).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.touch()
            LOGGER.info("created stub: %s", path)


def validate_srcs_exist(
    cfg: Config, resolved: ResolvedProfile, repo_root: Path
) -> None:
    """Pre-flight: every tracked ``src`` path in the resolved profile
    must exist on disk. Raises a single :class:`MissingTrackedFile`
    listing every missing path so ``install`` fails before any deploy
    or backup happens.
    """
    from setforge.compare import resolve_src

    missing: list[str] = []
    for name in resolved.tracked_files:
        tracked_file = cfg.tracked_files[name]
        src = resolve_src(tracked_file, repo_root)
        if not src.exists():
            missing.append(f"{name}: {src}")
    if missing:
        joined = "\n  ".join(missing)
        raise MissingTrackedFile(f"missing tracked source(s):\n  {joined}")
