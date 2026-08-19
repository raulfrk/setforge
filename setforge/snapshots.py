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

Symlink discipline: snapshots preserve symlinks AS symlinks;
``os.walk(followlinks=False)``
prevents balloon walks if a symlink in ``~/.claude/`` points up the
tree. Restore unlinks pre-existing dst symlinks before write to avoid
following them through to their target.

Mode preservation: frozen plans retain mode + mtime; apply masks the
setuid/setgid bits (``mode & 0o7777 & ~0o6000``) because snapshots are
user-owned and these bits are security-sensitive.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from setforge import atomicio, operations, transitions
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

_LOGGER = logging.getLogger(__name__)


def _require_meta_text(data: dict[str, object], field: str) -> str:
    value = data[field]
    if not isinstance(value, str) or not value:
        raise SetforgeError(f"snapshot meta: {field!r} must be non-empty text")
    return value


def _parse_meta_timestamp(raw: str) -> datetime:
    try:
        created_at = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise SetforgeError("snapshot meta: invalid 'created_at'") from exc
    if created_at.tzinfo is None:
        raise SetforgeError("snapshot meta: 'created_at' must include a timezone")
    return created_at


def _parse_meta_files(raw: object) -> tuple[Path, ...]:
    if not isinstance(raw, list):
        raise SetforgeError(
            f"snapshot meta: 'files' must be a list, got {type(raw).__name__}"
        )
    files: list[Path] = []
    seen: set[Path] = set()
    for raw_path in raw:
        if not isinstance(raw_path, str) or not raw_path:
            raise SetforgeError(
                "snapshot meta: every file must be canonical absolute text"
            )
        path = Path(raw_path)
        if (
            "\x00" in raw_path
            or not path.is_absolute()
            or path == Path("/")
            or ".." in path.parts
            or raw_path != str(path)
        ):
            raise SetforgeError(
                "snapshot meta: every file must be a canonical absolute path"
            )
        if path in seen:
            raise SetforgeError(f"snapshot meta: duplicate file path {path}")
        seen.add(path)
        files.append(path)
    return tuple(files)


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

    def __post_init__(self) -> None:
        """Keep direct construction inside the persisted schema contract."""
        for field, value in (
            ("snapshot_id", self.snapshot_id),
            ("label", self.label),
            ("profile", self.profile),
        ):
            if not isinstance(value, str) or not value or "\x00" in value:
                raise SetforgeError(
                    f"snapshot meta: {field!r} must be non-empty safe text"
                )
        if not isinstance(self.created_at, datetime):
            raise SetforgeError("snapshot meta: 'created_at' must be a datetime")
        if self.created_at.utcoffset() is None:
            raise SetforgeError("snapshot meta: 'created_at' must include a timezone")
        if _parse_meta_files([str(path) for path in self.files]) != self.files:
            raise SetforgeError("snapshot meta: files must be canonical paths")

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
        required = {"snapshot_id", "label", "created_at", "profile", "files"}
        if set(data) != required:
            raise SetforgeError(
                "snapshot meta: expected exactly " + ", ".join(sorted(required))
            )
        return cls(
            snapshot_id=_require_meta_text(data, "snapshot_id"),
            label=_require_meta_text(data, "label"),
            created_at=_parse_meta_timestamp(_require_meta_text(data, "created_at")),
            profile=_require_meta_text(data, "profile"),
            files=_parse_meta_files(data["files"]),
        )


@dataclass(slots=True, frozen=True)
class _FrozenSnapshotFile:
    """Stable file or symlink payload consumed by snapshot apply."""

    path: Path
    kind: operations.SnapshotKind
    mode: int | None
    payload: bytes | None
    link_target: str | None
    mtime_ns: int


@dataclass(slots=True, frozen=True)
class _RestorePlan:
    """Validated immutable input for one additive snapshot restore."""

    target: SnapshotMeta
    files: tuple[_FrozenSnapshotFile, ...]
    destination_ancestors: tuple[operations.PathGuard, ...]


def snapshots_root() -> Path:
    """Return the XDG-data root where snapshots live."""
    return Path.home() / ".local" / "share" / "setforge" / "snapshots"


