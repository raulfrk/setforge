"""Unit and recovery-contract tests for write-ahead operation journals."""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from setforge import codex_plugins, operations, transitions
from setforge.errors import SetforgeError
from setforge.locking import install_resources_lock
from setforge.ownership import (
    OwnershipClaim,
    OwnershipStore,
    ProvenanceFact,
    ProvenanceFactKind,
    ResourceId,
    ResourceScope,
    ScopeKind,
)


@pytest.fixture
def operation_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "state"
    monkeypatch.setattr(transitions, "state_root", lambda: root)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return root


def _prepare(
    tmp_path: Path, *, paths: tuple[Path, ...] = ()
) -> operations.OperationJournal:
    return operations.prepare(
        command="install",
        profile="p",
        config_dir=tmp_path,
        resources_lock=True,
        command_line=("install", "--profile=p"),
        paths=paths,
    )


def test_codex_recovery_rejects_unsafe_source_before_native_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        codex_plugins,
        "list_installed",
        lambda: pytest.fail("invalid recovery baseline reached native state"),
    )
    payload = {
        "plugins": ["review@official"],
        "marketplaces": [
            [
                "official",
                json.dumps(
                    {
                        "source": "github",
                        "repo": "https://token@github.com/owner/repo",
                    }
                ),
            ]
        ],
    }
    with pytest.raises(SetforgeError, match="credential-free owner/repo"):
        operations._recover_codex_plugins(payload)


def _path_guards(path: Path) -> tuple[operations.PathGuard, ...]:
    guards = []
    for ancestor in path.parents:
        if ancestor == Path("/"):
            continue
        info = ancestor.stat()
        guards.append(
            operations.PathGuard(ancestor, info.st_dev, info.st_ino, info.st_mode)
        )
    return tuple(guards)


def test_prepare_round_trips_exact_path_and_store_state(
    tmp_path: Path, operation_state: Path
) -> None:
    file_path = tmp_path / "live.txt"
    file_path.write_bytes(b"before\x00\n")
    file_path.chmod(0o640)
    file_mtime_ns = 1_700_000_000_123_456_789
    os.utime(file_path, ns=(file_mtime_ns, file_mtime_ns))
    link_path = tmp_path / "link"
    link_path.symlink_to("live.txt")
    directory = tmp_path / "directory"
    directory.mkdir(mode=0o750)
    absent = tmp_path / "absent"
    state = transitions.StateSnapshotEntry(
        store=transitions.SnapshotStore.BASE,
        profile="p",
        key="doc",
        payload=b"base\x00",
    )
    journal = operations.prepare(
        command="install",
        profile="p",
        config_dir=tmp_path,
        resources_lock=True,
        command_line=("install", "--profile=p"),
        paths=(file_path, link_path, directory, absent),
        state_snapshots=(state,),
    )

    assert operations.load("p") == journal
    assert journal.paths[0].mtime_ns == file_mtime_ns
    assert operations.journal_path("p").stat().st_mode & 0o777 == 0o600
    assert tmp_path / "home" / ".cache" / "setforge" / "operations" in (
        operations.journal_path("p").parents
    )


@settings(
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    payload=st.binary(max_size=4096),
    mode=st.sampled_from([0o600, 0o640, 0o755]),
)
def test_journal_round_trips_arbitrary_binary_file_payload(
    tmp_path: Path,
    operation_state: Path,
    payload: bytes,
    mode: int,
) -> None:
    path = tmp_path / "payload"
    path.write_bytes(payload)
    path.chmod(mode)

    journal = _prepare(tmp_path, paths=(path,))

    assert operations.load("p") == journal
    assert journal.paths[0].payload == payload
    assert journal.paths[0].mode == mode
    operations.complete(journal)


def test_prepare_refuses_to_shadow_active_operation(
    tmp_path: Path, operation_state: Path
) -> None:
    _prepare(tmp_path)
    with pytest.raises(SetforgeError, match=r"unfinished install operation.*recover"):
        _prepare(tmp_path)


def test_active_operation_blocks_same_config_but_not_other_repo(
    tmp_path: Path, operation_state: Path
) -> None:
    _prepare(tmp_path)

    with pytest.raises(SetforgeError, match="blocks this config mutation"):
        operations.refuse_config_mutation(tmp_path)

    operations.refuse_config_mutation(tmp_path / "other")


def test_checkpoint_intent_is_durable_before_completion(
    tmp_path: Path, operation_state: Path
) -> None:
    journal = _prepare(tmp_path)
    applying = operations.begin_checkpoint(
        journal,
        name="tracked-files",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore captured paths",
    )
    assert operations.load("p") == applying
    assert not applying.checkpoints[-1].completed

    completed = operations.finish_checkpoint(applying)
    assert operations.load("p") == completed
    assert completed.checkpoints[-1].completed


def test_extend_paths_snapshots_late_identity_before_publication(
    tmp_path: Path, operation_state: Path
) -> None:
    journal = _prepare(tmp_path)
    first = operations.begin_checkpoint(
        journal,
        name="create-target",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore target",
    )
    completed = operations.finish_checkpoint(first)
    late = tmp_path / "late-claim.json"

    extended = operations.extend_paths(completed, (late,))
    applying = operations.begin_checkpoint(
        extended,
        name="publish-claim",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="remove claim",
        paths=(late,),
    )
    late.write_text("claim", encoding="utf-8")

    operations.recover_files(applying)

    assert not late.exists()


