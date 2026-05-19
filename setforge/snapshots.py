"""Directory-copy snapshot/restore primitives for setforge.

Captures the profile-resolved ``tracked_files.dst`` set plus
``~/.config/setforge/local.yaml`` into a sortable, atomically-finalized
directory under ``~/.local/share/setforge/snapshots/<id>/``. Restore is
an additive overlay: only files present in the snapshot are overlaid
onto live; live-only files added since the snapshot are left
untouched. Auto-prune fires AFTER successful create — a failed create
keeps the prior good snapshot.

Storage layout::

    ~/.local/share/setforge/snapshots/
    ├── 20260518T210000Z-before-experiment/
    │   ├── _meta.json                            # commit marker (LAST)
    │   ├── home/raul/.claude/CLAUDE.md          # mirror of dst paths
    │   ├── home/raul/.config/setforge/local.yaml
    │   └── ...

Atomicity: create writes to ``<id>.partial/``, fsyncs each regular
file, writes ``_meta.json`` LAST as the commit marker, then
``os.replace(partial, final)`` atomically renames. Restore refuses any
snapshot missing ``_meta.json``.

Symlink discipline: snapshots preserve symlinks AS symlinks
(``os.symlink(os.readlink(src), dst)``); ``os.walk(followlinks=False)``
prevents balloon walks if a symlink in ``~/.claude/`` points up the
tree. Restore unlinks pre-existing dst symlinks before write to avoid
following them through to their target.

Mode preservation: ``shutil.copy2`` preserves mode + mtime; we then
mask the setuid/setgid bits (``mode & 0o7777 & ~0o6000``) because
snapshots are user-owned and these bits are security-sensitive.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from setforge.binaries import LOCAL_CONFIG_PATH
from setforge.compare import expand_tracked_file, resolve_dst, resolve_src
from setforge.config import Config, ResolvedProfile
from setforge.errors import SetforgeError
from setforge.transitions import now_utc

DEFAULT_KEEP: Final[int] = 10
"""Default retention count for auto-prune."""

_META_FILENAME: Final[str] = "_meta.json"
"""Commit marker written LAST inside the snapshot dir."""

_PARTIAL_SUFFIX: Final[str] = ".partial"
"""Temporary suffix used during atomic create."""

_SETUID_SETGID_MASK: Final[int] = ~0o6000 & 0o7777
"""Mask to strip the setuid + setgid bits while preserving the low 9 + sticky."""

_SNAPSHOT_TIMESTAMP_FMT: Final[str] = "%Y%m%dT%H%M%SZ"
"""UTC timestamp prefix for snapshot ids (matches ``transition_dirname``)."""


@dataclass(slots=True, frozen=True)
class PreSnapshotCtx:
    """Named bundle of the four args needed to capture a pre-restore snapshot.

    ``restore_snapshot(..., pre_snapshot=True)`` writes a fresh snapshot
    of current live state BEFORE applying the restore. That fresh
    snapshot needs the same ``(cfg, resolved, repo_root, profile)`` tuple
    ``create_snapshot`` does; bundling them as a named dataclass keeps
    the CLI seam readable and the call signature self-documenting.
    """

    cfg: Config
    resolved: ResolvedProfile
    repo_root: Path
    profile: str


@dataclass(slots=True, frozen=True)
class SnapshotMeta:
    """Metadata for one snapshot. Serialized to ``_meta.json``."""

    snapshot_id: str
    label: str
    created_at: datetime
    profile: str
    files: tuple[Path, ...]

    def to_dict(self) -> dict[str, object]:
        """Render as a JSON-ready dict (paths as strings)."""
        return {
            "snapshot_id": self.snapshot_id,
            "label": self.label,
            "created_at": self.created_at.isoformat(),
            "profile": self.profile,
            "files": [str(p) for p in self.files],
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> SnapshotMeta:
        """Inverse of :meth:`to_dict`."""
        files_raw = data["files"]
        if not isinstance(files_raw, list):
            raise SetforgeError(
                f"snapshot meta: 'files' must be a list, got {type(files_raw).__name__}"
            )
        return cls(
            snapshot_id=str(data["snapshot_id"]),
            label=str(data["label"]),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            profile=str(data["profile"]),
            files=tuple(Path(str(f)) for f in files_raw),
        )


def snapshots_root() -> Path:
    """Return the XDG-data root where snapshots live."""
    return Path.home() / ".local" / "share" / "setforge" / "snapshots"


def _snapshot_id(label: str, *, timestamp: datetime | None = None) -> str:
    """Build the ``<YYYYMMDDTHHMMSSZ>-<label>`` snapshot id."""
    ts = timestamp if timestamp is not None else now_utc()
    return f"{ts.strftime(_SNAPSHOT_TIMESTAMP_FMT)}-{label}"


def _resolve_dst_paths(
    cfg: Config, resolved: ResolvedProfile, repo_root: Path
) -> list[Path]:
    """Resolve every ``tracked_files.dst`` for the resolved profile, plus local.yaml.

    Mirrors the existing ``expand_tracked_file`` walk so directory-shaped
    tracked entries contribute one path per contained file. ``local.yaml``
    is appended last when it exists; it is NOT a tracked file but is the
    host-local config surface snapshots must capture.
    """
    dst_paths: list[Path] = []
    seen: set[Path] = set()
    for name in resolved.tracked_files:
        tracked_file = cfg.tracked_files[name]
        src = resolve_src(tracked_file, repo_root)
        dst = resolve_dst(tracked_file)
        for _, _, sub_dst in expand_tracked_file(name, src, dst):
            if sub_dst not in seen:
                seen.add(sub_dst)
                dst_paths.append(sub_dst)
    if LOCAL_CONFIG_PATH.exists() and LOCAL_CONFIG_PATH not in seen:
        dst_paths.append(LOCAL_CONFIG_PATH)
    return dst_paths


def _mirror_path(snapshot_dir: Path, live_path: Path) -> Path:
    """Compute the per-file in-snapshot mirror path for an absolute live path.

    Strips the leading ``/`` and joins under ``snapshot_dir`` — so
    ``/home/raul/.claude/CLAUDE.md`` becomes
    ``<snapshot_dir>/home/raul/.claude/CLAUDE.md``.
    """
    if not live_path.is_absolute():
        raise SetforgeError(
            f"snapshot: refusing to mirror non-absolute live path: {live_path}"
        )
    return snapshot_dir / live_path.relative_to("/")


def _fsync_path(path: Path) -> None:
    """fsync ``path`` (file or directory) if the OS exposes the fd."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _copy_one(src: Path, dst: Path) -> None:
    """Copy one live path into the snapshot tree, preserving symlinks + mode.

    - Symlinks are recreated as symlinks pointing at the same link
      target (``os.symlink(os.readlink(src), dst)``); we do NOT
      dereference them.
    - Regular files use :func:`shutil.copy2` (preserves mode + mtime),
      then mask the setuid/setgid bits on the destination because
      snapshots are user-owned.
    - Directories are NOT a valid src here — the caller resolves a
      flat file list via :func:`_resolve_dst_paths`.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_symlink():
        target = os.readlink(src)
        # Idempotent: defensively unlink in case a retry hit the same target.
        if dst.exists() or dst.is_symlink():
            dst.unlink()
        os.symlink(target, dst)
        return
    if not src.is_file():
        raise SetforgeError(
            f"snapshot: refusing to capture non-regular non-symlink path: {src}"
        )
    shutil.copy2(src, dst, follow_symlinks=False)
    src_mode = src.stat().st_mode
    dst.chmod(stat.S_IMODE(src_mode) & _SETUID_SETGID_MASK)
    _fsync_path(dst)


def _write_meta(snapshot_dir: Path, meta: SnapshotMeta) -> None:
    """Write the commit-marker ``_meta.json`` and fsync."""
    meta_path = snapshot_dir / _META_FILENAME
    payload = json.dumps(meta.to_dict(), indent=2) + "\n"
    meta_path.write_text(payload, encoding="utf-8")
    _fsync_path(meta_path)


def _load_meta(snapshot_dir: Path) -> SnapshotMeta:
    """Read ``_meta.json`` from a finalized snapshot dir.

    Raises :class:`SetforgeError` if the file is missing — that signals
    an incomplete snapshot (creator crashed before the commit marker).
    """
    meta_path = snapshot_dir / _META_FILENAME
    if not meta_path.exists():
        raise SetforgeError(
            f"snapshot {snapshot_dir.name}: missing {_META_FILENAME} "
            f"(incomplete or corrupt snapshot)"
        )
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SetforgeError(
            f"snapshot {snapshot_dir.name}: corrupt {_META_FILENAME}: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise SetforgeError(
            f"snapshot {snapshot_dir.name}: {_META_FILENAME} is not an object"
        )
    return SnapshotMeta.from_dict(data)


def _capture_files(partial_dir: Path, paths: Sequence[Path]) -> list[Path]:
    """Copy every live path that exists into ``partial_dir``; return captured list.

    Missing live files (e.g. first-install profile with no live yet) are
    skipped silently — snapshot fidelity is "files that exist now" and
    restore is additive, so absence stays absence.
    """
    captured: list[Path] = []
    for live_path in paths:
        if not live_path.exists() and not live_path.is_symlink():
            continue
        mirror = _mirror_path(partial_dir, live_path)
        _copy_one(live_path, mirror)
        captured.append(live_path)
    return captured


def _finalize(
    partial_dir: Path, final_dir: Path, meta: SnapshotMeta, keep: int
) -> None:
    """Write the commit marker, atomically rename partial → final, then prune.

    Prune fires AFTER ``os.replace`` so a crashed create never deletes
    the prior good snapshot — retention only kicks in once the new
    snapshot is fully on disk.
    """
    _write_meta(partial_dir, meta)
    os.replace(partial_dir, final_dir)
    prune_snapshots(keep)


def create_snapshot(
    cfg: Config,
    resolved: ResolvedProfile,
    repo_root: Path,
    profile: str,
    label: str,
    *,
    keep: int = DEFAULT_KEEP,
) -> SnapshotMeta:
    """Create a new snapshot for ``profile`` labeled ``label``.

    Atomicity: writes to ``<root>/<id>.partial/``, copies every resolved
    live file, writes ``_meta.json`` LAST as the commit marker, then
    ``os.replace`` renames the partial dir to its final id. Auto-prune
    runs AFTER successful create so a crashed create leaves the previous
    good snapshot intact.

    Raises :class:`SetforgeError` when ``label`` is empty, when ``keep``
    is negative, or when the snapshot root cannot be created.
    """
    if not label:
        raise SetforgeError("snapshot: --label must be a non-empty string")
    if keep < 0:
        raise SetforgeError(f"snapshot: --keep must be non-negative, got {keep}")
    root = snapshots_root()
    root.mkdir(parents=True, exist_ok=True)
    created_at = now_utc()
    snapshot_id = _snapshot_id(label, timestamp=created_at)
    partial_dir = root / f"{snapshot_id}{_PARTIAL_SUFFIX}"
    final_dir = root / snapshot_id
    if final_dir.exists():
        raise SetforgeError(
            f"snapshot {snapshot_id} already exists at {final_dir}; "
            f"choose a different --label or wait a moment"
        )
    if partial_dir.exists():
        shutil.rmtree(partial_dir)
    partial_dir.mkdir(parents=True)

    try:
        captured = _capture_files(
            partial_dir, _resolve_dst_paths(cfg, resolved, repo_root)
        )
        meta = SnapshotMeta(
            snapshot_id=snapshot_id,
            label=label,
            created_at=created_at,
            profile=profile,
            files=tuple(captured),
        )
        _finalize(partial_dir, final_dir, meta, keep)
    except BaseException:
        # On any failure during write: remove the .partial dir so a
        # subsequent attempt sees a clean slate. Re-raise unchanged.
        shutil.rmtree(partial_dir, ignore_errors=True)
        raise
    return meta


def list_snapshots() -> list[SnapshotMeta]:
    """Return every finalized snapshot under :func:`snapshots_root`, newest first.

    Incomplete (no ``_meta.json``) and corrupt entries are skipped
    silently — they remain on disk for manual inspection but never
    surface to callers. ``.partial`` dirs are filtered out by name.
    """
    root = snapshots_root()
    if not root.is_dir():
        return []
    snapshots: list[SnapshotMeta] = []
    for entry in sorted(root.iterdir(), reverse=True):
        if not entry.is_dir():
            continue
        if entry.name.endswith(_PARTIAL_SUFFIX):
            continue
        try:
            snapshots.append(_load_meta(entry))
        except SetforgeError:
            # Skip incomplete/corrupt snapshots; the user can inspect
            # them by hand under the snapshot root.
            continue
    return snapshots


def resolve_snapshot(snapshot_id_or_label: str) -> SnapshotMeta:
    """Resolve a user-supplied id-or-label to one ``SnapshotMeta``.

    Match precedence:
    1. Exact ``snapshot_id`` match (the full ``<ts>-<label>``).
    2. Exact ``label`` match across all snapshots (newest wins on tie).

    Raises :class:`SetforgeError` when no match is found.
    """
    candidates = list_snapshots()
    for snap in candidates:
        if snap.snapshot_id == snapshot_id_or_label:
            return snap
    for snap in candidates:
        if snap.label == snapshot_id_or_label:
            return snap
    raise SetforgeError(
        f"snapshot not found: {snapshot_id_or_label!r} "
        f"(run 'setforge snapshot list' to see available ids/labels)"
    )


def _restore_one(src: Path, dst: Path) -> None:
    """Overlay one snapshot-tree file onto its live destination.

    Mirrors :func:`_copy_one` in reverse direction: preserves symlinks,
    unlinks any pre-existing live symlink before write (so a live
    symlink target is not silently followed), copies mode + mtime,
    masks setuid/setgid.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink() or dst.exists():
        # remove pre-existing dst (incl. symlinks) so copy2 lands on a
        # fresh inode.
        dst.unlink()
    if src.is_symlink():
        os.symlink(os.readlink(src), dst)
        return
    shutil.copy2(src, dst, follow_symlinks=False)
    src_mode = src.stat().st_mode
    dst.chmod(stat.S_IMODE(src_mode) & _SETUID_SETGID_MASK)


