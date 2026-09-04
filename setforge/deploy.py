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
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from setforge import atomicio
from setforge.config import Config, ResolvedProfile, TrackedFile, resolve_symlink_target
from setforge.errors import MissingTrackedFile, SetforgeError
from setforge.markdown_merge import LineConflict
from setforge.structural_merge import PathConflict

LOGGER: logging.Logger = logging.getLogger(__name__)


class DeployAction(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    NOOP = "noop"
    REMOVED = "removed"


@dataclass(frozen=True, slots=True)
class DeployResult:
    """Outcome of a :func:`copy_atomic` call.

    ``new_base`` / ``merge_conflicts`` are populated on the disposition
    (byte-base 3-way) path, and inert defaults on a plain verbatim deploy.

    ``prior_mode`` records the live file's permission bits AS THEY WERE
    immediately before this deploy chmod-ed them, and ONLY when the deploy
    actually changed the mode of a pre-existing file (a content-NOOP
    mode-only fixup, or a content UPDATE whose tracked mode differs from
    the live mode). It is ``None`` whenever the mode was untouched (fresh
    CREATE, true NOOP, or an UPDATE whose mode already matched). The
    install-side transition writer records this so ``revert`` can restore
    the pre-install mode in lockstep with the content patch reverse — the
    content patch alone never carries permission bits.
    """

    dst: Path
    action: DeployAction
    backup_path: Path | None
    new_base: str | None = None
    merge_conflicts: list[LineConflict | PathConflict] = field(default_factory=list)
    prior_mode: int | None = None


@dataclass(slots=True, frozen=True)
class ResolvedDeploy:
    """The fully-computed, not-yet-written outcome of a deploy resolution.

    Produced by :func:`resolve_deploy` (pure read) and consumed by
    :func:`write_resolved_deploy` (the only writer). Carries everything the
    write step needs: the post-merge / post-overlay ``content``, the
    symlink-resolved ``real_dst`` plus its ``dst_existed`` probe, the
    ``effective_mode`` to apply, and the state-advance payload
    (``new_base`` / ``merge_conflicts``) that :class:`DeployResult` threads
    back to the caller. Holding these records in memory lets an orchestrator
    resolve EVERY file first and only then start writing (refuse-before-write).
    """

    src: Path
    real_dst: Path
    dst_existed: bool
    effective_mode: int
    content: str
    new_base: str | None
    merge_conflicts: list[LineConflict | PathConflict]


def copy_atomic(
    src: Path,
    dst: Path,
    *,
    backup: bool = True,
    mode: int | None = None,
) -> DeployResult:
    """Atomically deploy ``src`` to ``dst`` verbatim.

    Composes :func:`resolve_deploy` (the pure read) with
    :func:`write_resolved_deploy` (the only write step). The two-step seam
    exists so an orchestrator can resolve every file before writing any.

    When ``dst`` is a symlink the operation resolves to its target so the
    symlink itself is preserved (matches the legacy Makefile's behavior
    with ``link_tracked_file_default: nolink``).

    When the resulting content is byte-identical to the existing ``dst``,
    no write or backup is performed (action == :attr:`DeployAction.NOOP`).

    Raises :class:`MissingTrackedFile` when ``src`` does not exist (propagated
    from :func:`resolve_deploy`).
    """
    resolved = resolve_deploy(
        src,
        dst,
        mode=mode,
    )
    return write_resolved_deploy(resolved, backup=backup)


def resolve_deploy(
    src: Path,
    dst: Path,
    *,
    mode: int | None = None,
) -> ResolvedDeploy:
    """Compute a verbatim deploy's content WITHOUT writing anything.

    The read half of :func:`copy_atomic`: resolves ``dst`` through any
    pre-existing symlink, probes existence and effective mode, and reads the
    tracked source verbatim into memory. No directory is created and no file
    is touched — the returned :class:`ResolvedDeploy` is handed to
    :func:`write_resolved_deploy` when (and if) the caller decides to write.

    Sub-file reconciliation is owned by the per-unit reconcile engine
    (:mod:`setforge.reconcile`): the caller overrides
    :attr:`ResolvedDeploy.content` with the reconciled bytes before the write,
    so this function deploys ``src`` verbatim and leaves ``new_base`` /
    ``merge_conflicts`` inert.

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

    content = src.read_text(encoding="utf-8")

    return ResolvedDeploy(
        src=src,
        real_dst=real_dst,
        dst_existed=dst_existed,
        effective_mode=(mode if mode is not None else stat.S_IMODE(src.stat().st_mode)),
        content=content,
        new_base=None,
        merge_conflicts=[],
    )


def write_resolved_deploy(
    resolved: ResolvedDeploy, *, backup: bool = True
) -> DeployResult:
    """Write a :class:`ResolvedDeploy` to disk: the write half of :func:`copy_atomic`.

    Creates the destination's parent directories, then routes the resolved
    content through the shared NOOP/CREATED/UPDATED detection +
    :func:`_atomic_write` (see :func:`_write_resolved_content`). The
    resolution's state-advance payload rides through onto the returned
    :class:`DeployResult` unchanged.

    **Inter-resolve/write staleness assumption.** ``resolved`` snapshots the
    live file at :func:`resolve_deploy` time; an external edit to the live
    file between the resolve and this write is silently overwritten by the
    resolved content. setforge is a single-process CLI whose deploys are
    serialized under the profile lock, so the window is accepted and NOT
    re-checked here — the same single-setforge-process model documented for
    the symlink ordering window on :func:`deploy_symlinked_file`.
    """
    resolved.real_dst.parent.mkdir(parents=True, exist_ok=True)
    return _write_resolved_content(
        resolved.content,
        resolved.src,
        resolved.real_dst,
        resolved.dst_existed,
        backup,
        resolved.effective_mode,
        new_base=resolved.new_base,
        merge_conflicts=resolved.merge_conflicts,
    )


def _write_resolved_content(
    content: str,
    src: Path,
    real_dst: Path,
    dst_existed: bool,
    backup: bool,
    mode: int | None,
    *,
    new_base: str | None,
    merge_conflicts: list[LineConflict | PathConflict],
) -> DeployResult:
    """Apply NOOP/CREATED/UPDATED detection + atomic write to ``content``.

    Shared by both branches of :func:`copy_atomic` so the NOOP-detection,
    mode-only-drift fixup and :func:`_atomic_write` logic live in one place.
    ``new_base`` / ``merge_conflicts`` (disposition path) are threaded onto
    EVERY returned :class:`DeployResult` — including the NOOP and
    mode-only-drift paths — so a clean disposition merge that equals live still
    re-baselines even on a NOOP write whose post-splice content already equals
    live.
    """
    if dst_existed:
        existing = real_dst.read_text(encoding="utf-8")
        action = DeployAction.NOOP if existing == content else DeployAction.UPDATED
    else:
        action = DeployAction.CREATED

    if action is DeployAction.NOOP:
        # Content matches, but mode bits may have drifted. compare flags
        # mode-only drift; apply it here (path-based chmod is safe — no
        # content swap to race, real_dst already symlink-resolved) so
        # install fixes perms instead of reporting "unchanged".
        prior_mode: int | None = stat.S_IMODE(real_dst.stat().st_mode)
        if mode is not None and prior_mode != mode:
            real_dst.chmod(mode)
            return DeployResult(
                dst=real_dst,
                action=DeployAction.UPDATED,
                backup_path=None,
                new_base=new_base,
                merge_conflicts=merge_conflicts,
                # The content patch is empty for a mode-only fixup, so the
                # pre-install mode is the ONLY reversible record of this
                # change — hand it to the transition writer for revert.
                prior_mode=prior_mode,
            )
        return DeployResult(
            dst=real_dst,
            action=action,
            backup_path=None,
            new_base=new_base,
            merge_conflicts=merge_conflicts,
        )

    # Capture the live mode BEFORE the atomic write swaps perms, but only
    # when this UPDATE actually changes them (pre-existing dst whose mode
    # differs from the mode the write will apply). ``revert`` restores the
    # content via the patch reverse; ``prior_mode`` lets it restore perms in
    # lockstep, since atomic_write_text fchmods to the tracked/source mode.
    prior_mode = None
    if dst_existed:
        live_mode = stat.S_IMODE(real_dst.stat().st_mode)
        write_mode = mode if mode is not None else stat.S_IMODE(src.stat().st_mode)
        if live_mode != write_mode:
            prior_mode = live_mode
    backup_path = _atomic_write(content, src, real_dst, dst_existed, backup, mode)
    return DeployResult(
        dst=real_dst,
        action=action,
        backup_path=backup_path,
        new_base=new_base,
        merge_conflicts=merge_conflicts,
        prior_mode=prior_mode,
    )


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
    content: str,
    src: Path,
    dst: Path,
    dst_existed: bool,
    backup: bool,
    mode: int | None,
) -> Path | None:
    """Atomically write ``content`` to ``dst`` with explicit mode bits.

    Thin wrapper over :func:`setforge.atomicio.atomic_write_text`,
    which owns the tempfile + fchmod-on-fd + ``.bak``-rotation +
    ``os.replace`` dance (and pins fchmod-before-replace so the TOCTOU
    symlink-swap window stays closed). Deploy-specific semantics live
    here: ``mode=None`` falls back to the SOURCE file's perm bits (via
    :func:`stat.S_IMODE`) — today's behavior — and the backup is gated
    on ``dst_existed`` so a fresh deploy never tries to snapshot an
    absent destination. ``fsync=False`` is load-bearing: deploy has
    never fsynced its writes (only flushed), and byte-identical
    behavior means not adding durability silently.
    """
    effective_mode = mode if mode is not None else stat.S_IMODE(src.stat().st_mode)
    return atomicio.atomic_write_text(
        dst,
        content,
        fsync=False,
        mode=effective_mode,
        backup=backup and dst_existed,
    )


def deploy_symlinked_file(
    src: Path,
    dst: Path,
    tracked_file: TrackedFile,
    *,
    backup: bool = True,
    source_content: str | None = None,
    source_mode: int | None = None,
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

    ``source_content`` and ``source_mode`` let a plan supply the immutable
    source snapshot captured before the first write. Direct callers may omit
    both to retain the legacy read-at-call behavior.

    Raises :class:`AssertionError` when ``tracked_file.symlink`` is None —
    a caller-contract violation (this function must only be called for a
    tracked_file that declares ``symlink:``), not a runtime/config
    condition. Raises :class:`MissingTrackedFile` when ``src`` does not
    exist.

    Refusal contract: if ``dst`` already exists as a *regular file* or a
    *directory* (anything that is not a symlink), this function raises
    :class:`SetforgeError` — with a message distinguishing the two cases.
    The caller should treat that as drift requiring user intervention
    rather than silently clobbering local content. A pre-existing
    symlink at ``dst`` — regardless of where it points — is replaced
    atomically by :func:`os.replace`.

    Returns a :class:`DeployResult` mirroring :func:`copy_atomic`'s
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
    if source_content is None and not src.exists():
        raise MissingTrackedFile(f"tracked source not found: {src}")
    if (source_content is None) != (source_mode is None):
        raise AssertionError("source_content and source_mode must be supplied together")

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

    _deploy_target_content(
        src,
        target,
        tracked_file,
        backup=backup,
        source_content=source_content,
        source_mode=source_mode,
    )
    action = _replace_symlink_atomic(dst, tracked_file.symlink)
    return DeployResult(dst=dst, action=action, backup_path=None)


def _deploy_target_content(
    src: Path,
    target: Path,
    tracked_file: TrackedFile,
    *,
    backup: bool,
    source_content: str | None,
    source_mode: int | None,
) -> None:
    """Write ``src`` content verbatim to ``target`` via :func:`_atomic_write`.

    Symlink-deployed tracked_files carry no host-local overlay after the
    marker-retire migration (host-local content is markerless overlay-only, and
    symlink targets do not yet route through the overlay injector — a future
    enhancement). ``mode`` rides through unchanged.
    """
    target_existed = target.exists()
    content = (
        source_content
        if source_content is not None
        else src.read_text(encoding="utf-8")
    )
    mode = source_mode if source_mode is not None else tracked_file.mode
    _atomic_write(content, src, target, target_existed, backup, mode)


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