def test_recover_files_resolves_ownership_move_before_restoring_claims(
    tmp_path: Path,
    operation_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = OwnershipStore()
    owner = uuid.uuid4()
    scope = ResourceScope(ScopeKind.USER_HOST, "current-user")
    source = ResourceId("package", "cargo", "source", scope)
    destination = ResourceId("package", "cargo", "destination", scope)
    with install_resources_lock():
        claim = store.claim_locked(
            resource_id=source,
            owner_id=owner,
            declaration_refs=("packages.cargo.source",),
            provenance=(ProvenanceFact(ProvenanceFactKind.ORIGIN, "test"),),
            locator="source",
            fingerprint="before",
            expected_generation=None,
        )
    journal = _prepare(
        tmp_path,
        paths=(store.claim_path(source), store.claim_path(destination)),
    )
    applying = operations.begin_checkpoint(
        journal,
        name="move-claim",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore claim identity",
    )
    real_unlink = store._unlink_intent

    def crash_after_move(
        intent_id: uuid.UUID, *, directory_fd: int | None = None
    ) -> None:
        raise OSError("crash after move")

    monkeypatch.setattr(store, "_unlink_intent", crash_after_move)
    with install_resources_lock(), pytest.raises(OSError, match="crash after move"):
        store.move_locked(
            source,
            destination,
            expected_owner=owner,
            expected_generation=claim.generation,
        )
    monkeypatch.setattr(store, "_unlink_intent", real_unlink)

    with install_resources_lock():
        operations.recover_files(applying)

    restored = store.read(source)
    assert isinstance(restored, OwnershipClaim)
    assert store.read(destination) is None
    assert not tuple(store.intents_root.glob("*.json"))


def test_recover_files_restores_file_symlink_directory_and_absence(
    tmp_path: Path, operation_state: Path
) -> None:
    file_path = tmp_path / "file"
    file_path.write_text("before", encoding="utf-8")
    file_path.chmod(0o640)
    target = tmp_path / "target"
    target.write_text("target", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to("target")
    directory = tmp_path / "kept-dir"
    directory.mkdir(mode=0o750)
    directory.chmod(0o750)
    file_mtime_ns = 1_700_000_000_111_111_111
    link_mtime_ns = 1_700_000_000_222_222_222
    directory_mtime_ns = 1_700_000_000_333_333_333
    os.utime(file_path, ns=(file_mtime_ns, file_mtime_ns))
    os.utime(link, ns=(link_mtime_ns, link_mtime_ns), follow_symlinks=False)
    os.utime(directory, ns=(directory_mtime_ns, directory_mtime_ns))
    created = tmp_path / "created"
    journal = operations.begin_checkpoint(
        _prepare(tmp_path, paths=(file_path, link, directory, created)),
        name="files",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore files",
    )

    file_path.write_text("after", encoding="utf-8")
    file_path.chmod(0o600)
    link.unlink()
    link.symlink_to("elsewhere")
    directory.chmod(0o700)
    created.write_text("new", encoding="utf-8")

    recovered = operations.recover_files(journal)

    assert file_path.read_text(encoding="utf-8") == "before"
    assert file_path.stat().st_mode & 0o777 == 0o640
    assert file_path.stat().st_mtime_ns == file_mtime_ns
    assert link.readlink() == Path("target")
    assert link.lstat().st_mtime_ns == link_mtime_ns
    assert directory.stat().st_mode & 0o777 == 0o750
    assert directory.stat().st_mtime_ns == directory_mtime_ns
    assert not created.exists()
    assert recovered.phase is operations.OperationPhase.RECOVERING
    operations.complete(recovered)
    assert operations.active("p") is None


def test_recovery_round_trips_pre_epoch_mtime(
    tmp_path: Path, operation_state: Path
) -> None:
    path = tmp_path / "pre-epoch"
    path.write_text("before", encoding="utf-8")
    expected_mtime_ns = -1_000_000_000
    os.utime(path, ns=(expected_mtime_ns, expected_mtime_ns))
    journal = operations.begin_checkpoint(
        _prepare(tmp_path, paths=(path,)),
        name="files",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore files",
    )
    path.write_text("after", encoding="utf-8")

    operations.recover_files(journal)

    assert path.read_text(encoding="utf-8") == "before"
    assert path.stat().st_mtime_ns == expected_mtime_ns


def test_snapshot_restore_recovery_refuses_retargeted_parent_symlink(
    tmp_path: Path, operation_state: Path
) -> None:
    parent = tmp_path / "live"
    parent.mkdir()
    path = parent / "file"
    path.write_text("before", encoding="utf-8")
    journal = operations.prepare(
        command="snapshot restore",
        profile="p",
        config_dir=tmp_path,
        resources_lock=False,
        command_line=("snapshot", "restore"),
        paths=(path,),
        path_guards=_path_guards(path),
    )
    journal = operations.begin_checkpoint(
        journal,
        name="restore-files",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore files",
    )
    path.write_text("restored snapshot", encoding="utf-8")
    original_parent = tmp_path / "original-live"
    parent.rename(original_parent)
    external = tmp_path / "external"
    external.mkdir()
    parent.symlink_to(external, target_is_directory=True)

    with pytest.raises(SetforgeError, match="journaled path parent changed"):
        operations.recover_files(journal)

    assert not (external / "file").exists()
    assert (original_parent / "file").read_text(encoding="utf-8") == (
        "restored snapshot"
    )
    assert operations.active("p") is not None


def test_snapshot_restore_recovery_refuses_replaced_parent_directory(
    tmp_path: Path, operation_state: Path
) -> None:
    parent = tmp_path / "live"
    parent.mkdir()
    path = parent / "file"
    path.write_text("before", encoding="utf-8")
    journal = operations.prepare(
        command="snapshot restore",
        profile="p",
        config_dir=tmp_path,
        resources_lock=False,
        command_line=("snapshot", "restore"),
        paths=(path,),
        path_guards=_path_guards(path),
    )
    journal = operations.begin_checkpoint(
        journal,
        name="restore-files",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore files",
    )
    path.write_text("restored snapshot", encoding="utf-8")
    original_parent = tmp_path / "original-live"
    parent.rename(original_parent)
    parent.mkdir()
    path.write_text("unrelated replacement", encoding="utf-8")

    with pytest.raises(SetforgeError, match="journaled path parent changed"):
        operations.recover_files(journal)

    assert path.read_text(encoding="utf-8") == "unrelated replacement"
    assert (original_parent / "file").read_text(encoding="utf-8") == (
        "restored snapshot"
    )
    assert operations.active("p") is not None


def test_prepare_round_trips_config_reservations_and_path_guards(
    tmp_path: Path, operation_state: Path
) -> None:
    path = tmp_path / ".config" / "setforge" / "local.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("before", encoding="utf-8")
    extra_config = path.parent
    guards = _path_guards(path)

    journal = operations.prepare(
        command="snapshot restore",
        profile="p",
        config_dir=tmp_path,
        config_dirs=(extra_config,),
        resources_lock=False,
        command_line=("snapshot", "restore"),
        paths=(path,),
        path_guards=guards,
    )
    loaded = operations.load("p")

    assert loaded == journal
    assert operations.locked_config_dirs(loaded) == tuple(
        sorted((tmp_path.resolve(), extra_config.resolve()), key=str)
    )
    assert loaded.path_guards == tuple(sorted(guards, key=lambda item: str(item.path)))
    assert operations.conflicting_journals(
        resources=False,
        config_dir=extra_config,
        profile=None,
    ) == (loaded,)

    journal_path = operations.journal_path("p")
    raw = json.loads(journal_path.read_text(encoding="utf-8"))
    raw["reserved_config_dirs"] = [str(tmp_path.resolve())]
    journal_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(SetforgeError, match="invalid operation journal"):
        operations.load("p")


def test_snapshot_restore_allows_tracked_path_with_local_config_suffix(
    tmp_path: Path, operation_state: Path
) -> None:
    path = tmp_path / "project" / ".config" / "setforge" / "local.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("tracked", encoding="utf-8")

    journal = operations.prepare(
        command="snapshot restore",
        profile="p",
        config_dir=tmp_path,
        resources_lock=False,
        command_line=("snapshot", "restore"),
        paths=(path,),
        path_guards=_path_guards(path),
    )

    assert operations.load("p") == journal
    assert operations.locked_config_dirs(journal) == (tmp_path.resolve(),)


def test_schema_one_legacy_journal_fields_remain_recoverable(
    tmp_path: Path, operation_state: Path
) -> None:
    path = tmp_path / "file"
    path.write_text("before", encoding="utf-8")
    journal = operations.begin_checkpoint(
        _prepare(tmp_path, paths=(path,)),
        name="files",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore files",
    )
    journal_path = operations.journal_path("p")
    raw = json.loads(journal_path.read_text(encoding="utf-8"))
    raw.pop("path_guards")
    raw.pop("reserved_config_dirs")
    raw.pop("reserved_config_dirs_digest")
    for row in raw["paths"]:
        row.pop("mtime_ns")
    journal_path.write_text(json.dumps(raw), encoding="utf-8")
    path.write_text("after", encoding="utf-8")

    loaded = operations.load("p")
    operations.recover_files(loaded)

    assert loaded.operation_id == journal.operation_id
    assert loaded.path_guards == ()
    assert operations.locked_config_dirs(loaded) == (tmp_path.resolve(),)
    assert path.read_text(encoding="utf-8") == "before"


def test_snapshot_restore_journal_cannot_drop_path_guards(
    tmp_path: Path, operation_state: Path
) -> None:
    path = tmp_path / "live" / "file"
    path.parent.mkdir()
    path.write_text("before", encoding="utf-8")
    operations.prepare(
        command="snapshot restore",
        profile="p",
        config_dir=tmp_path,
        resources_lock=False,
        command_line=("snapshot", "restore"),
        paths=(path,),
        path_guards=_path_guards(path),
    )
    journal_path = operations.journal_path("p")
    raw = json.loads(journal_path.read_text(encoding="utf-8"))
    raw.pop("path_guards")
    journal_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(SetforgeError, match="invalid operation journal"):
        operations.load("p")


def test_snapshot_restore_journal_cannot_drop_config_reservations(
    tmp_path: Path, operation_state: Path
) -> None:
    path = tmp_path / "live" / "file"
    path.parent.mkdir()
    path.write_text("before", encoding="utf-8")
    operations.prepare(
        command="snapshot restore",
        profile="p",
        config_dir=tmp_path,
        resources_lock=False,
        command_line=("snapshot", "restore"),
        paths=(path,),
        path_guards=_path_guards(path),
    )
    journal_path = operations.journal_path("p")
    raw = json.loads(journal_path.read_text(encoding="utf-8"))
    raw.pop("reserved_config_dirs")
    journal_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(SetforgeError, match="invalid operation journal"):
        operations.load("p")


def test_recovery_refuses_parent_swap_after_preflight(
    tmp_path: Path,
    operation_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "live"
    parent.mkdir()
    path = parent / "file"
    path.write_text("before", encoding="utf-8")
    journal = operations.begin_checkpoint(
        operations.prepare(
            command="snapshot restore",
            profile="p",
            config_dir=tmp_path,
            resources_lock=False,
            command_line=("snapshot", "restore"),
            paths=(path,),
            path_guards=_path_guards(path),
        ),
        name="restore-files",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore files",
    )
    path.write_text("snapshot-applied", encoding="utf-8")
    real_validate = operations._validate_snapshot_restore_parents

    def validate_then_swap(candidate: operations.OperationJournal) -> None:
        real_validate(candidate)
        parent.rename(tmp_path / "original-live")
        parent.mkdir()
        path.write_text("unrelated replacement", encoding="utf-8")

    monkeypatch.setattr(
        operations, "_validate_snapshot_restore_parents", validate_then_swap
    )

    with pytest.raises(SetforgeError, match="parent changed before write"):
        operations.recover_files(journal)

    assert path.read_text(encoding="utf-8") == "unrelated replacement"
    assert (tmp_path / "original-live" / "file").read_text(encoding="utf-8") == (
        "snapshot-applied"
    )


@pytest.mark.parametrize(
    "kind", [operations.SnapshotKind.FILE, operations.SnapshotKind.SYMLINK]
)
def test_anchored_restore_refuses_parent_swap_after_descriptor_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: operations.SnapshotKind,
) -> None:
    parent = tmp_path / "live"
    parent.mkdir()
    path = parent / "file"
    path.write_text("unrelated original", encoding="utf-8")
    identities = operations._guard_identities(_path_guards(path))
    real_verify = operations._verify_parent_binding
    swapped = False

    def swap_then_verify(parent_fd: int, lexical_parent: Path) -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            parent.rename(tmp_path / "original-live")
            parent.mkdir()
            path.write_text("unrelated replacement", encoding="utf-8")
        real_verify(parent_fd, lexical_parent)

    monkeypatch.setattr(operations, "_verify_parent_binding", swap_then_verify)
    snapshot = operations.PathSnapshot(
        path=path,
        kind=kind,
        mode=0o600 if kind is operations.SnapshotKind.FILE else 0o777,
        payload=b"restored" if kind is operations.SnapshotKind.FILE else None,
        link_target="target" if kind is operations.SnapshotKind.SYMLINK else None,
    )

    with pytest.raises(SetforgeError, match="parent changed before write"):
        operations._restore_path(snapshot, guard_identities=identities)

    assert path.read_text(encoding="utf-8") == "unrelated replacement"
    assert (tmp_path / "original-live" / "file").read_text(encoding="utf-8") == (
        "unrelated original"
    )


@pytest.mark.parametrize(
    "kind", [operations.SnapshotKind.FILE, operations.SnapshotKind.SYMLINK]
)
def test_anchored_restore_refuses_parent_swap_during_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: operations.SnapshotKind,
) -> None:
    parent = tmp_path / "live"
    parent.mkdir()
    path = parent / "file"
    path.write_text("before", encoding="utf-8")
    identities = operations._guard_identities(_path_guards(path))
    real_remove = operations._remove_replaceable_at
    swapped = False

    def remove_then_swap(parent_fd: int, name: str, destination: Path) -> None:
        nonlocal swapped
        real_remove(parent_fd, name, destination)
        if not swapped:
            swapped = True
            parent.rename(tmp_path / "moved-live")
            parent.mkdir()
            path.write_text("unrelated replacement", encoding="utf-8")

    monkeypatch.setattr(operations, "_remove_replaceable_at", remove_then_swap)
    snapshot = operations.PathSnapshot(
        path=path,
        kind=kind,
        mode=0o600 if kind is operations.SnapshotKind.FILE else 0o777,
        payload=b"restored" if kind is operations.SnapshotKind.FILE else None,
        link_target="target" if kind is operations.SnapshotKind.SYMLINK else None,
    )

    with pytest.raises(SetforgeError, match="parent changed before write"):
        operations._restore_path(snapshot, guard_identities=identities)

    assert path.read_text(encoding="utf-8") == "unrelated replacement"
    moved = tmp_path / "moved-live" / "file"
    if kind is operations.SnapshotKind.FILE:
        assert moved.read_bytes() == b"restored"
    else:
        assert moved.readlink() == Path("target")


@pytest.mark.parametrize(
    "kind", [operations.SnapshotKind.ABSENT, operations.SnapshotKind.DIRECTORY]
)
def test_anchored_recovery_kind_refuses_parent_swap_during_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: operations.SnapshotKind,
) -> None:
    parent = tmp_path / "live"
    parent.mkdir()
    path = parent / "entry"
    if kind is operations.SnapshotKind.ABSENT:
        path.write_text("operation-created", encoding="utf-8")
    else:
        path.mkdir()
    identities = operations._guard_identities(_path_guards(path))
    real_fsync = operations.os.fsync
    swapped = False

    def fsync_then_swap(descriptor: int) -> None:
        nonlocal swapped
        real_fsync(descriptor)
        if not swapped:
            swapped = True
            parent.rename(tmp_path / "moved-live")
            parent.mkdir()
            (parent / "unrelated").write_text("replacement", encoding="utf-8")

    monkeypatch.setattr(operations.os, "fsync", fsync_then_swap)
    snapshot = operations.PathSnapshot(
        path=path,
        kind=kind,
        mode=0o700 if kind is operations.SnapshotKind.DIRECTORY else None,
    )

    with pytest.raises(SetforgeError, match="parent changed before write"):
        operations._restore_path(
            snapshot,
            guard_identities=identities,
            permit_existing_absent=True,
        )

    assert (parent / "unrelated").read_text(encoding="utf-8") == "replacement"
    moved = tmp_path / "moved-live" / "entry"
    if kind is operations.SnapshotKind.ABSENT:
        assert not moved.exists()
    else:
        assert moved.is_dir()