def _snapshot_id(label: str, *, timestamp: datetime | None = None) -> str:
    """Build the ``<YYYYMMDDTHHMMSSZ>-<label>`` snapshot id."""
    if (
        not label
        or label in {".", ".."}
        or Path(label).name != label
        or label.endswith(_PARTIAL_SUFFIX)
        or any(ord(character) < 32 or ord(character) == 127 for character in label)
    ):
        raise SetforgeError("snapshot: --label must be a safe non-empty name")
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
    if LOCAL_CONFIG_PATH not in seen:
        dst_paths.append(LOCAL_CONFIG_PATH)
    return dst_paths


def _mirror_path(snapshot_dir: Path, live_path: Path) -> Path:
    """Compute the per-file in-snapshot mirror path for an absolute live path.

    Strips the leading ``/`` and joins under ``snapshot_dir`` — so
    ``/home/raul/.claude/CLAUDE.md`` becomes
    ``<snapshot_dir>/home/raul/.claude/CLAUDE.md``.
    """
    if (
        not live_path.is_absolute()
        or live_path in {Path("/"), Path(f"/{_META_FILENAME}")}
        or ".." in live_path.parts
    ):
        raise SetforgeError(
            f"snapshot: refusing to mirror unsafe live path: {live_path}"
        )
    return snapshot_dir / live_path.relative_to("/")


def _freeze_file(path: Path) -> _FrozenSnapshotFile | None:
    """Capture one stable regular-file or symlink payload without rereads."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SetforgeError(
            f"snapshot source changed while planning {path}; retry"
        ) from exc
    captured = operations.snapshot_path(path)
    if captured.kind is operations.SnapshotKind.ABSENT:
        raise SetforgeError(f"snapshot source changed while planning {path}; retry")
    if captured.kind is operations.SnapshotKind.DIRECTORY:
        raise SetforgeError(
            f"snapshot: refusing to capture non-regular non-symlink path: {path}"
        )
    try:
        after = path.lstat()
    except OSError as exc:
        raise SetforgeError(
            f"snapshot source changed while planning {path}; retry"
        ) from exc
    stable_fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        raise SetforgeError(f"snapshot source changed while planning {path}; retry")
    return _FrozenSnapshotFile(
        path=path,
        kind=captured.kind,
        mode=captured.mode,
        payload=captured.payload,
        link_target=captured.link_target,
        mtime_ns=before.st_mtime_ns,
    )


def _write_frozen_file(source: _FrozenSnapshotFile, destination: Path) -> None:
    """Write one frozen payload without consulting its source path again."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_dir() and not destination.is_symlink():
        raise SetforgeError(
            f"snapshot: refusing to replace mirror directory: {destination}"
        )
    if destination.exists() or destination.is_symlink():
        destination.unlink()
    if source.kind is operations.SnapshotKind.SYMLINK:
        if source.link_target is None:
            raise AssertionError("frozen symlink is missing its target")
        destination.symlink_to(source.link_target)
        os.utime(
            destination,
            ns=(source.mtime_ns, source.mtime_ns),
            follow_symlinks=False,
        )
        atomicio.fsync_dir(destination.parent)
        return
    if source.kind is not operations.SnapshotKind.FILE or source.payload is None:
        raise AssertionError(f"unsupported frozen snapshot kind: {source.kind}")
    mode = source.mode if source.mode is not None else 0o600
    atomicio.atomic_write_bytes(
        destination,
        source.payload,
        mode=mode & _SETUID_SETGID_MASK,
    )
    os.utime(
        destination,
        ns=(source.mtime_ns, source.mtime_ns),
        follow_symlinks=False,
    )
    atomicio.fsync_path(destination, strict=True)


def _write_meta(snapshot_dir: Path, meta: SnapshotMeta) -> None:
    """Write the commit-marker ``_meta.json`` and fsync."""
    meta_path = snapshot_dir / _META_FILENAME
    payload = json.dumps(meta.to_dict(), indent=2) + "\n"
    meta_path.write_text(payload, encoding="utf-8")
    atomicio.fsync_path(meta_path, strict=True)
    atomicio.fsync_dir(snapshot_dir)