def _run_pre_snapshot(
    target: SnapshotMeta, pre_snapshot_ctx: PreSnapshotCtx
) -> SnapshotMeta:
    """Capture a fresh ``pre-restore-<target.snapshot_id>`` snapshot.

    Returns the new pre-restore snapshot's meta. Called by
    :func:`restore_snapshot` before the overlay so the user has a
    single-step undo if the restored state is undesirable.
    """
    return create_snapshot(
        pre_snapshot_ctx.cfg,
        pre_snapshot_ctx.resolved,
        pre_snapshot_ctx.repo_root,
        pre_snapshot_ctx.profile,
        f"pre-restore-{target.snapshot_id}",
    )


def restore_snapshot(
    snapshot_id_or_label: str,
    *,
    pre_snapshot: bool,
    pre_snapshot_ctx: PreSnapshotCtx | None = None,
) -> SnapshotMeta:
    """Overlay the snapshot's files onto live (additive overlay).

    When ``pre_snapshot=True``, captures a fresh snapshot of current
    live state BEFORE applying the restore — gives the user a
    single-step undo if the restored state is undesirable. The
    pre-snapshot is labeled ``pre-restore-<snapshot_id>`` and uses
    ``pre_snapshot_ctx`` (required in that case).

    Restore is an additive overlay: files present in the snapshot get
    overlaid onto their live destinations; files that exist live but
    not in the snapshot are left alone.

    Returns the restored snapshot's :class:`SnapshotMeta`. Raises
    :class:`SetforgeError` on missing/corrupt snapshot or when
    ``pre_snapshot=True`` without a ``pre_snapshot_ctx``.
    """
    target = resolve_snapshot(snapshot_id_or_label)
    root = snapshots_root()
    snapshot_dir = root / target.snapshot_id
    if pre_snapshot:
        if pre_snapshot_ctx is None:
            raise SetforgeError(
                "snapshot restore: --pre-snapshot requires a profile context"
            )
        _run_pre_snapshot(target, pre_snapshot_ctx)
    for live_path in target.files:
        mirror = _mirror_path(snapshot_dir, live_path)
        if not (mirror.exists() or mirror.is_symlink()):
            # The meta references a file that's missing from the
            # snapshot tree — corrupt; refuse the whole restore to
            # avoid a partial overlay.
            raise SetforgeError(
                f"snapshot {target.snapshot_id}: meta references "
                f"{live_path} but {mirror} is missing on disk"
            )
        _restore_one(mirror, live_path)
    return target