@pytest.mark.parametrize("permit_existing_absent", [False, True])
def test_anchored_restore_never_recreates_expected_existing_parent(
    tmp_path: Path,
    permit_existing_absent: bool,
) -> None:
    parent = tmp_path / "live"
    parent.mkdir()
    path = parent / "file"
    path.write_text("before", encoding="utf-8")
    identities = operations._guard_identities(_path_guards(path))
    parent.rename(tmp_path / "moved-live")

    with pytest.raises(SetforgeError, match="parent changed before write"):
        operations._restore_path(
            operations.PathSnapshot(
                path=path,
                kind=operations.SnapshotKind.FILE,
                mode=0o600,
                payload=b"restored",
            ),
            guard_identities=identities,
            permit_existing_absent=permit_existing_absent,
        )

    assert not parent.exists()
    assert (tmp_path / "moved-live" / "file").read_text(encoding="utf-8") == ("before")


def test_anchored_restore_normalizes_failure_opening_created_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "new-parent"
    path = parent / "file"
    identities = operations._guard_identities(
        (
            *_path_guards(tmp_path / "placeholder"),
            operations.PathGuard(parent, None, None, None),
        )
    )
    real_open = operations.os.open
    parent_opens = 0

    def fail_second_parent_open(
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal parent_opens
        if target == parent.name and dir_fd is not None:
            parent_opens += 1
            if parent_opens == 2:
                raise PermissionError("simulated post-mkdir open failure")
        return real_open(target, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(operations.os, "open", fail_second_parent_open)

    with pytest.raises(SetforgeError, match="parent changed before write"):
        operations._restore_path(
            operations.PathSnapshot(
                path=path,
                kind=operations.SnapshotKind.FILE,
                mode=0o600,
                payload=b"restored",
            ),
            guard_identities=identities,
        )

    assert parent_opens == 2
    assert not path.exists()


def test_anchored_restore_closes_created_parent_when_fstat_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "new-parent"
    path = parent / "file"
    identities = operations._guard_identities(
        (
            *_path_guards(tmp_path / "placeholder"),
            operations.PathGuard(parent, None, None, None),
        )
    )
    real_open = operations.os.open
    real_fstat = operations.os.fstat
    created_parent_fd: int | None = None

    def record_created_parent_open(
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal created_parent_fd
        descriptor = real_open(target, flags, mode, dir_fd=dir_fd)
        if target == parent.name and dir_fd is not None:
            created_parent_fd = descriptor
        return descriptor

    def fail_created_parent_fstat(descriptor: int) -> os.stat_result:
        if descriptor == created_parent_fd:
            raise OSError("simulated post-mkdir fstat failure")
        return real_fstat(descriptor)

    monkeypatch.setattr(operations.os, "open", record_created_parent_open)
    monkeypatch.setattr(operations.os, "fstat", fail_created_parent_fstat)

    with pytest.raises(SetforgeError, match="parent changed before write"):
        operations._restore_path(
            operations.PathSnapshot(
                path=path,
                kind=operations.SnapshotKind.FILE,
                mode=0o600,
                payload=b"restored",
            ),
            guard_identities=identities,
        )

    assert created_parent_fd is not None
    with pytest.raises(OSError, match="Bad file descriptor"):
        real_fstat(created_parent_fd)


def test_recovery_refuses_nonempty_created_directory(
    tmp_path: Path, operation_state: Path
) -> None:
    created = tmp_path / "created"
    journal = operations.begin_checkpoint(
        _prepare(tmp_path, paths=(created,)),
        name="files",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore files",
    )
    created.mkdir()
    (created / "unknown").write_text("user", encoding="utf-8")

    with pytest.raises(SetforgeError, match="non-empty recovery directory"):
        operations.recover_files(journal)
    assert operations.active("p") is not None


def test_recovery_restores_mode_zero_exactly(
    tmp_path: Path, operation_state: Path
) -> None:
    path = tmp_path / "private"
    path.write_bytes(b"after")
    operations._restore_path(
        operations.PathSnapshot(
            path=path,
            kind=operations.SnapshotKind.FILE,
            mode=0o000,
            payload=b"before",
        )
    )

    assert path.stat().st_mode & 0o777 == 0o000
    path.chmod(0o600)
    assert path.read_bytes() == b"before"


def test_recovery_removes_missing_parent_directories_created_by_writer(
    tmp_path: Path, operation_state: Path
) -> None:
    parent = tmp_path / "new-parent" / "nested"
    created = parent / "file"
    journal = operations.begin_checkpoint(
        _prepare(tmp_path, paths=(created,)),
        name="files",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore files",
        paths=(created,),
    )
    parent.mkdir(parents=True)
    created.write_text("new", encoding="utf-8")

    operations.recover_files(journal)

    assert not (tmp_path / "new-parent").exists()


def test_snapshot_refuses_atomic_replacement_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "live"
    path.write_text("old", encoding="utf-8")
    replacement = tmp_path / "replacement"
    replacement.write_text("new", encoding="utf-8")
    real_open = operations.os.open

    def replace_then_open(target: Path, flags: int) -> int:
        replacement.replace(path)
        return real_open(target, flags)

    monkeypatch.setattr(operations.os, "open", replace_then_open)

    with pytest.raises(SetforgeError, match="changed while snapshotting"):
        operations.snapshot_path(path)


def test_snapshot_stat_detects_same_size_write_with_restored_mtime(
    tmp_path: Path,
) -> None:
    path = tmp_path / "live"
    path.write_bytes(b"old")
    before = path.stat()
    time.sleep(0.01)
    path.write_bytes(b"new")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

    assert not operations._same_snapshot_stat(before, path.stat())


def test_prepare_refuses_ancestor_topology_change_during_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "new-parent" / "file"
    calls = 0
    real_inventory = operations._paths_with_missing_ancestors

    def changing_inventory(paths: tuple[Path, ...]) -> tuple[Path, ...]:
        nonlocal calls
        calls += 1
        result = real_inventory(paths)
        if calls == 2:
            return (*result, tmp_path / "newly-missing-parent")
        return result

    monkeypatch.setattr(operations, "_paths_with_missing_ancestors", changing_inventory)

    with pytest.raises(SetforgeError, match="ancestor topology changed"):
        _prepare(tmp_path, paths=(path,))


def test_recovery_ignores_snapshots_for_checkpoints_that_never_began(
    tmp_path: Path, operation_state: Path
) -> None:
    untouched = tmp_path / "later"
    untouched.write_text("baseline", encoding="utf-8")
    journal = _prepare(tmp_path, paths=(untouched,))
    untouched.write_text("user change", encoding="utf-8")

    operations.recover_files(journal)

    assert untouched.read_text(encoding="utf-8") == "user change"


def test_recovery_removes_transition_committed_after_prepare(
    tmp_path: Path, operation_state: Path
) -> None:
    journal = operations.begin_checkpoint(
        _prepare(tmp_path),
        name="transition",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="remove transition",
    )
    transition = transitions.write_transition(
        transitions.make_meta(transitions.TransitionCommand.REVERT, "p"),
        {},
        {},
        None,
    )
    other = transitions.write_transition(
        transitions.make_meta(transitions.TransitionCommand.REVERT, "other"),
        {},
        {},
        None,
    )

    operations.recover_files(journal)

    assert not transition.exists()
    assert other.exists()


def test_recover_on_error_restores_and_preserves_primary_exception(
    tmp_path: Path, operation_state: Path
) -> None:
    path = tmp_path / "live"
    path.write_text("before", encoding="utf-8")

    def fail_during_apply() -> None:
        with operations.recover_on_error("p", "install"):
            journal = _prepare(tmp_path, paths=(path,))
            operations.begin_checkpoint(
                journal,
                name="files",
                kind=operations.CheckpointKind.REVERSIBLE,
                recovery="restore files",
            )
            path.write_text("after", encoding="utf-8")
            raise RuntimeError("apply failed")

    with pytest.raises(RuntimeError, match="apply failed"):
        fail_during_apply()

    assert path.read_text(encoding="utf-8") == "before"
    assert operations.active("p") is None


def test_recover_on_error_preserves_primary_when_recovery_subprocess_fails(
    tmp_path: Path,
    operation_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _prepare(tmp_path)
    operations.begin_checkpoint(
        journal,
        name="adapter",
        kind=operations.CheckpointKind.COMPENSATABLE,
        recovery="restore adapter",
    )
    monkeypatch.setattr(
        operations,
        "recover_adapters",
        lambda _journal: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, ["tool"])
        ),
    )

    with (
        pytest.raises(RuntimeError, match="primary") as caught,
        operations.recover_on_error("p", "install"),
    ):
        raise RuntimeError("primary")

    assert any("automatic recovery failed" in note for note in caught.value.__notes__)
    assert operations.active("p") is not None


def test_recover_on_error_preserves_primary_when_journal_load_fails(
    tmp_path: Path,
    operation_state: Path,
) -> None:
    _prepare(tmp_path)
    operations.journal_path("p").write_text("not-json", encoding="utf-8")

    with (
        pytest.raises(RuntimeError, match="primary") as caught,
        operations.recover_on_error("p", "install"),
    ):
        raise RuntimeError("primary")

    assert any("automatic recovery failed" in note for note in caught.value.__notes__)


def test_adapter_recovery_restores_extension_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from setforge import vscode_extensions

    installed = {"extra.ext"}
    monkeypatch.setattr(vscode_extensions, "list_installed", lambda: set(installed))
    monkeypatch.setattr(vscode_extensions, "install_one", installed.add)
    monkeypatch.setattr(vscode_extensions, "uninstall_one", installed.remove)
    journal = operations.OperationJournal(
        operation_id="op",
        command="install",
        profile="p",
        config_dir=None,
        state_dir=transitions.state_root().resolve(),
        resources_lock=True,
        phase=operations.OperationPhase.APPLYING,
        created_at="2026-01-01T00:00:00+00:00",
        command_line=(),
        paths=(),
        state_snapshots=(),
        adapters=(
            operations.AdapterSnapshot(
                operations.AdapterKind.EXTENSIONS, '["expected.ext"]'
            ),
        ),
        checkpoints=(
            operations.OperationCheckpoint(
                "extensions",
                operations.CheckpointKind.COMPENSATABLE,
                "restore extensions",
                adapters=(operations.AdapterKind.EXTENSIONS,),
            ),
        ),
    )

    operations.recover_adapters(journal)

    assert installed == {"expected.ext"}


def test_adapter_recovery_restores_mcp_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from setforge import mcp_servers
    from setforge.config import McpScope

    current: dict[str, tuple[tuple[str, ...], McpScope]] = {
        "server": (("new",), McpScope.USER)
    }
    monkeypatch.setattr(mcp_servers, "mcp_get_command", current.get)
    monkeypatch.setattr(
        mcp_servers, "mcp_remove", lambda name, **_kwargs: current.pop(name)
    )
    monkeypatch.setattr(
        mcp_servers,
        "mcp_add",
        lambda name, ref: current.__setitem__(name, (tuple(ref.command), ref.scope)),
    )
    journal = operations.OperationJournal(
        operation_id="op",
        command="install",
        profile="p",
        config_dir=None,
        state_dir=transitions.state_root().resolve(),
        resources_lock=True,
        phase=operations.OperationPhase.APPLYING,
        created_at="2026-01-01T00:00:00+00:00",
        command_line=(),
        paths=(),
        state_snapshots=(),
        adapters=(
            operations.AdapterSnapshot(
                operations.AdapterKind.MCP,
                '[{"name":"server","prior":[["old","--flag"],"user"]}]',
            ),
        ),
        checkpoints=(
            operations.OperationCheckpoint(
                "mcp",
                operations.CheckpointKind.COMPENSATABLE,
                "restore MCP",
                adapters=(operations.AdapterKind.MCP,),
            ),
        ),
    )

    operations.recover_adapters(journal)

    assert current == {"server": (("old", "--flag"), McpScope.USER)}


def test_plugin_recovery_respects_dependencies_and_replaces_drifted_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from setforge import claude_plugins

    events: list[str] = []
    marketplaces: dict[str, dict[str, object]] = {
        "extra": {"source": "path:/x"},
        "expected": {"source": "github:wrong/repo"},
    }
    plugins: dict[str, dict[str, object]] = {
        "extra-tool@extra": {"enabled": True},
        "tool@expected": {"enabled": True},
    }
    add_attempts = 0
    monkeypatch.setattr(claude_plugins, "list_marketplaces", lambda: dict(marketplaces))
    monkeypatch.setattr(claude_plugins, "list_installed", lambda: dict(plugins))

    def remove_marketplace(name: str) -> None:
        assert not any(plugin.endswith(f"@{name}") for plugin in plugins)
        events.append(f"remove-marketplace:{name}")
        marketplaces.pop(name)

    def add_marketplace(name: str, source: object) -> None:
        nonlocal add_attempts
        add_attempts += 1
        events.append(f"add-marketplace:{name}")
        if add_attempts == 1:
            raise RuntimeError("interrupted marketplace replacement")
        marketplaces[name] = {"source": "github:owner/repo", "model": source}

    def install_plugin(name: str, marketplace: str) -> None:
        assert marketplace in marketplaces
        events.append(f"install:{name}@{marketplace}")
        plugins[f"{name}@{marketplace}"] = {"enabled": True}

    def uninstall_plugin(plugin_id: str) -> None:
        events.append(f"uninstall:{plugin_id}")
        plugins.pop(plugin_id)

    monkeypatch.setattr(claude_plugins, "marketplace_remove", remove_marketplace)
    monkeypatch.setattr(claude_plugins, "marketplace_add", add_marketplace)
    monkeypatch.setattr(claude_plugins, "plugin_install", install_plugin)
    monkeypatch.setattr(claude_plugins, "plugin_uninstall", uninstall_plugin)
    monkeypatch.setattr(claude_plugins, "plugin_enable", lambda _name: None)
    monkeypatch.setattr(claude_plugins, "plugin_disable", lambda _name: None)
    journal = operations.OperationJournal(
        operation_id="op",
        command="install",
        profile="p",
        config_dir=None,
        state_dir=transitions.state_root().resolve(),
        resources_lock=True,
        phase=operations.OperationPhase.APPLYING,
        created_at="2026-01-01T00:00:00+00:00",
        command_line=(),
        paths=(),
        state_snapshots=(),
        adapters=(
            operations.AdapterSnapshot(
                operations.AdapterKind.PLUGINS,
                json.dumps(
                    {
                        "marketplaces": {"expected": {"source": "github:owner/repo"}},
                        "plugins": {"tool@expected": {"enabled": True}},
                    }
                ),
            ),
        ),
        checkpoints=(
            operations.OperationCheckpoint(
                "plugins",
                operations.CheckpointKind.COMPENSATABLE,
                "restore plugins",
                adapters=(operations.AdapterKind.PLUGINS,),
            ),
        ),
    )

    with pytest.raises(RuntimeError, match="interrupted marketplace"):
        operations.recover_adapters(journal)
    operations.recover_adapters(journal)

    assert events == [
        "uninstall:extra-tool@extra",
        "uninstall:tool@expected",
        "remove-marketplace:expected",
        "remove-marketplace:extra",
        "add-marketplace:expected",
        "add-marketplace:expected",
        "install:tool@expected",
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda raw: raw.update(schema_version=999),
        lambda raw: raw.update(profile="other"),
        lambda raw: raw.update(paths="not-a-list"),
    ],
)
def test_load_fails_closed_on_invalid_journal(
    tmp_path: Path,
    operation_state: Path,
    mutation: Callable[[dict[str, object]], None],
) -> None:
    _prepare(tmp_path)
    path = operations.journal_path("p")
    raw = json.loads(path.read_text(encoding="utf-8"))
    mutation(raw)
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(SetforgeError, match="operation journal"):
        operations.load("p")


def _set_nested(
    raw: dict[str, object], section: str, index: int, key: str, value: object
) -> None:
    raw[section][index][key] = value  # type: ignore[index]


def _duplicate_row(raw: dict[str, object], section: str) -> None:
    rows = raw[section]
    rows.append(deepcopy(rows[0]))  # type: ignore[attr-defined,index]


def _duplicate_state_alias(raw: dict[str, object]) -> None:
    rows = raw["state_snapshots"]
    alias = deepcopy(rows[0])  # type: ignore[index]
    alias["key"] = "./file"
    rows.append(alias)  # type: ignore[attr-defined]


def _make_path_noncanonical(raw: dict[str, object], key: str) -> None:
    value = raw[key]
    assert isinstance(value, str)
    raw[key] = f"{value}/child/.."


def _add_invalid_path_guard(raw: dict[str, object]) -> None:
    paths = raw["paths"]
    path = Path(paths[0]["path"]).parent  # type: ignore[index]
    raw["path_guards"] = [
        {"path": str(path), "device": 0, "inode": 0, "mode": 0o100644}
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda raw: _set_nested(raw, "paths", 0, "path", "relative"),
        lambda raw: _set_nested(raw, "paths", 0, "mode", True),
        lambda raw: _set_nested(raw, "paths", 0, "mode", 0o10000),
        lambda raw: _set_nested(raw, "paths", 0, "mtime_ns", True),
        lambda raw: _set_nested(raw, "paths", 0, "payload", None),
        lambda raw: _duplicate_row(raw, "paths"),
        _add_invalid_path_guard,
        lambda raw: _set_nested(raw, "state_snapshots", 0, "store", "unknown"),
        lambda raw: _set_nested(raw, "state_snapshots", 0, "key", "../escape"),
        lambda raw: _set_nested(raw, "state_snapshots", 0, "key", "."),
        lambda raw: _set_nested(raw, "state_snapshots", 0, "profile", "../../escape"),
        lambda raw: _set_nested(raw, "state_snapshots", 0, "profile", "."),
        lambda raw: _duplicate_row(raw, "state_snapshots"),
        _duplicate_state_alias,
        lambda raw: _set_nested(raw, "adapters", 0, "payload_json", "{"),
        lambda raw: _set_nested(raw, "adapters", 0, "payload_json", '[""]'),
        lambda raw: _set_nested(raw, "checkpoints", 0, "paths", ["/unknown"]),
        lambda raw: _set_nested(raw, "checkpoints", 0, "adapters", ["mcp"]),
        lambda raw: _set_nested(raw, "checkpoints", 0, "restore_state", "yes"),
        lambda raw: raw.update(config_dir="relative"),
        lambda raw: raw.update(state_dir="relative"),
        lambda raw: _make_path_noncanonical(raw, "config_dir"),
        lambda raw: _make_path_noncanonical(raw, "state_dir"),
        lambda raw: raw.update(reserved_config_dirs=[]),
        lambda raw: raw.update(reserved_config_dirs=["relative"]),
        lambda raw: raw.update(resources_lock="yes"),
        lambda raw: raw.update(resources_lock=False),
        lambda raw: raw.update(reserved_profiles=[]),
        lambda raw: raw.update(reserved_profiles=["p", "p"]),
        lambda raw: raw.update(reserved_profiles=["z", "p"]),
        lambda raw: raw.update(command_line="install"),
        lambda raw: raw.update(schema_version=True),
    ],
)
def test_load_rejects_semantically_invalid_recovery_rows(
    tmp_path: Path,
    operation_state: Path,
    mutation: Callable[[dict[str, object]], None],
) -> None:
    path = tmp_path / "file"
    path.write_text("before", encoding="utf-8")
    state = transitions.StateSnapshotEntry(
        store=transitions.SnapshotStore.BASE,
        profile="p",
        key="file",
        payload=b"base",
    )
    journal = operations.prepare(
        command="install",
        profile="p",
        config_dir=tmp_path,
        resources_lock=True,
        command_line=("install",),
        paths=(path,),
        state_snapshots=(state,),
        adapters=(
            operations.AdapterSnapshot(
                operations.AdapterKind.EXTENSIONS, '["expected.ext"]'
            ),
        ),
    )
    operations.begin_checkpoint(
        journal,
        name="effect",
        kind=operations.CheckpointKind.COMPENSATABLE,
        recovery="restore effect",
    )
    journal_path = operations.journal_path("p")
    raw = json.loads(journal_path.read_text(encoding="utf-8"))
    mutation(raw)
    journal_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(SetforgeError, match="invalid operation journal"):
        operations.load("p")


