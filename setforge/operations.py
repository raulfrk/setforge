"""Durable write-ahead journals for transition-backed mutations."""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import json
import os
import shutil
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final, cast
from uuid import uuid4

from setforge import atomicio, transitions
from setforge.errors import SetforgeError

JOURNAL_SCHEMA_VERSION: Final[int] = 1


class OperationPhase(StrEnum):
    """Durable lifecycle state of one operation journal."""

    PREPARED = "prepared"
    APPLYING = "applying"
    RECOVERING = "recovering"
    MANUAL = "manual"


class SnapshotKind(StrEnum):
    """Filesystem object kinds supported by automatic recovery."""

    ABSENT = "absent"
    FILE = "file"
    SYMLINK = "symlink"
    DIRECTORY = "directory"


class CheckpointKind(StrEnum):
    """Recovery capability of an effect checkpoint."""

    REVERSIBLE = "reversible"
    COMPENSATABLE = "compensatable"
    IRREVERSIBLE = "irreversible"


class AdapterKind(StrEnum):
    """External inventories with executable baseline compensation."""

    EXTENSIONS = "extensions"
    PLUGINS = "plugins"
    MCP = "mcp"


@dataclass(frozen=True, slots=True)
class PathSnapshot:
    """Exact pre-operation state of one filesystem path."""

    path: Path
    kind: SnapshotKind
    mode: int | None = None
    payload: bytes | None = None
    link_target: str | None = None


@dataclass(frozen=True, slots=True)
class OperationCheckpoint:
    """One write-ahead effect intent and its durable completion state."""

    name: str
    kind: CheckpointKind
    recovery: str
    paths: tuple[str, ...] = ()
    restore_state: bool = False
    restore_transitions: bool = False
    adapters: tuple[AdapterKind, ...] = ()
    completed: bool = False
    recovered: bool = False


@dataclass(frozen=True, slots=True)
class AdapterSnapshot:
    """Frozen JSON baseline for one external adapter."""

    kind: AdapterKind
    payload_json: str


@dataclass(frozen=True, slots=True)
class OperationJournal:
    """Immutable in-memory view of one active journal."""

    operation_id: str
    command: str
    profile: str
    config_dir: Path | None
    state_dir: Path
    resources_lock: bool
    phase: OperationPhase
    created_at: str
    command_line: tuple[str, ...]
    paths: tuple[PathSnapshot, ...]
    state_snapshots: tuple[transitions.StateSnapshotEntry, ...]
    reserved_profiles: tuple[str, ...] = ()
    adapters: tuple[AdapterSnapshot, ...] = ()
    checkpoints: tuple[OperationCheckpoint, ...] = ()
    transition_names_before: tuple[str, ...] = ()


def locked_profiles(journal: OperationJournal) -> tuple[str, ...]:
    """Return every profile namespace reserved by ``journal`` in lock order."""
    if journal.reserved_profiles:
        return journal.reserved_profiles
    return tuple(
        sorted({journal.profile, *(item.profile for item in journal.state_snapshots)})
    )


def journals_root() -> Path:
    """Return the user-global active-operation namespace.

    Recovery reservations protect user-global adapters and config repositories,
    so an operator-selected transition state root must not hide them.
    """
    return Path("~/.cache/setforge/operations").expanduser()


def journal_path(profile: str) -> Path:
    """Return a traversal-safe deterministic active-journal path."""
    digest = hashlib.sha256(profile.encode("utf-8")).hexdigest()[:24]
    return journals_root() / f"{digest}.json"


@contextmanager
def _registry_lock() -> Iterator[None]:
    """Serialize global journal discovery and creation across processes."""
    root = journals_root()
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".registry.lock").open("a") as fd:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)


def _load_all() -> tuple[OperationJournal, ...]:
    """Load every global active journal, failing closed on corruption."""
    root = journals_root()
    if not root.exists():
        return ()
    journals: list[OperationJournal] = []
    for path in sorted(root.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            profile = _require_str(raw, "profile")
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise SetforgeError(f"corrupt operation journal {path}: {exc}") from exc
        expected = journal_path(profile)
        if path != expected:
            raise SetforgeError(f"operation journal has invalid identity: {path}")
        journals.append(load(profile))
    return tuple(journals)


def snapshot_path(path: Path) -> PathSnapshot:
    """Capture one stable path state without following its final symlink."""
    path = path.expanduser().absolute()
    try:
        info = path.lstat()
    except FileNotFoundError:
        return PathSnapshot(path=path, kind=SnapshotKind.ABSENT)
    try:
        if stat.S_ISLNK(info.st_mode):
            link_target = str(path.readlink())
            if not _same_snapshot_stat(info, path.lstat()):
                raise OSError("symlink identity changed")
            return PathSnapshot(
                path=path,
                kind=SnapshotKind.SYMLINK,
                mode=stat.S_IMODE(info.st_mode),
                link_target=link_target,
            )
        if stat.S_ISREG(info.st_mode):
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags)
            try:
                opened = os.fstat(fd)
                if not _same_snapshot_stat(info, opened):
                    raise OSError("file identity changed")
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    payload = stream.read()
                final = os.fstat(fd)
                if not _same_snapshot_stat(opened, final):
                    raise OSError("file changed while reading")
            finally:
                os.close(fd)
            return PathSnapshot(
                path=path,
                kind=SnapshotKind.FILE,
                mode=stat.S_IMODE(opened.st_mode),
                payload=payload,
            )
        if stat.S_ISDIR(info.st_mode):
            if not _same_snapshot_stat(info, path.lstat()):
                raise OSError("directory identity changed")
            return PathSnapshot(
                path=path,
                kind=SnapshotKind.DIRECTORY,
                mode=stat.S_IMODE(info.st_mode),
            )
    except OSError as exc:
        raise SetforgeError(
            f"filesystem path changed while snapshotting {path}; retry"
        ) from exc
    raise SetforgeError(f"cannot journal unsupported filesystem object: {path}")