def _load_meta(snapshot_dir: Path) -> SnapshotMeta:
    """Read ``_meta.json`` from a finalized snapshot dir.

    Raises :class:`SetforgeError` if the file is missing — that signals
    an incomplete snapshot (creator crashed before the commit marker).
    """
    try:
        directory_before = snapshot_dir.lstat()
    except OSError as exc:
        raise SetforgeError(
            f"snapshot {snapshot_dir.name}: unsafe snapshot directory"
        ) from exc
    if not stat.S_ISDIR(directory_before.st_mode):
        raise SetforgeError(f"snapshot {snapshot_dir.name}: unsafe snapshot directory")
    meta_path = snapshot_dir / _META_FILENAME
    fd: int | None = None
    try:
        fd = os.open(meta_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError("metadata is not a regular file")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = None
            data = json.loads(stream.read())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SetforgeError(
            f"snapshot {snapshot_dir.name}: corrupt {_META_FILENAME}: {exc}"
        ) from exc
    finally:
        if fd is not None:
            os.close(fd)
    if not isinstance(data, dict):
        raise SetforgeError(
            f"snapshot {snapshot_dir.name}: {_META_FILENAME} is not an object"
        )
    meta = SnapshotMeta.from_dict(data)
    if meta.snapshot_id != snapshot_dir.name:
        raise SetforgeError(
            f"snapshot {snapshot_dir.name}: metadata id {meta.snapshot_id!r} "
            "does not match its directory"
        )
    try:
        directory_after = snapshot_dir.lstat()
    except OSError as exc:
        raise SetforgeError(
            f"snapshot {snapshot_dir.name}: directory changed while loading; retry"
        ) from exc
    if (
        directory_before.st_dev,
        directory_before.st_ino,
        directory_before.st_mode,
    ) != (
        directory_after.st_dev,
        directory_after.st_ino,
        directory_after.st_mode,
    ):
        raise SetforgeError(
            f"snapshot {snapshot_dir.name}: directory changed while loading; retry"
        )
    return meta


def _capture_files(partial_dir: Path, paths: Sequence[Path]) -> list[Path]:
    """Copy every live path that exists into ``partial_dir``; return captured list.

    Missing live files (e.g. first-install profile with no live yet) are
    skipped silently — snapshot fidelity is "files that exist now" and
    restore is additive, so absence stays absence.
    """
    planned: list[_FrozenSnapshotFile] = []
    for live_path in paths:
        frozen = _freeze_file(live_path)
        if frozen is not None:
            planned.append(frozen)
    for frozen in planned:
        mirror = _require_safe_mirror(partial_dir, frozen.path)
        _write_frozen_file(frozen, mirror)
    return [frozen.path for frozen in planned]


def _finalize(
    partial_dir: Path, final_dir: Path, meta: SnapshotMeta, keep: int
) -> None:
    """Write the commit marker, atomically rename partial → final, then prune.

    Prune fires AFTER ``os.replace`` so a crashed create never deletes
    the prior good snapshot — retention only kicks in once the new
    snapshot is fully on disk.
    """
    _write_meta(partial_dir, meta)
    partial_dir.replace(final_dir)
    atomicio.fsync_dir(final_dir.parent)
    try:
        prune_snapshots(keep)
    except OSError as exc:
        with suppress(Exception):  # diagnostics cannot undo publication
            _LOGGER.warning(
                "snapshot %s was created, but retention pruning failed: %s",
                meta.snapshot_id,
                exc,
            )


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

    finalized = False
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
        finalized = True
    finally:
        # On any non-finalized exit (including KeyboardInterrupt /
        # SystemExit / GeneratorExit): remove the .partial dir so a
        # subsequent attempt sees a clean slate. Runs only on failure;
        # on success partial_dir was already renamed to final_dir.
        if not finalized:
            shutil.rmtree(partial_dir, ignore_errors=True)
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


def resolve_snapshot(
    snapshot_id_or_label: str, *, profile: str | None = None
) -> SnapshotMeta:
    """Resolve a user-supplied id-or-label to one ``SnapshotMeta``.

    Match precedence:
    1. Exact ``snapshot_id`` match (the full ``<ts>-<label>``).
    2. Exact ``label`` match across all snapshots (newest wins on tie).

    Raises :class:`SetforgeError` when no match is found.
    """
    candidates = tuple(
        snap for snap in list_snapshots() if profile is None or snap.profile == profile
    )
    for snap in candidates:
        if snap.snapshot_id == snapshot_id_or_label:
            return snap
    for snap in candidates:
        if snap.label == snapshot_id_or_label:
            return snap
    raise SetforgeError(
        f"snapshot not found: {snapshot_id_or_label!r} "
        + (f"for profile {profile!r} " if profile is not None else "")
        + "(run 'setforge snapshot list' to see available ids/labels)"
    )


def _require_safe_mirror(snapshot_dir: Path, live_path: Path) -> Path:
    """Return a mirror path whose intermediate components are real directories."""
    mirror = _mirror_path(snapshot_dir, live_path)
    current = snapshot_dir
    for component in live_path.relative_to("/").parts[:-1]:
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SetforgeError(
                f"snapshot {snapshot_dir.name}: unsafe mirror parent {current}"
            )
    return mirror


def _snapshot_destination_ancestors(
    paths: Sequence[Path],
) -> tuple[operations.PathGuard, ...]:
    """Freeze every lexical destination parent without following symlinks."""
    ancestors: dict[Path, operations.PathGuard] = {}
    for live_path in paths:
        relative_parts = live_path.relative_to("/").parts[:-1]
        current = Path("/")
        for component in relative_parts:
            current /= component
            if current in ancestors:
                continue
            try:
                info = current.lstat()
            except FileNotFoundError:
                ancestors[current] = operations.PathGuard(current, None, None, None)
                continue
            except OSError as exc:
                raise SetforgeError(
                    f"snapshot restore: destination parent changed: {current}; retry"
                ) from exc
            if stat.S_ISLNK(info.st_mode):
                raise SetforgeError(
                    "snapshot restore: refusing symlinked destination parent: "
                    f"{current}"
                )
            if not stat.S_ISDIR(info.st_mode):
                raise SetforgeError(
                    "snapshot restore: destination parent is not a directory: "
                    f"{current}"
                )
            try:
                after = current.lstat()
            except OSError as exc:
                raise SetforgeError(
                    f"snapshot restore: destination parent changed: {current}; retry"
                ) from exc
            fields = (
                info.st_dev,
                info.st_ino,
                info.st_mode,
            )
            if fields != (
                after.st_dev,
                after.st_ino,
                after.st_mode,
            ):
                raise SetforgeError(
                    f"snapshot restore: destination parent changed: {current}; retry"
                )
            ancestors[current] = operations.PathGuard(
                path=current,
                device=fields[0],
                inode=fields[1],
                mode=fields[2],
            )
    return tuple(ancestors.values())


def _require_safe_restore_destinations(target: SnapshotMeta) -> None:
    """Reject destinations that overlap SetForge's restore control paths."""
    controls = (
        operations.journals_root().parent.absolute(),
        snapshots_root().absolute(),
        transitions.state_root().absolute(),
    )
    for destination in target.files:
        for control in controls:
            lexical_destination = destination.expanduser().absolute()
            lexical_control = control.expanduser().absolute()
            resolved_destination = lexical_destination.resolve(strict=False)
            resolved_control = lexical_control.resolve(strict=False)
            if any(
                left == right
                or left.is_relative_to(right)
                or right.is_relative_to(left)
                for left, right in (
                    (lexical_destination, lexical_control),
                    (resolved_destination, resolved_control),
                )
            ):
                raise SetforgeError(
                    "snapshot restore: managed destination overlaps SetForge "
                    f"control path {control}: {destination}"
                )


def _plan_restore_snapshot(
    snapshot_id_or_label: str,
    *,
    cfg: Config | None = None,
    resolved: ResolvedProfile | None = None,
    repo_root: Path | None = None,
    profile: str | None = None,
) -> _RestorePlan:
    """Validate and freeze every source consumed by an additive restore."""
    context_values = (cfg, resolved, repo_root, profile)
    if any(value is not None for value in context_values) and any(
        value is None for value in context_values
    ):
        raise SetforgeError("snapshot restore: incomplete effective-profile context")
    target = resolve_snapshot(snapshot_id_or_label, profile=profile)
    if cfg is not None and resolved is not None and repo_root is not None:
        allowed = set(_resolve_dst_paths(cfg, resolved, repo_root))
        unmanaged = tuple(path for path in target.files if path not in allowed)
        if unmanaged:
            raise SetforgeError(
                f"snapshot {target.snapshot_id}: destination is no longer managed "
                f"by profile {profile!r}: {unmanaged[0]}"
            )
    snapshot_dir = snapshots_root() / target.snapshot_id
    _require_safe_restore_destinations(target)
    try:
        directory_before = snapshot_dir.lstat()
    except OSError as exc:
        raise SetforgeError(
            f"snapshot {target.snapshot_id}: directory changed before planning; retry"
        ) from exc
    if _load_meta(snapshot_dir) != target:
        raise SetforgeError(
            f"snapshot {target.snapshot_id}: metadata changed before planning; retry"
        )
    files: list[_FrozenSnapshotFile] = []
    for live_path in target.files:
        mirror = _require_safe_mirror(snapshot_dir, live_path)
        frozen = _freeze_file(mirror)
        if frozen is None:
            raise SetforgeError(
                f"snapshot {target.snapshot_id}: meta references "
                f"{live_path} but {mirror} is missing on disk"
            )
        files.append(
            _FrozenSnapshotFile(
                path=live_path,
                kind=frozen.kind,
                mode=frozen.mode,
                payload=frozen.payload,
                link_target=frozen.link_target,
                mtime_ns=frozen.mtime_ns,
            )
        )
    try:
        directory_after = snapshot_dir.lstat()
    except OSError as exc:
        raise SetforgeError(
            f"snapshot {target.snapshot_id}: directory changed while planning; retry"
        ) from exc
    if (
        directory_before.st_dev,
        directory_before.st_ino,
        directory_before.st_mode,
    ) != (
        directory_after.st_dev,
        directory_after.st_ino,
        directory_after.st_mode,
    ):
        raise SetforgeError(
            f"snapshot {target.snapshot_id}: directory changed while planning; retry"
        )
    return _RestorePlan(
        target=target,
        files=tuple(files),
        destination_ancestors=_snapshot_destination_ancestors(target.files),
    )


def _validate_restore_plan(plan: _RestorePlan) -> None:
    """Refuse a changed destination topology before the first restore write."""
    if (
        _snapshot_destination_ancestors(tuple(file.path for file in plan.files))
        != plan.destination_ancestors
    ):
        raise SetforgeError(
            "snapshot restore: destination parent topology changed after "
            "planning; retry"
        )


def _write_restored_file(
    frozen: _FrozenSnapshotFile,
    guard_identities: dict[Path, tuple[int, int, int] | None],
) -> None:
    """Publish one frozen payload beneath descriptor-verified parents."""
    operations._restore_path(
        operations.PathSnapshot(
            path=frozen.path,
            kind=frozen.kind,
            mode=(
                frozen.mode & _SETUID_SETGID_MASK if frozen.mode is not None else None
            ),
            payload=frozen.payload,
            link_target=frozen.link_target,
            mtime_ns=frozen.mtime_ns,
        ),
        guard_identities=guard_identities,
        permit_existing_absent=False,
    )


def _apply_restore_plan(plan: _RestorePlan, *, validate: bool = True) -> SnapshotMeta:
    """Apply one fully validated frozen restore plan additively."""
    if validate:
        _validate_restore_plan(plan)
    guard_identities = operations._guard_identities(plan.destination_ancestors)
    for frozen in plan.files:
        _write_restored_file(frozen, guard_identities)
    return plan.target


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
    plan = _plan_restore_snapshot(
        snapshot_id_or_label,
        cfg=pre_snapshot_ctx.cfg if pre_snapshot_ctx is not None else None,
        resolved=pre_snapshot_ctx.resolved if pre_snapshot_ctx is not None else None,
        repo_root=pre_snapshot_ctx.repo_root if pre_snapshot_ctx is not None else None,
        profile=pre_snapshot_ctx.profile if pre_snapshot_ctx is not None else None,
    )
    target = plan.target
    if pre_snapshot:
        if pre_snapshot_ctx is None:
            raise SetforgeError(
                "snapshot restore: --pre-snapshot requires a profile context"
            )
        _run_pre_snapshot(target, pre_snapshot_ctx)
    return _apply_restore_plan(plan)


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