def prune_snapshots(keep: int) -> int:
    """Delete oldest snapshots until at most ``keep`` remain. Returns count removed.

    ``keep=0`` removes every snapshot (explicit "no retention"); ``keep
    < 0`` raises :class:`SetforgeError` rather than silently meaning
    "unlimited" (borg's footgun). Only finalized snapshots are counted
    against the limit; ``.partial`` dirs are ignored (they're cleanup
    candidates handled by failed-create logic, not by retention).
    """
    if keep < 0:
        raise SetforgeError(f"snapshot prune: keep must be non-negative, got {keep}")
    snapshots = list_snapshots()
    excess = snapshots[keep:]
    removed = 0
    root = snapshots_root()
    for snap in excess:
        snapshot_dir = root / snap.snapshot_id
        if snapshot_dir.is_dir():
            shutil.rmtree(snapshot_dir)
            removed += 1
    return removed


def directory_size_bytes(snapshot_id: str) -> int:
    """Return the total byte size of ``<root>/<snapshot_id>`` (followlinks=False).

    Used by ``snapshot list``'s size column. ``followlinks=False`` on
    :func:`os.walk` prevents balloon walks if a symlink inside the
    snapshot tree points outward.
    """
    root = snapshots_root() / snapshot_id
    if not root.is_dir():
        return 0
    total = 0
    for dirpath, _, filenames in os.walk(root, followlinks=False):
        dir_path = Path(dirpath)
        for name in filenames:
            file_path = dir_path / name
            try:
                # lstat() returns symlink-string size for symlinks;
                # mirrors on-disk footprint, not target file size.
                total += file_path.lstat().st_size
            except FileNotFoundError:
                continue
    return total


def format_age(now: datetime, then: datetime) -> str:
    """Format ``now - then`` as a coarse ``Nh ago`` / ``Nd ago`` / ``Nm ago``.

    Coarse on purpose: snapshot list is a quick visual scan, not a
    precise audit log. ``transitions list`` already uses the same
    coarse-bucketing convention.
    """
    delta = now - then
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def format_size(num_bytes: int) -> str:
    """Format ``num_bytes`` as a human-readable string (``32M``, ``1.2G``)."""
    for unit, threshold in (
        ("G", 1024**3),
        ("M", 1024**2),
        ("K", 1024),
    ):
        if num_bytes >= threshold:
            value = num_bytes / threshold
            if value >= 100:
                return f"{value:.0f}{unit}"
            if value >= 10:
                return f"{value:.1f}{unit}"
            return f"{value:.2f}{unit}"
    return f"{num_bytes}B"


__all__ = [
    "DEFAULT_KEEP",
    "PreSnapshotCtx",
    "SnapshotMeta",
    "create_snapshot",
    "directory_size_bytes",
    "format_age",
    "format_size",
    "list_snapshots",
    "prune_snapshots",
    "resolve_snapshot",
    "restore_snapshot",
    "snapshots_root",
]