def _same_snapshot_stat(left: os.stat_result, right: os.stat_result) -> bool:
    """Compare identity and mutable metadata used by a path snapshot."""
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def prepare(
    *,
    command: str,
    profile: str,
    config_dir: Path | None,
    resources_lock: bool,
    command_line: tuple[str, ...],
    paths: tuple[Path, ...],
    state_snapshots: tuple[transitions.StateSnapshotEntry, ...] = (),
    adapters: tuple[AdapterSnapshot, ...] = (),
    profiles: tuple[str, ...] = (),
) -> OperationJournal:
    """Durably publish a complete prepared journal or refuse an active one."""
    with _registry_lock():
        existing = _load_all()
        if existing:
            active_journal = existing[0]
            raise SetforgeError(
                f"unfinished {active_journal.command} operation "
                f"{active_journal.operation_id} blocks this mutation; run "
                f"`setforge recover --profile={active_journal.profile}`"
            )
        journal = OperationJournal(
            operation_id=uuid4().hex,
            command=command,
            profile=profile,
            config_dir=config_dir.resolve() if config_dir is not None else None,
            state_dir=transitions.state_root().resolve(),
            resources_lock=resources_lock,
            phase=OperationPhase.PREPARED,
            created_at=datetime.now(UTC).isoformat(),
            command_line=command_line,
            paths=_snapshot_paths_with_missing_ancestors(paths),
            state_snapshots=state_snapshots,
            reserved_profiles=tuple(
                sorted(
                    {
                        profile,
                        *profiles,
                        *(item.profile for item in state_snapshots),
                    }
                )
            ),
            adapters=adapters,
            transition_names_before=_committed_transition_names(),
        )
        _write(journal, create=True)
        return journal


def _snapshot_paths_with_missing_ancestors(
    paths: tuple[Path, ...],
) -> tuple[PathSnapshot, ...]:
    """Capture requested paths plus absent parents a writer may create."""
    before = _paths_with_missing_ancestors(paths)
    snapshots = tuple(snapshot_path(path) for path in before)
    after = _paths_with_missing_ancestors(paths)
    if before != after:
        raise SetforgeError(
            "filesystem ancestor topology changed while snapshotting; retry"
        )
    return snapshots