def test_invalid_later_snapshot_is_rejected_before_any_recovery_effect(
    tmp_path: Path,
    operation_state: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_text("before", encoding="utf-8")
    second.write_text("before", encoding="utf-8")
    journal = operations.begin_checkpoint(
        _prepare(tmp_path, paths=(first, second)),
        name="files",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore files",
    )
    first.write_text("after", encoding="utf-8")
    raw = json.loads(operations.journal_path("p").read_text(encoding="utf-8"))
    raw["paths"][1]["payload"] = "not-base64!"
    operations.journal_path("p").write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(SetforgeError, match="invalid operation journal"):
        operations.load(journal.profile)

    assert first.read_text(encoding="utf-8") == "after"


def test_state_root_mismatch_refuses_before_path_recovery(
    tmp_path: Path,
    operation_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "live"
    path.write_text("before", encoding="utf-8")
    journal = operations.begin_checkpoint(
        _prepare(tmp_path, paths=(path,)),
        name="files",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore files",
    )
    path.write_text("after", encoding="utf-8")
    monkeypatch.setattr(transitions, "state_root", lambda: tmp_path / "other-state")

    with pytest.raises(SetforgeError, match="SETFORGE_STATE_DIR"):
        operations.recover_files(journal)

    assert path.read_text(encoding="utf-8") == "after"


def test_invalid_later_adapter_is_rejected_before_earlier_adapter_calls(
    tmp_path: Path,
    operation_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from setforge import vscode_extensions

    journal = operations.prepare(
        command="install",
        profile="p",
        config_dir=tmp_path,
        resources_lock=True,
        command_line=("install",),
        paths=(),
        adapters=(
            operations.AdapterSnapshot(
                operations.AdapterKind.EXTENSIONS, '["expected.ext"]'
            ),
            operations.AdapterSnapshot(operations.AdapterKind.MCP, "[]"),
        ),
    )
    operations.begin_checkpoint(
        journal,
        name="adapters",
        kind=operations.CheckpointKind.COMPENSATABLE,
        recovery="restore adapters",
    )
    raw = json.loads(operations.journal_path("p").read_text(encoding="utf-8"))
    raw["adapters"][1]["payload_json"] = '[{"name":"bad","prior":[1]}]'
    operations.journal_path("p").write_text(json.dumps(raw), encoding="utf-8")
    calls = 0

    def list_installed() -> set[str]:
        nonlocal calls
        calls += 1
        return set()

    monkeypatch.setattr(vscode_extensions, "list_installed", list_installed)

    with pytest.raises(SetforgeError, match="invalid operation journal"):
        operations.load("p")

    assert calls == 0


def test_cross_profile_state_snapshot_reserves_its_profile_namespace(
    tmp_path: Path, operation_state: Path
) -> None:
    state = transitions.StateSnapshotEntry(
        store=transitions.SnapshotStore.BASE,
        profile="actual",
        key="file",
        payload=b"base",
    )
    journal = operations.prepare(
        command="revert",
        profile="migrate",
        config_dir=tmp_path,
        resources_lock=False,
        command_line=("revert",),
        paths=(),
        state_snapshots=(state,),
    )

    assert operations.locked_profiles(journal) == ("actual", "migrate")
    assert operations.conflicting_journals(
        resources=False,
        config_dir=None,
        profile="actual",
    ) == (journal,)


def test_extra_reserved_profile_survives_reload_and_blocks_mutation(
    tmp_path: Path, operation_state: Path
) -> None:
    journal = operations.prepare(
        command="migrate",
        profile="migrate",
        config_dir=tmp_path,
        resources_lock=False,
        command_line=("migrate",),
        paths=(),
        profiles=("team/dev",),
    )

    loaded = operations.load("migrate")

    assert loaded.reserved_profiles == ("migrate", "team/dev")
    assert operations.locked_profiles(loaded) == ("migrate", "team/dev")
    assert operations.conflicting_journals(
        resources=False,
        config_dir=None,
        profile="team/dev",
    ) == (journal,)


@pytest.mark.parametrize(
    ("kind", "valid_payload", "invalid_payload"),
    [
        (
            operations.AdapterKind.PLUGINS,
            {
                "marketplaces": {"market": {"source": "github:owner/repo"}},
                "plugins": {"tool@market": {"enabled": True}},
            },
            {
                "marketplaces": {"market": {"source": "github:owner/repo"}},
                "plugins": {"@market": {"enabled": True}},
            },
        ),
        (
            operations.AdapterKind.PLUGINS,
            {"marketplaces": {}, "plugins": {}},
            {
                "marketplaces": {"market": {"source": "github:"}},
                "plugins": {},
            },
        ),
        (
            operations.AdapterKind.PLUGINS,
            {
                "marketplaces": {"market": {"source": "github:owner/repo"}},
                "plugins": {"tool@market": {"enabled": True}},
            },
            {
                "marketplaces": {"market": {"source": "github:owner/repo"}},
                "plugins": {"tool@missing": {"enabled": True}},
            },
        ),
        (
            operations.AdapterKind.MCP,
            [{"name": "server", "prior": None}],
            [{"name": "", "prior": None}],
        ),
        (
            operations.AdapterKind.MCP,
            [{"name": "server", "prior": None}],
            [{"name": "server", "prior": [[""], "user"]}],
        ),
    ],
)
def test_load_rejects_invalid_adapter_identity_before_recovery(
    tmp_path: Path,
    operation_state: Path,
    kind: operations.AdapterKind,
    valid_payload: object,
    invalid_payload: object,
) -> None:
    journal = operations.prepare(
        command="install",
        profile="p",
        config_dir=tmp_path,
        resources_lock=True,
        command_line=("install",),
        paths=(),
        adapters=(operations.AdapterSnapshot(kind, json.dumps(valid_payload)),),
    )
    operations.begin_checkpoint(
        journal,
        name="adapter",
        kind=operations.CheckpointKind.COMPENSATABLE,
        recovery="restore adapter",
        adapters=(kind,),
    )
    path = operations.journal_path("p")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["adapters"][0]["payload_json"] = json.dumps(invalid_payload)
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(SetforgeError, match="invalid operation journal"):
        operations.load("p")


def test_state_root_mismatch_refuses_before_adapter_recovery(
    tmp_path: Path,
    operation_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from setforge import vscode_extensions

    journal = operations.prepare(
        command="install",
        profile="p",
        config_dir=tmp_path,
        resources_lock=True,
        command_line=("install",),
        paths=(),
        adapters=(
            operations.AdapterSnapshot(
                operations.AdapterKind.EXTENSIONS, '["expected.ext"]'
            ),
        ),
    )
    journal = operations.begin_checkpoint(
        journal,
        name="extensions",
        kind=operations.CheckpointKind.COMPENSATABLE,
        recovery="restore extensions",
    )
    calls = 0

    def list_installed() -> set[str]:
        nonlocal calls
        calls += 1
        return set()

    monkeypatch.setattr(vscode_extensions, "list_installed", list_installed)
    monkeypatch.setattr(transitions, "state_root", lambda: tmp_path / "other-state")

    with pytest.raises(SetforgeError, match="SETFORGE_STATE_DIR"):
        operations.validate_recovery(journal)

    assert calls == 0


def test_active_journal_is_visible_across_transition_state_roots(
    tmp_path: Path,
    operation_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _prepare(tmp_path)
    monkeypatch.setattr(transitions, "state_root", lambda: tmp_path / "other-state")

    assert operations.load("p").operation_id == journal.operation_id
    with pytest.raises(SetforgeError, match="unfinished install"):
        operations.refuse_conflicting_mutation(
            resources=True, config_dir=None, profile="other"
        )
