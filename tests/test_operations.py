"""Unit and recovery-contract tests for write-ahead operation journals."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from copy import deepcopy
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from setforge import codex_plugins, locking, operations, orphan_scan, transitions
from setforge.errors import SetforgeError
from setforge.locking import mutation_locks, profile_lock
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


def _held_elsewhere(lock_path: Path) -> bool:
    """Return whether another open file description holds ``lock_path``."""
    with lock_path.open("a") as probe:
        try:
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
        return False


def _writer_locks_held(profile: str) -> tuple[bool, bool]:
    """Return whether the mutation gate and ``profile`` lock are held right now."""
    locking.require_resources_lock()
    return (
        _held_elsewhere(Path.home() / ".cache/setforge/locks/mutation-gate.lock"),
        _held_elsewhere(locking._profile_lock_path(profile)),
    )


def _prepare(
    tmp_path: Path, *, paths: tuple[Path, ...] = ()
) -> operations.OperationJournal:
    return operations.prepare(
        command="install",
        profile="p",
        config_dir=tmp_path,
        resources_lock=True,
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

    with pytest.raises(SetforgeError, match="blocks this mutation"):
        operations.refuse_conflicting_mutation(
            resources=False, config_dir=tmp_path, profile=None
        )

    operations.refuse_conflicting_mutation(
        resources=False, config_dir=tmp_path / "other", profile=None
    )


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


def test_checkpoint_refuses_path_without_a_journaled_preimage(
    tmp_path: Path, operation_state: Path
) -> None:
    journaled = tmp_path / "journaled.txt"
    journaled.write_text("before", encoding="utf-8")
    journal = _prepare(tmp_path, paths=(journaled,))

    with pytest.raises(SetforgeError, match="absent from the journal"):
        operations.begin_checkpoint(
            journal,
            name="tracked-files",
            kind=operations.CheckpointKind.REVERSIBLE,
            recovery="restore captured paths",
            paths=(journaled, tmp_path / "unjournaled.txt"),
        )

    assert operations.load("p") == journal


def test_checkpoint_covers_path_below_journaled_absent_ancestor(
    tmp_path: Path, operation_state: Path
) -> None:
    created = tmp_path / "created"
    journal = _prepare(tmp_path, paths=(created,))

    applying = operations.begin_checkpoint(
        journal,
        name="tracked-files",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore captured paths",
        paths=(created / "nested" / "leaf.txt",),
    )

    assert applying.checkpoints[-1].paths == (str(created),)


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
    with mutation_locks(resources=True):
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
    with (
        mutation_locks(resources=True, allow_operation_id=journal.operation_id),
        pytest.raises(OSError, match="crash after move"),
    ):
        store.move_locked(
            source,
            destination,
            expected_owner=owner,
            expected_generation=claim.generation,
        )
    monkeypatch.setattr(store, "_unlink_intent", real_unlink)

    with mutation_locks(resources=True, allow_operation_id=journal.operation_id):
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


def _symlinked_live(tmp_path: Path) -> tuple[Path, Path]:
    real = tmp_path / "volume"
    (real / "live").mkdir(parents=True)
    link = tmp_path / "alias"
    link.symlink_to(real, target_is_directory=True)
    return link, real


def _journal_below_symlink(tmp_path: Path) -> tuple[Path, Path]:
    link, real = _symlinked_live(tmp_path)
    path = link / "live" / "file"
    path.write_text("before", encoding="utf-8")
    operations.begin_checkpoint(
        operations.prepare(
            command="sync",
            profile="p",
            config_dir=tmp_path,
            resources_lock=False,
            paths=(path,),
            path_guards=orphan_scan.capture_parent_path_guards((path,)),
        ),
        name="files",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore files",
    )
    path.write_text("after", encoding="utf-8")
    return link, real


def test_recovery_restores_below_ancestor_captured_as_symlink(
    tmp_path: Path, operation_state: Path
) -> None:
    link, real = _journal_below_symlink(tmp_path)

    operations.recover_files(operations.load("p"))

    assert (real / "live" / "file").read_text(encoding="utf-8") == "before"
    assert link.is_symlink()


def test_recovery_refuses_captured_symlink_retargeted_before_recovery(
    tmp_path: Path, operation_state: Path
) -> None:
    link, real = _journal_below_symlink(tmp_path)
    other = tmp_path / "other"
    (other / "live").mkdir(parents=True)
    (other / "live" / "file").write_text("foreign", encoding="utf-8")
    link.unlink()
    link.symlink_to(other, target_is_directory=True)

    with pytest.raises(SetforgeError, match="parent changed before recovery"):
        operations.recover_files(operations.load("p"))

    assert (other / "live" / "file").read_text(encoding="utf-8") == "foreign"
    assert (real / "live" / "file").read_text(encoding="utf-8") == "after"
    assert operations.active("p") is not None


def test_anchored_restore_refuses_captured_symlink_retargeted_before_write(
    tmp_path: Path,
) -> None:
    link, real = _symlinked_live(tmp_path)
    path = link / "live" / "file"
    identities = operations._guard_identities(
        orphan_scan.capture_parent_path_guards((path,))
    )
    other = tmp_path / "other"
    (other / "live").mkdir(parents=True)
    link.unlink()
    link.symlink_to(other, target_is_directory=True)

    with pytest.raises(SetforgeError, match="parent changed before write"):
        operations._restore_path(
            operations.PathSnapshot(
                path, operations.SnapshotKind.FILE, mode=0o600, payload=b"restored"
            ),
            guard_identities=identities,
        )

    assert not (other / "live" / "file").exists()
    assert not (real / "live" / "file").exists()


def test_anchored_restore_refuses_directory_replaced_by_symlink_to_itself(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "live"
    parent.mkdir()
    path = parent / "file"
    identities = operations._guard_identities(
        orphan_scan.capture_parent_path_guards((path,))
    )
    moved = tmp_path / "moved"
    parent.rename(moved)
    parent.symlink_to(moved, target_is_directory=True)

    with pytest.raises(SetforgeError, match="parent changed before write"):
        operations._restore_path(
            operations.PathSnapshot(
                path, operations.SnapshotKind.FILE, mode=0o600, payload=b"restored"
            ),
            guard_identities=identities,
        )

    assert not (moved / "file").exists()


def _journal_plain_file(tmp_path: Path, command: str) -> Path:
    parent = tmp_path / "live"
    parent.mkdir()
    path = parent / "file"
    path.write_text("before", encoding="utf-8")
    operations.begin_checkpoint(
        operations.prepare(
            command=command,
            profile="p",
            config_dir=tmp_path,
            resources_lock=False,
            paths=(path,),
            path_guards=_path_guards(path),
        ),
        name="files",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore files",
    )
    path.write_text("after", encoding="utf-8")
    return path


@pytest.mark.parametrize("command", ["sync", "snapshot restore", "install"])
def test_recovery_accepts_guards_journaled_under_another_device_number(
    tmp_path: Path, operation_state: Path, command: str
) -> None:
    path = _journal_plain_file(tmp_path, command)
    journal_file = operations.journal_path("p")
    raw = json.loads(journal_file.read_text(encoding="utf-8"))
    for guard in raw["path_guards"]:
        guard["device"] += 7
    journal_file.write_text(json.dumps(raw), encoding="utf-8")

    operations.validate_recovery(operations.load("p"))
    operations.recover_files(operations.load("p"))

    assert path.read_text(encoding="utf-8") == "before"


def test_recovery_refuses_one_guard_moved_to_another_filesystem(
    tmp_path: Path, operation_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _journal_plain_file(tmp_path, "sync")
    lstat = Path.lstat

    def moved(candidate: Path) -> os.stat_result:
        info = lstat(candidate)
        if candidate != path.parent:
            return info
        fields = list(info)
        fields[2] += 7
        return os.stat_result(fields)

    monkeypatch.setattr(Path, "lstat", moved)

    with pytest.raises(SetforgeError, match="parent changed before recovery"):
        operations.recover_files(operations.load("p"))

    assert path.read_text(encoding="utf-8") == "after"
    assert operations.active("p") is not None


def test_recovery_accepts_permission_change_on_plain_ancestor(
    tmp_path: Path, operation_state: Path
) -> None:
    path = _journal_plain_file(tmp_path, "sync")
    path.parent.chmod(0o700)

    operations.recover_files(operations.load("p"))

    assert path.read_text(encoding="utf-8") == "before"


def _partially_guarded_journal(tmp_path: Path, command: str) -> tuple[Path, Path]:
    guarded = tmp_path / "guarded" / "item"
    plain = tmp_path / "plain" / "note.txt"
    for path in (guarded, plain):
        path.parent.mkdir()
        path.write_text("before", encoding="utf-8")
    operations.begin_checkpoint(
        operations.prepare(
            command=command,
            profile="p",
            config_dir=tmp_path,
            resources_lock=False,
            paths=(guarded, plain),
            path_guards=orphan_scan.capture_parent_path_guards((guarded,)),
        ),
        name="files",
        kind=operations.CheckpointKind.COMPENSATABLE,
        recovery="restore files",
    )
    for path in (guarded, plain):
        path.write_text("after", encoding="utf-8")
    return guarded, plain


def test_revert_recovery_restores_paths_it_never_guarded(
    tmp_path: Path, operation_state: Path
) -> None:
    guarded, plain = _partially_guarded_journal(tmp_path, "revert")

    operations.recover_files(operations.load("p"))

    assert guarded.read_text(encoding="utf-8") == "before"
    assert plain.read_text(encoding="utf-8") == "before"


def test_revert_recovery_keeps_guarded_restore_for_guarded_paths(
    tmp_path: Path, operation_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guarded, plain = _partially_guarded_journal(tmp_path, "revert")
    restore = operations._restore_path
    anchored: dict[Path, bool] = {}

    def record(snapshot: operations.PathSnapshot, **kwargs: object) -> bool:
        anchored[snapshot.path] = kwargs["guard_identities"] is not None
        return restore(snapshot, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(operations, "_restore_path", record)

    operations.recover_files(operations.load("p"))

    assert anchored == {guarded: True, plain: False}


def test_other_recovery_still_refuses_a_path_without_guards(
    tmp_path: Path, operation_state: Path
) -> None:
    _, plain = _partially_guarded_journal(tmp_path, "sync")

    with pytest.raises(SetforgeError, match="lacks an identity guard"):
        operations.recover_files(operations.load("p"))

    assert plain.read_text(encoding="utf-8") == "after"
    assert operations.active("p") is not None


def test_recovery_refuses_unscoped_parent_removed_after_preflight(
    tmp_path: Path,
    operation_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert operation_state.resolve().is_relative_to(tmp_path.resolve())
    parent = tmp_path / "live"
    parent.mkdir()
    path = parent / "file"
    path.write_text("before", encoding="utf-8")
    journal = operations.prepare(
        command="sync",
        profile="p",
        config_dir=tmp_path,
        resources_lock=False,
        paths=(path,),
        path_guards=_path_guards(path),
    )
    journal = operations.begin_checkpoint(
        journal,
        name="files",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore files",
    )
    path.write_text("after", encoding="utf-8")
    original = tmp_path / "original-live"
    validate = operations._validate_path_guards

    def remove_after_validation(candidate: operations.OperationJournal) -> None:
        validate(candidate)
        parent.rename(original)

    monkeypatch.setattr(operations, "_validate_path_guards", remove_after_validation)

    with pytest.raises(SetforgeError, match="parent changed"):
        operations.recover_files(journal)

    assert not parent.exists()
    assert (original / "file").read_text(encoding="utf-8") == "after"
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
        paths=(path,),
        path_guards=guards,
    )
    loaded = operations.load("p")

    assert loaded == journal
    assert loaded.reserved_config_dirs == tuple(
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
        paths=(path,),
        path_guards=_path_guards(path),
    )

    assert operations.load("p") == journal
    assert journal.reserved_config_dirs == (tmp_path.resolve(),)


@pytest.mark.parametrize(
    "key",
    [
        "path_guards",
        "adapters",
        "reserved_config_dirs",
        "reserved_config_dirs_digest",
        "transition_names_before",
    ],
)
def test_journal_missing_a_required_key_is_invalid(
    tmp_path: Path, operation_state: Path, key: str
) -> None:
    path = tmp_path / "live" / "file"
    path.parent.mkdir()
    path.write_text("before", encoding="utf-8")
    operations.prepare(
        command="snapshot restore",
        profile="p",
        config_dir=tmp_path,
        resources_lock=False,
        paths=(path,),
        path_guards=_path_guards(path),
    )
    journal_path = operations.journal_path("p")
    raw = json.loads(journal_path.read_text(encoding="utf-8"))
    raw.pop(key)
    journal_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(SetforgeError, match="invalid operation journal"):
        operations.load("p")


def test_snapshot_restore_journal_cannot_narrow_path_guards(
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
        paths=(path,),
        path_guards=_path_guards(path),
    )
    journal_path = operations.journal_path("p")
    raw = json.loads(journal_path.read_text(encoding="utf-8"))
    raw["path_guards"] = raw["path_guards"][1:]
    journal_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(SetforgeError, match="invalid operation journal"):
        operations.load("p")


def test_journal_stays_compatible_with_earlier_releases(
    tmp_path: Path, operation_state: Path
) -> None:
    path = tmp_path / "file"
    path.write_text("before", encoding="utf-8")
    journal = operations.begin_checkpoint(
        _prepare(tmp_path, paths=(path,)),
        name="files",
        kind=operations.CheckpointKind.REVERSIBLE,
    )
    journal_path = operations.journal_path("p")
    raw = json.loads(journal_path.read_text(encoding="utf-8"))
    assert raw["command_line"] == []
    raw["command_line"] = ["install", "--profile=p"]
    for row in raw["checkpoints"]:
        assert row["recovery"]
        assert row["recovered"] is False
        row["recovered"] = True
    journal_path.write_text(json.dumps(raw), encoding="utf-8")
    path.write_text("after", encoding="utf-8")

    loaded = operations.load("p")
    assert loaded == journal
    assert operations.recover_automatically(loaded)

    assert path.read_text(encoding="utf-8") == "before"
    assert operations.active("p") is None


def test_irreversible_checkpoint_requires_manual_recovery_text(
    tmp_path: Path, operation_state: Path
) -> None:
    journal = _prepare(tmp_path)

    with pytest.raises(SetforgeError, match="needs manual recovery text"):
        operations.begin_checkpoint(
            journal, name="packages", kind=operations.CheckpointKind.IRREVERSIBLE
        )

    assert operations.load("p").checkpoints == ()


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


def _journal_creating_state_root(
    operation_state: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(operation_state))
    store_file = operation_state / "scalar-bases" / "p.json"
    operations.begin_checkpoint(
        operations.prepare(
            command="migrate",
            profile="p",
            config_dir=operation_state.parent,
            resources_lock=False,
            paths=(store_file,),
        ),
        name="files",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore files",
    )
    return store_file


def test_recovery_keeps_created_state_root_that_holds_only_its_locks(
    tmp_path: Path, operation_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_file = _journal_creating_state_root(operation_state, monkeypatch)

    with profile_lock("p"):
        store_file.parent.mkdir(parents=True)
        store_file.write_text("{}", encoding="utf-8")
        operations.complete(operations.recover_files(operations.load("p")))

    assert not store_file.parent.exists()
    assert [path.name for path in operation_state.iterdir()] == ["locks"]
    assert operations.active("p") is None


def test_recovery_refuses_created_state_root_with_foreign_content(
    tmp_path: Path, operation_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _journal_creating_state_root(operation_state, monkeypatch)

    with (
        profile_lock("p"),
        profile_lock("q"),
        pytest.raises(SetforgeError, match="non-empty recovery directory"),
    ):
        operations.recover_files(operations.load("p"))

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


def test_noninstall_recovery_accepts_journaled_created_parent(
    tmp_path: Path, operation_state: Path
) -> None:
    root = tmp_path / "created-root"
    child = root / "file"
    assert operation_state.resolve().is_relative_to(tmp_path.resolve())
    assert child.resolve().is_relative_to(tmp_path.resolve())
    guards: list[operations.PathGuard] = []
    for ancestor in child.parents:
        if ancestor == Path("/"):
            continue
        try:
            info = ancestor.stat()
        except FileNotFoundError:
            guards.append(operations.PathGuard(ancestor, None, None, None))
        else:
            guards.append(
                operations.PathGuard(ancestor, info.st_dev, info.st_ino, info.st_mode)
            )
    journal = operations.prepare(
        command="sync",
        profile="p",
        config_dir=tmp_path,
        resources_lock=False,
        paths=(child,),
        path_guards=tuple(guards),
    )
    journal = operations.begin_checkpoint(
        journal,
        name="files",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore files",
    )
    root.mkdir()
    child.write_text("created", encoding="utf-8")

    recovered = operations.recover_files(journal)

    assert not root.exists()
    assert recovered.phase is operations.OperationPhase.RECOVERING
    operations.complete(recovered)
    assert operations.active("p") is None


@pytest.mark.parametrize("inspection", ["iterdir", "rglob"])
def test_unbound_install_root_normalizes_inspection_failure(
    tmp_path: Path,
    operation_state: Path,
    monkeypatch: pytest.MonkeyPatch,
    inspection: str,
) -> None:
    root = tmp_path / "prepared-root"
    assert operation_state.resolve().is_relative_to(tmp_path.resolve())
    assert root.resolve().is_relative_to(tmp_path.resolve())
    guards = [operations.PathGuard(root, None, None, None)]
    for ancestor in root.parents:
        if ancestor == Path("/"):
            continue
        info = ancestor.stat()
        guards.append(
            operations.PathGuard(ancestor, info.st_dev, info.st_ino, info.st_mode)
        )
    journal = operations.prepare(
        command="install",
        profile="p",
        config_dir=tmp_path,
        resources_lock=False,
        paths=(root,),
        path_guards=tuple(guards),
    )
    journal = operations.begin_checkpoint(
        journal,
        name="prepare-target-roots",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore initially absent target roots",
        paths=(root,),
    )
    root.mkdir()
    if inspection == "iterdir":
        original_iterdir = Path.iterdir

        def fail_iterdir(path: Path) -> Iterator[Path]:
            if path == root:
                raise PermissionError("injected root listing failure")
            return original_iterdir(path)

        monkeypatch.setattr(Path, "iterdir", fail_iterdir)
    else:
        original_rglob = Path.rglob

        def fail_rglob(path: Path, pattern: str) -> Iterator[Path]:
            if path == root:
                raise PermissionError("injected descendant listing failure")
            return original_rglob(path, pattern)

        monkeypatch.setattr(Path, "rglob", fail_rglob)

    with pytest.raises(SetforgeError) as failure:
        operations.recover_files(journal)

    assert str(failure.value) == (
        f"journaled absent parent changed before recovery: {root}"
    )
    assert root.is_dir()
    assert operations.active("p") is not None


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

    assert transitions.stat_identity(before) != transitions.stat_identity(path.stat())


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


def test_transaction_rolls_back_a_failed_block_while_its_locks_are_held(
    tmp_path: Path, operation_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "live"
    path.write_text("before", encoding="utf-8")
    held_during_recovery: list[tuple[bool, bool]] = []
    real_recover = operations.recover_automatically

    def recording_recover(journal: operations.OperationJournal) -> bool:
        held_during_recovery.append(_writer_locks_held("p"))
        return real_recover(journal)

    monkeypatch.setattr(operations, "recover_automatically", recording_recover)

    def fail_during_apply() -> None:
        with operations.transaction(
            resources=True, profile="p", recover=("p", "install")
        ):
            journal = _prepare(tmp_path, paths=(path,))
            operations.begin_checkpoint(
                journal,
                name="files",
                kind=operations.CheckpointKind.REVERSIBLE,
                recovery="restore files",
            )
            path.write_text("after", encoding="utf-8")
            raise RuntimeError("apply failed")

    with pytest.raises(RuntimeError, match="apply failed") as caught:
        fail_during_apply()

    assert held_during_recovery == [(True, True)]
    assert not getattr(caught.value, "__notes__", [])
    assert path.read_text(encoding="utf-8") == "before"
    assert operations.active("p") is None
    with pytest.raises(SetforgeError, match="global resource lock"):
        _writer_locks_held("p")
    assert not _held_elsewhere(locking._profile_lock_path("p"))


def test_transaction_refuses_an_unfinished_operation_outside_its_scopes(
    tmp_path: Path, operation_state: Path
) -> None:
    journal = _prepare(tmp_path)

    with (
        pytest.raises(SetforgeError, match="unfinished install operation"),
        operations.transaction(profile="other", recover=("other", "install")),
    ):
        pytest.fail("the block ran beside an unfinished operation")

    assert operations.load("p") == journal


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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    from setforge import mcp_servers
    from setforge.config import McpScope

    current: dict[str, tuple[list[str], McpScope]] = {
        "server": (["new"], McpScope.USER)
    }
    monkeypatch.setattr(mcp_servers, "mcp_get_command", current.get)
    monkeypatch.setattr(
        mcp_servers, "mcp_remove", lambda name, **_kwargs: current.pop(name)
    )
    monkeypatch.setattr(
        mcp_servers,
        "mcp_add",
        lambda name, ref: current.__setitem__(name, (list(ref.command), ref.scope)),
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
        paths=(),
        state_snapshots=(),
        adapters=(
            operations.AdapterSnapshot(
                operations.AdapterKind.MCP,
                json.dumps(
                    [
                        {
                            "name": "server",
                            "prior": [["old", "--flag"], "user"],
                            "planned": [[["new"], "user"]],
                            "context": mcp_servers.inventory_context(),
                        }
                    ]
                ),
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

    assert current == {"server": (["old", "--flag"], McpScope.USER)}


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


@pytest.mark.parametrize(
    "payload",
    [b"", b'{"schema_version": 1, "profi', b"{}", b"[]", b"\xff\xfe\x00garbage\x80"],
    ids=["empty", "truncated", "empty-object", "not-an-object", "not-utf8"],
)
def test_unusable_journal_reports_the_file_and_how_to_get_past_it(
    tmp_path: Path, operation_state: Path, payload: bytes
) -> None:
    _prepare(tmp_path)
    path = operations.journal_path("p")
    path.write_bytes(payload)

    for blocked in (
        lambda: operations.load("p"),
        operations._refuse_active,
    ):
        with pytest.raises(SetforgeError) as failure:
            blocked()
        message = str(failure.value)
        assert f"operation journal {path}" in message
        assert "move that file aside" in message


def test_journal_from_newer_setforge_asks_for_an_upgrade(
    tmp_path: Path, operation_state: Path
) -> None:
    _prepare(tmp_path)
    path = operations.journal_path("p")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["schema_version"] = operations.JOURNAL_SCHEMA_VERSION + 1
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(SetforgeError, match="upgrade SetForge") as failure:
        operations.load("p")

    assert "move that file aside" not in str(failure.value)


def test_unreadable_journal_is_not_reported_as_corrupt(
    tmp_path: Path, operation_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare(tmp_path)
    path = operations.journal_path("p")
    read_text = Path.read_text

    def stale(candidate: Path, *args: object, **kwargs: object) -> str:
        if candidate == path:
            raise OSError(116, "Stale file handle", str(path))
        return read_text(candidate, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", stale)

    with pytest.raises(SetforgeError, match="cannot read operation journal") as failure:
        operations.load("p")

    assert "move that file aside" not in str(failure.value)


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
        paths=(),
        state_snapshots=(state,),
    )

    assert journal.reserved_profiles == ("actual", "migrate")
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
        paths=(),
        profiles=("team/dev",),
    )

    loaded = operations.load("migrate")

    assert loaded.reserved_profiles == ("migrate", "team/dev")
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


@pytest.mark.parametrize("target", ["real/", "./real"])
def test_install_root_recovery_accepts_an_ancestor_alias_with_a_redundant_target(
    tmp_path: Path, operation_state: Path, target: str
) -> None:
    (tmp_path / "real").mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target)
    root = alias / "root"
    alias_paths, guards = operations.capture_install_parent_guards((root,), (root,))
    journal = operations.prepare(
        command="install",
        profile="p",
        config_dir=tmp_path,
        resources_lock=True,
        paths=(root, *alias_paths),
        path_guards=guards,
    )
    preparing = operations.begin_checkpoint(
        journal,
        name="prepare-target-roots",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore initially absent target roots",
        paths=(root,),
    )
    root.mkdir()
    created = root.stat()
    bound = operations.bind_install_roots(
        preparing, ((root, created.st_dev, created.st_ino, created.st_mode),)
    )

    assert alias_paths == (alias,)
    assert operations.recover_automatically(bound)
    assert not (tmp_path / "real" / "root").exists()
    assert os.readlink(alias) == target  # noqa: PTH115 - raw target text
    assert operations.active("p") is None
    assert {item.path: item.link_target for item in bound.paths}[alias] == "real"


@pytest.mark.parametrize("target", ["real/", "./real", "a//b"])
def test_path_captures_record_the_pathlib_link_target_and_anchored_ones_the_raw(
    tmp_path: Path, target: str
) -> None:
    link = tmp_path / "link"
    link.symlink_to(target)
    recorded = str(Path(target))
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        anchored = operations._snapshot_path_at(parent_fd, link)
        anchored_image = transitions.capture_filesystem_image("link", dir_fd=parent_fd)
    finally:
        os.close(parent_fd)

    assert recorded in {"real", "a/b"}
    assert operations.snapshot_path(link).link_target == recorded
    assert transitions.snapshot_filesystem_image(link).link_target == recorded
    assert anchored.link_target == target
    assert anchored_image is not None
    assert anchored_image.link_target == target


def test_journal_recovers_after_config_ancestor_became_a_symlink(
    tmp_path: Path, operation_state: Path
) -> None:
    config_dir = tmp_path / "real" / "cfg"
    config_dir.mkdir(parents=True)
    path = tmp_path / "live.txt"
    path.write_text("before", encoding="utf-8")
    journal = operations.begin_checkpoint(
        operations.prepare(
            command="sync",
            profile="p",
            config_dir=config_dir,
            resources_lock=False,
            paths=(path,),
        ),
        name="files",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore files",
    )
    path.write_text("after", encoding="utf-8")
    (tmp_path / "real").rename(tmp_path / "moved")
    (tmp_path / "real").symlink_to("moved")

    operations.refuse_conflicting_mutation(
        resources=False, config_dir=None, profile="other"
    )
    with pytest.raises(SetforgeError, match="unfinished sync operation"):
        operations.refuse_conflicting_mutation(
            resources=False, config_dir=config_dir, profile=None
        )
    assert operations.load("p") == journal
    assert operations.recover_automatically(journal)

    assert path.read_text(encoding="utf-8") == "before"
    assert operations.active("p") is None


def test_journal_reports_retryable_error_after_state_ancestor_became_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "real" / "state"
    state_root.mkdir(parents=True)
    monkeypatch.setattr(transitions, "state_root", lambda: state_root)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = tmp_path / "live.txt"
    path.write_text("before", encoding="utf-8")
    journal = operations.begin_checkpoint(
        _prepare(tmp_path, paths=(path,)),
        name="files",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore files",
    )
    path.write_text("after", encoding="utf-8")
    (tmp_path / "real").rename(tmp_path / "moved")
    (tmp_path / "real").symlink_to("moved")

    assert operations.load("p") == journal
    with pytest.raises(SetforgeError, match="SETFORGE_STATE_DIR"):
        operations.recover_automatically(journal)

    (tmp_path / "real").unlink()
    (tmp_path / "moved").rename(tmp_path / "real")
    assert operations.recover_automatically(journal)
    assert path.read_text(encoding="utf-8") == "before"


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