def _paths_with_missing_ancestors(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    """Enumerate requested paths and the currently absent parent chain."""
    expanded: dict[Path, None] = {}
    for raw_path in paths:
        path = raw_path.expanduser().absolute()
        expanded[path] = None
        parent = path.parent
        while parent != parent.parent:
            try:
                parent.lstat()
            except FileNotFoundError:
                expanded[parent] = None
                parent = parent.parent
                continue
            break
    return tuple(expanded)


def load(profile: str) -> OperationJournal:
    """Load and validate the active journal for ``profile``."""
    path = journal_path(profile)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SetforgeError(f"no unfinished operation for profile {profile!r}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise SetforgeError(f"corrupt operation journal {path}: {exc}") from exc
    try:
        schema_version = raw["schema_version"]
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != JOURNAL_SCHEMA_VERSION
        ):
            raise ValueError(f"unsupported schema_version {schema_version!r}")
        journal = _from_json(raw)
    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise SetforgeError(f"invalid operation journal {path}: {exc}") from exc
    if journal.profile != profile:
        raise SetforgeError(
            f"operation journal profile mismatch: {journal.profile!r} != {profile!r}"
        )
    return journal


def active(profile: str) -> OperationJournal | None:
    """Return the active journal, or ``None`` without creating state."""
    if not journal_path(profile).exists():
        return None
    return load(profile)


def refuse_active(profile: str) -> None:
    """Fail when any unfinished mutation still owns recovery baselines."""
    journals = _load_all()
    if not journals:
        return
    journal = journals[0]
    raise SetforgeError(
        f"unfinished {journal.command} operation {journal.operation_id} blocks "
        "this mutation; run "
        f"`setforge recover --profile={journal.profile}`"
    )


def refuse_conflicting_mutation(
    *,
    resources: bool,
    config_dir: Path | None,
    profile: str | None,
    profiles: tuple[str, ...] = (),
    allow_operation_id: str | None = None,
) -> None:
    """Refuse a mutation whose locked namespaces overlap an active journal."""
    conflicts = conflicting_journals(
        resources=resources,
        config_dir=config_dir,
        profile=profile,
        profiles=profiles,
    )
    blocked = tuple(
        item for item in conflicts if item.operation_id != allow_operation_id
    )
    if blocked:
        journal = blocked[0]
        raise SetforgeError(
            f"unfinished {journal.command} operation {journal.operation_id} "
            "blocks this mutation; run "
            f"`setforge recover --profile={journal.profile}`"
        )


def conflicting_journals(
    *,
    resources: bool,
    config_dir: Path | None,
    profile: str | None,
    profiles: tuple[str, ...] = (),
) -> tuple[OperationJournal, ...]:
    """Return active journals overlapping the declared lock namespaces."""
    expected_config = config_dir.resolve() if config_dir is not None else None
    expected_profiles = {*profiles, *((profile,) if profile is not None else ())}
    return tuple(
        journal
        for journal in _load_all()
        if (resources and journal.resources_lock)
        or (expected_config is not None and journal.config_dir == expected_config)
        or bool(expected_profiles.intersection(locked_profiles(journal)))
    )


def refuse_config_mutation(config_dir: Path) -> None:
    """Refuse a config write covered by any unfinished operation journal."""
    expected = config_dir.resolve()
    for journal in _load_all():
        if journal.config_dir == expected:
            raise SetforgeError(
                f"unfinished {journal.command} operation {journal.operation_id} "
                "blocks this config mutation; run "
                f"`setforge recover --profile={journal.profile}`"
            )


def begin_checkpoint(
    journal: OperationJournal,
    *,
    name: str,
    kind: CheckpointKind,
    recovery: str,
    paths: tuple[Path, ...] | None = None,
    restore_state: bool | None = None,
    restore_transitions: bool = True,
    adapters: tuple[AdapterKind, ...] | None = None,
) -> OperationJournal:
    """Write effect intent durably before the effect begins."""
    if journal.checkpoints and not journal.checkpoints[-1].completed:
        raise SetforgeError(
            "cannot begin a checkpoint while the prior one is uncertain"
        )
    requested_paths = (
        tuple(str(item.path) for item in journal.paths)
        if paths is None
        else tuple(str(path.expanduser().absolute()) for path in paths)
    )
    requested_objects = tuple(Path(path) for path in requested_paths)
    scoped_paths = tuple(
        str(item.path)
        for item in journal.paths
        if str(item.path) in requested_paths
        or (
            item.kind is SnapshotKind.ABSENT
            and any(item.path in requested.parents for requested in requested_objects)
        )
    )
    available_paths = {str(item.path) for item in journal.paths}
    if not set(scoped_paths) <= available_paths:
        raise SetforgeError("checkpoint references a path absent from the journal")
    scoped_adapters = (
        tuple(item.kind for item in journal.adapters) if adapters is None else adapters
    )
    available_adapters = {item.kind for item in journal.adapters}
    if not set(scoped_adapters) <= available_adapters:
        raise SetforgeError("checkpoint references an adapter absent from the journal")
    updated = replace(
        journal,
        phase=OperationPhase.APPLYING,
        checkpoints=(
            *journal.checkpoints,
            OperationCheckpoint(
                name,
                kind,
                recovery,
                paths=scoped_paths,
                restore_state=bool(journal.state_snapshots)
                if restore_state is None
                else restore_state,
                restore_transitions=restore_transitions,
                adapters=scoped_adapters,
            ),
        ),
    )
    _write(updated)
    return updated


def finish_checkpoint(journal: OperationJournal) -> OperationJournal:
    """Durably mark the most recent checkpoint completed."""
    if not journal.checkpoints:
        raise SetforgeError("cannot finish an operation with no checkpoint")
    if journal.checkpoints[-1].completed:
        raise SetforgeError("cannot finish an already completed checkpoint")
    updated = replace(
        journal,
        checkpoints=(
            *journal.checkpoints[:-1],
            replace(journal.checkpoints[-1], completed=True),
        ),
    )
    _write(updated)
    return updated


def recover_files(journal: OperationJournal) -> OperationJournal:
    """Restore path/store snapshots and durably enter recovery state."""
    _require_matching_state_root(journal)
    recovering = replace(journal, phase=OperationPhase.RECOVERING)
    _write(recovering)
    scoped_paths = {path for item in recovering.checkpoints for path in item.paths}
    for snapshot in sorted(
        (item for item in recovering.paths if str(item.path) in scoped_paths),
        key=lambda item: len(item.path.parts),
        reverse=True,
    ):
        _restore_path(snapshot)
    if any(item.restore_state for item in recovering.checkpoints):
        transitions.restore_state_snapshots(recovering.state_snapshots)
    if any(item.restore_transitions for item in recovering.checkpoints):
        _remove_uncommitted_transition_records(recovering)
    return recovering


def finish_recovery(journal: OperationJournal) -> OperationJournal:
    """Durably mark every executable checkpoint compensated/restored."""
    updated = replace(
        journal,
        phase=OperationPhase.RECOVERING,
        checkpoints=tuple(
            replace(
                item,
                recovered=item.kind is not CheckpointKind.IRREVERSIBLE,
            )
            for item in journal.checkpoints
        ),
    )
    _write(updated)
    return updated


def has_irreversible_effect(journal: OperationJournal) -> bool:
    """Return whether recovery requires explicit operator remediation."""
    return any(item.kind is CheckpointKind.IRREVERSIBLE for item in journal.checkpoints)


def recover_automatically(journal: OperationJournal) -> bool:
    """Best-effort rollback after an in-process failure.

    Returns ``True`` when the journal was fully cleared. Any begun irreversible
    checkpoint keeps a MANUAL record after executable compensation: an
    incomplete checkpoint is uncertain, not proof that no package effect ran.
    """
    current = load(journal.profile)
    if current.operation_id != journal.operation_id:
        raise SetforgeError("operation journal changed before automatic recovery")
    validate_recovery(current)
    recover_adapters(current)
    recovered = finish_recovery(recover_files(current))
    if has_irreversible_effect(recovered):
        mark_manual(recovered)
        return False
    complete(recovered)
    return True


@contextmanager
def recover_on_error(profile: str, command: str) -> Iterator[None]:
    """Rollback this command's active journal while preserving its exception."""
    try:
        yield
    except BaseException as primary:
        try:
            journal = active(profile)
            if (
                journal is not None
                and journal.command == command
                and not recover_automatically(journal)
            ):
                primary.add_note(
                    "automatic compensation completed, but an irreversible "
                    "checkpoint requires `setforge recover` remediation"
                )
        except BaseException as recovery_error:
            primary.add_note(
                f"automatic recovery failed; the journal was retained: {recovery_error}"
            )
        raise


def recover_adapters(journal: OperationJournal) -> None:
    """Restore frozen adapter inventories in reverse application order."""
    scoped = {kind for item in journal.checkpoints for kind in item.adapters}
    for snapshot in reversed(
        tuple(item for item in journal.adapters if item.kind in scoped)
    ):
        payload = json.loads(snapshot.payload_json)
        match snapshot.kind:
            case AdapterKind.EXTENSIONS:
                _recover_extensions(payload)
            case AdapterKind.PLUGINS:
                _recover_plugins(payload)
            case AdapterKind.MCP:
                _recover_mcp(payload)


def validate_recovery(journal: OperationJournal) -> None:
    """Validate the recovery environment before the first compensating effect."""
    _require_matching_state_root(journal)
    for snapshot in journal.adapters:
        try:
            payload = json.loads(snapshot.payload_json)
        except json.JSONDecodeError as exc:
            raise SetforgeError("invalid adapter recovery baseline") from exc
        try:
            _validate_adapter_payload(snapshot.kind, payload)
        except (TypeError, ValueError) as exc:
            raise SetforgeError(f"invalid adapter recovery baseline: {exc}") from exc


def mark_manual(journal: OperationJournal) -> OperationJournal:
    """Persist that automatic recovery finished but manual work remains."""
    updated = replace(journal, phase=OperationPhase.MANUAL)
    _write(updated)
    return updated


def complete(journal: OperationJournal) -> None:
    """Durably remove the active record after transition/recovery commit."""
    target = journal_path(journal.profile)
    current = load(journal.profile)
    if current.operation_id != journal.operation_id:
        raise SetforgeError("operation journal identity changed before completion")
    with _registry_lock():
        current = load(journal.profile)
        if current.operation_id != journal.operation_id:
            raise SetforgeError("operation journal identity changed before completion")
        target.unlink()
        atomicio.fsync_dir(target.parent)


def _require_matching_state_root(journal: OperationJournal) -> None:
    current = transitions.state_root().resolve()
    if current != journal.state_dir:
        raise SetforgeError(
            "operation journal belongs to transition state root "
            f"{journal.state_dir}; set SETFORGE_STATE_DIR to that path and retry"
        )


def _restore_path(snapshot: PathSnapshot) -> None:
    path = snapshot.path
    if snapshot.kind is SnapshotKind.ABSENT:
        _restore_absent(path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if snapshot.kind is SnapshotKind.DIRECTORY:
        _restore_directory(snapshot)
        return
    _remove_replaceable(path)
    if snapshot.kind is SnapshotKind.FILE:
        assert snapshot.payload is not None
        mode = snapshot.mode if snapshot.mode is not None else 0o600
        atomicio.atomic_write_bytes(path, snapshot.payload, mode=mode)
        return
    if snapshot.kind is SnapshotKind.SYMLINK:
        assert snapshot.link_target is not None
        path.symlink_to(snapshot.link_target)
        return
    raise AssertionError(f"unhandled snapshot kind: {snapshot.kind}")


def _restore_absent(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        try:
            path.rmdir()
        except OSError as exc:
            raise SetforgeError(
                f"refusing to remove non-empty recovery directory {path}"
            ) from exc


def _restore_directory(snapshot: PathSnapshot) -> None:
    path = snapshot.path
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise SetforgeError(
            f"refusing to replace non-directory during recovery: {path}"
        )
    path.mkdir(exist_ok=True)
    if snapshot.mode is not None:
        path.chmod(snapshot.mode)


def _remove_replaceable(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        try:
            path.rmdir()
        except OSError as exc:
            raise SetforgeError(
                f"refusing to replace non-empty directory during recovery: {path}"
            ) from exc
    elif path.is_symlink() or path.exists():
        path.unlink()


def _write(journal: OperationJournal, *, create: bool = False) -> None:
    _from_json(_to_json(journal))
    target = journal_path(journal.profile)
    target.parent.mkdir(parents=True, exist_ok=True)
    if create and target.exists():
        raise SetforgeError(f"operation journal already exists: {target}")
    atomicio.atomic_write_text(
        target,
        json.dumps(_to_json(journal), indent=2, sort_keys=True) + "\n",
        mode=0o600,
    )
    atomicio.fsync_dir(target.parent)


def _b64(payload: bytes | None) -> str | None:
    return None if payload is None else base64.b64encode(payload).decode("ascii")


def _unb64(payload: object) -> bytes | None:
    if payload is None:
        return None
    if not isinstance(payload, str):
        raise TypeError("snapshot payload must be base64 text or null")
    return base64.b64decode(payload, validate=True)


def _to_json(journal: OperationJournal) -> dict[str, object]:
    return {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "operation_id": journal.operation_id,
        "command": journal.command,
        "profile": journal.profile,
        "config_dir": str(journal.config_dir) if journal.config_dir else None,
        "state_dir": str(journal.state_dir),
        "reserved_profiles": list(locked_profiles(journal)),
        "resources_lock": journal.resources_lock,
        "phase": journal.phase.value,
        "created_at": journal.created_at,
        "command_line": list(journal.command_line),
        "paths": [
            {
                "path": str(item.path),
                "kind": item.kind.value,
                "mode": item.mode,
                "payload": _b64(item.payload),
                "link_target": item.link_target,
            }
            for item in journal.paths
        ],
        "state_snapshots": [
            {
                "store": item.store.value,
                "profile": item.profile,
                "key": item.key,
                "payload": _b64(item.payload),
            }
            for item in journal.state_snapshots
        ],
        "adapters": [
            {"kind": item.kind.value, "payload_json": item.payload_json}
            for item in journal.adapters
        ],
        "checkpoints": [
            {
                "name": item.name,
                "kind": item.kind.value,
                "recovery": item.recovery,
                "paths": list(item.paths),
                "restore_state": item.restore_state,
                "restore_transitions": item.restore_transitions,
                "adapters": [kind.value for kind in item.adapters],
                "completed": item.completed,
                "recovered": item.recovered,
            }
            for item in journal.checkpoints
        ],
        "transition_names_before": list(journal.transition_names_before),
    }


def _from_json(raw: dict[str, object]) -> OperationJournal:  # noqa: C901
    profile = _require_str(raw, "profile")
    resources_lock = _require_bool(raw, "resources_lock")
    reserved_profiles = _require_str_tuple(raw, "reserved_profiles")
    path_rows = raw["paths"]
    state_rows = raw["state_snapshots"]
    checkpoint_rows = raw["checkpoints"]
    adapter_rows = raw.get("adapters", [])
    if not isinstance(path_rows, list) or not isinstance(state_rows, list):
        raise TypeError("paths/state_snapshots must be lists")
    if not isinstance(checkpoint_rows, list):
        raise TypeError("checkpoints must be a list")
    if not isinstance(adapter_rows, list):
        raise TypeError("adapters must be a list")
    for label, rows in (
        ("paths", path_rows),
        ("state_snapshots", state_rows),
        ("checkpoints", checkpoint_rows),
        ("adapters", adapter_rows),
    ):
        if not all(isinstance(row, dict) for row in rows):
            raise TypeError(f"{label} entries must be objects")
    paths = tuple(_parse_path_snapshot(row) for row in path_rows)
    _require_unique((str(item.path) for item in paths), "path snapshot")
    states = tuple(_parse_state_snapshot(row) for row in state_rows)
    required_profiles = {profile, *(item.profile for item in states)}
    if (
        not reserved_profiles
        or any(not item for item in reserved_profiles)
        or tuple(sorted(set(reserved_profiles))) != reserved_profiles
        or not required_profiles <= set(reserved_profiles)
    ):
        raise ValueError(
            "reserved_profiles must be sorted, unique, and cover journal state"
        )
    _require_unique(
        (
            str(
                transitions._snapshot_target(
                    item.store, item.profile, item.key
                ).resolve()
            )
            for item in states
        ),
        "state snapshot target",
    )
    adapters = tuple(_parse_adapter_snapshot(row) for row in adapter_rows)
    if adapters and not resources_lock:
        raise ValueError("adapter snapshots require the global resources lock")
    _require_unique((item.kind.value for item in adapters), "adapter snapshot")
    checkpoints = tuple(_parse_checkpoint(row) for row in checkpoint_rows)
    _require_unique((item.name for item in checkpoints), "checkpoint")
    if any(not item.completed for item in checkpoints[:-1]):
        raise ValueError("only the final checkpoint may be incomplete")
    available_paths = {str(item.path) for item in paths}
    available_adapters = {item.kind for item in adapters}
    for checkpoint in checkpoints:
        if not set(checkpoint.paths) <= available_paths:
            raise ValueError(f"checkpoint {checkpoint.name!r} has unknown paths")
        if not set(checkpoint.adapters) <= available_adapters:
            raise ValueError(f"checkpoint {checkpoint.name!r} has unknown adapters")
    config_raw = raw["config_dir"]
    if config_raw is not None and not isinstance(config_raw, str):
        raise TypeError("config_dir must be an absolute path or null")
    config_dir = Path(config_raw) if config_raw is not None else None
    state_dir = Path(_require_str(raw, "state_dir"))
    if config_dir is not None and not config_dir.is_absolute():
        raise ValueError("config_dir must be absolute")
    if not state_dir.is_absolute():
        raise ValueError("state_dir must be absolute")
    if config_dir is not None and config_dir != config_dir.resolve():
        raise ValueError("config_dir must be canonical")
    if state_dir != state_dir.resolve():
        raise ValueError("state_dir must be canonical")
    return OperationJournal(
        operation_id=_require_str(raw, "operation_id"),
        command=_require_str(raw, "command"),
        profile=profile,
        config_dir=config_dir,
        state_dir=state_dir,
        resources_lock=resources_lock,
        phase=OperationPhase(_require_str(raw, "phase")),
        created_at=_require_str(raw, "created_at"),
        command_line=_require_str_tuple(raw, "command_line"),
        paths=paths,
        state_snapshots=states,
        reserved_profiles=reserved_profiles,
        adapters=adapters,
        checkpoints=checkpoints,
        transition_names_before=_optional_str_tuple(raw, "transition_names_before"),
    )


def _parse_path_snapshot(  # noqa: C901
    row: dict[object, object],
) -> PathSnapshot:
    path_raw = row.get("path")
    if not isinstance(path_raw, str) or not Path(path_raw).is_absolute():
        raise ValueError("snapshot path must be absolute text")
    kind_raw = row.get("kind")
    if not isinstance(kind_raw, str):
        raise TypeError("snapshot kind must be text")
    kind = SnapshotKind(kind_raw)
    mode = row.get("mode")
    if mode is not None and (isinstance(mode, bool) or not isinstance(mode, int)):
        raise TypeError("snapshot mode must be an integer or null")
    if isinstance(mode, int) and not 0 <= mode <= 0o7777:
        raise ValueError("snapshot mode is outside the supported range")
    payload = _unb64(row.get("payload"))
    link_target = row.get("link_target")
    if link_target is not None and not isinstance(link_target, str):
        raise TypeError("snapshot link_target must be text or null")
    if kind is SnapshotKind.ABSENT:
        if mode is not None or payload is not None or link_target is not None:
            raise ValueError("absent snapshot cannot carry metadata")
    elif kind is SnapshotKind.FILE:
        if mode is None or payload is None or link_target is not None:
            raise ValueError("file snapshot requires mode/payload only")
    elif kind is SnapshotKind.SYMLINK:
        if mode is None or payload is not None or link_target is None:
            raise ValueError("symlink snapshot requires mode/link_target only")
    elif mode is None or payload is not None or link_target is not None:
        raise ValueError("directory snapshot requires mode only")
    return PathSnapshot(Path(path_raw), kind, mode, payload, link_target)


def _parse_state_snapshot(
    row: dict[object, object],
) -> transitions.StateSnapshotEntry:
    store_raw = row.get("store")
    profile = row.get("profile")
    key = row.get("key")
    if not isinstance(store_raw, str):
        raise TypeError("state snapshot store must be text")
    if not isinstance(profile, str) or not isinstance(key, str):
        raise TypeError("state snapshot profile/key must be text")
    for label, value in (("profile", profile), ("key", key)):
        component = Path(value)
        if (
            not value
            or not component.parts
            or component.is_absolute()
            or ".." in component.parts
        ):
            raise ValueError(
                f"state snapshot {label} must be non-empty relative text "
                "without '..' components"
            )
    store = transitions.SnapshotStore(store_raw)
    # Resolve once during decoding so every store-specific traversal guard runs
    # before recovery can perform any filesystem or adapter effect.
    try:
        transitions._snapshot_target(store, profile, key)
    except SetforgeError as exc:
        raise ValueError(f"unsafe state snapshot target: {exc}") from exc
    return transitions.StateSnapshotEntry(
        store=store,
        profile=profile,
        key=key,
        payload=_unb64(row.get("payload")),
    )


def _parse_adapter_snapshot(row: dict[object, object]) -> AdapterSnapshot:
    kind_raw = row.get("kind")
    payload_json = row.get("payload_json")
    if not isinstance(kind_raw, str) or not isinstance(payload_json, str):
        raise TypeError("adapter kind/payload_json must be text")
    snapshot = AdapterSnapshot(AdapterKind(kind_raw), payload_json)
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise ValueError("adapter payload_json is invalid JSON") from exc
    _validate_adapter_payload(snapshot.kind, payload)
    return snapshot


def _parse_checkpoint(row: dict[object, object]) -> OperationCheckpoint:
    name = row.get("name")
    kind_raw = row.get("kind")
    recovery = row.get("recovery")
    paths = row.get("paths")
    restore_state = row.get("restore_state")
    restore_transitions = row.get("restore_transitions")
    adapter_rows = row.get("adapters")
    completed = row.get("completed")
    recovered = row.get("recovered")
    if not isinstance(name, str) or not name:
        raise TypeError("checkpoint name must be non-empty text")
    if not isinstance(kind_raw, str) or not kind_raw:
        raise TypeError("checkpoint kind must be non-empty text")
    if not isinstance(recovery, str) or not recovery:
        raise TypeError("checkpoint name/kind/recovery must be non-empty text")
    if not isinstance(paths, list) or not all(isinstance(item, str) for item in paths):
        raise TypeError("checkpoint paths must be a list of strings")
    if not isinstance(adapter_rows, list) or not all(
        isinstance(item, str) for item in adapter_rows
    ):
        raise TypeError("checkpoint adapters must be a list of strings")
    if not isinstance(restore_state, bool):
        raise TypeError("checkpoint restore_state must be boolean")
    if not isinstance(restore_transitions, bool):
        raise TypeError("checkpoint restore_transitions must be boolean")
    if not isinstance(completed, bool) or not isinstance(recovered, bool):
        raise TypeError("checkpoint state flags must be booleans")
    _require_unique(iter(paths), "checkpoint path")
    _require_unique(iter(adapter_rows), "checkpoint adapter")
    return OperationCheckpoint(
        name=name,
        kind=CheckpointKind(kind_raw),
        recovery=recovery,
        paths=tuple(paths),
        restore_state=restore_state,
        restore_transitions=restore_transitions,
        adapters=tuple(AdapterKind(item) for item in adapter_rows),
        completed=completed,
        recovered=recovered,
    )


def _require_unique(values: Iterator[str], label: str) -> None:
    rows = tuple(values)
    if len(rows) != len(set(rows)):
        raise ValueError(f"duplicate {label}")


def _committed_transition_names() -> tuple[str, ...]:
    root = transitions.transitions_root()
    if not root.exists():
        return ()
    return tuple(
        sorted(
            path.name
            for path in root.iterdir()
            if path.is_dir()
            and not path.name.startswith(".pending-")
            and (path / "meta.json").is_file()
        )
    )


def _remove_uncommitted_transition_records(journal: OperationJournal) -> None:
    """Remove committed records written by an operation that is rolling back."""
    root = transitions.transitions_root()
    if not root.exists():
        return
    baseline = set(journal.transition_names_before)
    for path in root.iterdir():
        if path.name in baseline or not path.is_dir():
            continue
        meta_path = path / "meta.json"
        if not meta_path.is_file():
            continue
        try:
            meta = transitions.load_meta(transitions.TransitionDir(path))
        except SetforgeError:
            continue
        if meta.profile != journal.profile:
            continue
        shutil.rmtree(path)
        atomicio.fsync_dir(root)


def _recover_extensions(payload: object) -> None:
    from setforge import vscode_extensions

    if not isinstance(payload, list) or not all(
        isinstance(item, str) for item in payload
    ):
        raise SetforgeError("invalid extension recovery baseline")
    expected = set(payload)
    current = vscode_extensions.list_installed()
    for ext_id in sorted(current - expected):
        vscode_extensions.uninstall_one(ext_id)
    for ext_id in sorted(expected - current):
        vscode_extensions.install_one(ext_id)


def _validate_adapter_payload(kind: AdapterKind, payload: object) -> None:
    """Validate a complete adapter baseline before recovery can mutate."""
    if kind is AdapterKind.EXTENSIONS:
        if not isinstance(payload, list) or not all(
            isinstance(item, str) and item for item in payload
        ):
            raise ValueError("invalid extension recovery baseline")
        _require_unique(iter(payload), "extension recovery id")
        return
    if kind is AdapterKind.PLUGINS:
        _validate_plugin_payload(payload)
        return
    _validate_mcp_payload(payload)


def _validate_plugin_payload(payload: object) -> None:
    if not isinstance(payload, dict):
        raise ValueError("invalid plugin recovery baseline")
    plugins = payload.get("plugins")
    marketplaces = payload.get("marketplaces")
    if not isinstance(plugins, dict) or not isinstance(marketplaces, dict):
        raise ValueError("invalid plugin/marketplace recovery baseline")
    for name, row in marketplaces.items():
        if not isinstance(name, str) or not name or not isinstance(row, dict):
            raise ValueError("invalid marketplace recovery entry")
        _marketplace_source_identity(name, row)
    for plugin_id, row in plugins.items():
        name, separator, marketplace = (
            plugin_id.rpartition("@") if isinstance(plugin_id, str) else ("", "", "")
        )
        if (
            not isinstance(plugin_id, str)
            or not separator
            or not name
            or not marketplace
            or marketplace not in marketplaces
            or not isinstance(row, dict)
            or not isinstance(row.get("enabled"), bool)
        ):
            raise ValueError("invalid plugin recovery entry")


def _validate_mcp_payload(payload: object) -> None:
    if not isinstance(payload, list):
        raise ValueError("invalid MCP recovery baseline")
    names: list[str] = []
    for row in payload:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("name"), str)
            or not row.get("name")
        ):
            raise ValueError("invalid MCP recovery entry")
        names.append(row["name"])
        prior = row.get("prior")
        if prior is None:
            continue
        if not (
            isinstance(prior, list)
            and len(prior) == 2
            and isinstance(prior[0], list)
            and bool(prior[0])
            and all(isinstance(token, str) for token in prior[0])
            and bool(prior[0][0])
            and isinstance(prior[1], str)
        ):
            raise ValueError("invalid MCP prior registration")
        from setforge.config import McpScope

        McpScope(prior[1])
    _require_unique(iter(names), "MCP recovery name")


def _recover_plugins(payload: object) -> None:
    from setforge import claude_plugins

    if not isinstance(payload, dict):
        raise SetforgeError("invalid plugin recovery baseline")
    plugins = payload.get("plugins")
    marketplaces = payload.get("marketplaces")
    if not isinstance(plugins, dict) or not isinstance(marketplaces, dict):
        raise SetforgeError("invalid plugin/marketplace recovery baseline")
    typed_marketplaces = cast(dict[str, dict[str, object]], marketplaces)
    current = claude_plugins.list_installed()
    expected_ids = set(plugins)
    drifted_marketplaces = _drifted_marketplace_names(
        typed_marketplaces, claude_plugins.list_marketplaces()
    )
    source_drift_dependents = {
        plugin_id
        for plugin_id in expected_ids & set(current)
        if plugin_id.rpartition("@")[2] in drifted_marketplaces
    }
    for plugin_id in sorted((set(current) - expected_ids) | source_drift_dependents):
        claude_plugins.plugin_uninstall(plugin_id)
    _recover_marketplaces(marketplaces)
    current = claude_plugins.list_installed()
    for plugin_id in sorted(expected_ids - set(current)):
        name, separator, marketplace = plugin_id.rpartition("@")
        if not separator:
            raise SetforgeError(f"invalid plugin recovery id: {plugin_id!r}")
        claude_plugins.plugin_install(name, marketplace)
    refreshed = claude_plugins.list_installed()
    for plugin_id, row in plugins.items():
        if not isinstance(plugin_id, str) or not isinstance(row, dict):
            raise SetforgeError("invalid plugin recovery entry")
        _restore_plugin_enabled(plugin_id, row, refreshed.get(plugin_id, {}))


def _recover_marketplaces(marketplaces: dict[object, object]) -> None:
    from setforge import claude_plugins
    from setforge.config import MarketplaceSource, MarketplaceSourceKind

    typed = cast(dict[str, dict[str, object]], marketplaces)
    current = claude_plugins.list_marketplaces()
    drifted = _drifted_marketplace_names(typed, current)
    for name in sorted((set(current) - set(typed)) | drifted):
        claude_plugins.marketplace_remove(name)
    for name in sorted((set(typed) - set(current)) | drifted):
        row = typed[name]
        source_kind, source_value = _marketplace_source_identity(name, row)
        if source_kind == "github":
            source = MarketplaceSource(
                source=MarketplaceSourceKind.GITHUB,
                repo=source_value,
            )
        else:
            source = MarketplaceSource(
                source=MarketplaceSourceKind.PATH,
                path=Path(source_value),
            )
        claude_plugins.marketplace_add(name, source)


def _drifted_marketplace_names(
    expected: Mapping[str, dict[str, object]],
    current: Mapping[str, dict[str, object]],
) -> set[str]:
    """Return shared marketplace names whose normalized sources differ."""
    return {
        name
        for name in set(current) & set(expected)
        if _marketplace_source_identity(name, current[name])
        != _marketplace_source_identity(name, expected[name])
    }


def _marketplace_source_identity(
    name: str, row: Mapping[str, object]
) -> tuple[str, str]:
    """Normalize Claude's accepted marketplace source JSON representations."""
    raw_source = row.get("source")
    repo = row.get("repo")
    if raw_source == "github" and isinstance(repo, str) and repo:
        return "github", repo
    if isinstance(raw_source, str) and raw_source.startswith("github:"):
        value = raw_source.removeprefix("github:")
        if value:
            return "github", value
    if isinstance(raw_source, str) and raw_source.startswith("path:"):
        value = raw_source.removeprefix("path:")
        if value:
            return "path", value
    if isinstance(raw_source, str) and raw_source:
        if Path(raw_source).is_absolute():
            return "path", raw_source
        if "/" in raw_source:
            return "github", raw_source
    raise ValueError(f"marketplace {name!r} has no recoverable source identity")


def _restore_plugin_enabled(
    plugin_id: str,
    expected: dict[str, object],
    current: dict[str, object],
) -> None:
    from setforge import claude_plugins

    expected_enabled = expected.get("enabled") is True
    current_enabled = current.get("enabled") is True
    if expected_enabled and not current_enabled:
        claude_plugins.plugin_enable(plugin_id)
    elif current_enabled and not expected_enabled:
        claude_plugins.plugin_disable(plugin_id)


def _recover_mcp(payload: object) -> None:
    from setforge import mcp_servers
    from setforge.config import McpScope, McpServerRef

    if not isinstance(payload, list):
        raise SetforgeError("invalid MCP recovery baseline")
    for row in payload:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise SetforgeError("invalid MCP recovery entry")
        name = row["name"]
        prior = row.get("prior")
        current = mcp_servers.mcp_get_command(name)
        if current is not None:
            mcp_servers.mcp_remove(name, scope=current[1])
        if prior is None:
            continue
        if not (
            isinstance(prior, list)
            and len(prior) == 2
            and isinstance(prior[0], list)
            and all(isinstance(token, str) for token in prior[0])
            and isinstance(prior[1], str)
        ):
            raise SetforgeError("invalid MCP prior registration")
        mcp_servers.mcp_add(
            name,
            McpServerRef(command=prior[0], scope=McpScope(prior[1])),
        )


def _require_str(raw: dict[str, object], key: str) -> str:
    value = raw[key]
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    return value


def _require_bool(raw: dict[str, object], key: str) -> bool:
    value = raw[key]
    if not isinstance(value, bool):
        raise TypeError(f"{key} must be a boolean")
    return value


def _require_str_tuple(raw: dict[str, object], key: str) -> tuple[str, ...]:
    value = raw[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"{key} must be a list of strings")
    return tuple(value)


def _optional_str_tuple(raw: dict[str, object], key: str) -> tuple[str, ...]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"{key} must be a list of strings")
    return tuple(value)
