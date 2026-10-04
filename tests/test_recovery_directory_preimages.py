"""Recovery may recreate scoped directory preimages, never unrelated parents."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from setforge import operations
from setforge.errors import SetforgeError
from tests.test_operations import _path_guards
from tests.test_operations import operation_state as operation_state


def _removed_tree(
    tmp_path: Path,
    *,
    record: bool = True,
    scope: bool = True,
    command: str = "project-sync",
) -> tuple[operations.OperationJournal, Path]:
    parent = tmp_path / "retired"
    nested = parent / "deeper"
    nested.mkdir(parents=True)
    parent.chmod(0o751)
    nested.chmod(0o710)
    leaf = nested / "file"
    leaf.write_bytes(b"before\n")
    leaf.chmod(0o640)
    paths = (parent, nested, leaf) if record else (leaf,)
    journal = operations.prepare(
        command=command,
        profile="p",
        config_dir=None,
        resources_lock=False,
        paths=paths,
        path_guards=_path_guards(leaf),
    )
    journal = operations.begin_checkpoint(
        journal,
        name="files",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore recorded paths",
        paths=paths if scope else (leaf,),
    )
    leaf.unlink()
    nested.rmdir()
    parent.rmdir()
    return journal, leaf


def _assert_restored(leaf: Path) -> None:
    assert leaf.read_bytes() == b"before\n"
    assert leaf.stat().st_mode & 0o7777 == 0o640
    assert leaf.parent.stat().st_mode & 0o7777 == 0o710
    assert leaf.parent.parent.stat().st_mode & 0o7777 == 0o751


def test_recovery_restores_scoped_directory_preimages(
    tmp_path: Path, operation_state: Path
) -> None:
    journal, leaf = _removed_tree(tmp_path)

    recovered = operations.recover_files(journal)

    _assert_restored(leaf)
    assert recovered.phase is operations.OperationPhase.RECOVERING


def test_install_recovery_restores_scoped_directory_preimages(
    tmp_path: Path, operation_state: Path
) -> None:
    assert operation_state.resolve().is_relative_to(tmp_path.resolve())
    journal, leaf = _removed_tree(tmp_path, command="install")

    recovered = operations.recover_files(journal)

    _assert_restored(leaf)
    assert recovered.phase is operations.OperationPhase.RECOVERING


def test_install_recovery_reports_directory_inspection_failure(
    tmp_path: Path,
    operation_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert operation_state.resolve().is_relative_to(tmp_path.resolve())
    journal, leaf = _removed_tree(tmp_path, command="install")
    parent = leaf.parent.parent
    original_lstat = Path.lstat
    inspections = 0

    def fail_second_inspection(candidate: Path) -> os.stat_result:
        nonlocal inspections
        if candidate == parent:
            inspections += 1
            if inspections == 2:
                raise PermissionError("injected directory inspection failure")
        return original_lstat(candidate)

    monkeypatch.setattr(Path, "lstat", fail_second_inspection)

    with pytest.raises(SetforgeError) as failure:
        operations.recover_files(journal)

    assert str(failure.value) == (
        f"journaled path parent changed before recovery: {parent}"
    )
    assert not parent.exists()
    assert operations.active("p") is not None


@pytest.mark.parametrize(("record", "scope"), [(False, True), (True, False)])
def test_recovery_refuses_missing_parent_without_scoped_preimage(
    tmp_path: Path, operation_state: Path, record: bool, scope: bool
) -> None:
    journal, leaf = _removed_tree(tmp_path, record=record, scope=scope)

    with pytest.raises(SetforgeError, match="parent changed before recovery"):
        operations.recover_files(journal)

    assert not leaf.parent.parent.exists()
    assert operations.load("p") == journal


@pytest.mark.parametrize("foreign_content", [False, True])
def test_recovery_retry_after_directory_recreation(
    tmp_path: Path,
    operation_state: Path,
    monkeypatch: pytest.MonkeyPatch,
    foreign_content: bool,
) -> None:
    journal, leaf = _removed_tree(tmp_path)
    restore = operations._restore_path

    def interrupt(snapshot: operations.PathSnapshot, **kwargs: object) -> bool:
        result = restore(snapshot, **kwargs)  # type: ignore[arg-type]
        if snapshot.path == leaf:
            raise OSError("interrupted after directory recreation")
        return result

    monkeypatch.setattr(operations, "_restore_path", interrupt)
    with pytest.raises(OSError, match="interrupted after directory recreation"):
        operations.recover_files(journal)
    monkeypatch.setattr(operations, "_restore_path", restore)
    resumed = operations.load("p")
    assert resumed.phase is operations.OperationPhase.RECOVERING
    assert leaf.read_bytes() == b"before\n"

    if foreign_content:
        foreign = leaf.parent / "external"
        foreign.write_bytes(b"preserve me\n")
        with pytest.raises(SetforgeError, match="parent changed before recovery"):
            operations.recover_files(resumed)
        assert foreign.read_bytes() == b"preserve me\n"
        assert operations.active("p") is not None
    else:
        operations.recover_files(resumed)
        _assert_restored(leaf)


def test_recovery_restores_the_mode_an_operation_changed_on_a_recorded_directory(
    tmp_path: Path, operation_state: Path
) -> None:
    parent = tmp_path / "kept"
    parent.mkdir()
    parent.chmod(0o751)
    leaf = parent / "file"
    leaf.write_bytes(b"before\n")
    journal = operations.begin_checkpoint(
        operations.prepare(
            command="project-sync",
            profile="p",
            config_dir=None,
            resources_lock=False,
            paths=(parent, leaf),
            path_guards=_path_guards(leaf),
        ),
        name="files",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore recorded paths",
    )
    parent.chmod(0o700)
    leaf.write_bytes(b"after\n")

    operations.validate_recovery(journal)
    operations.recover_files(journal)

    assert parent.stat().st_mode & 0o7777 == 0o751
    assert leaf.read_bytes() == b"before\n"


@pytest.mark.parametrize("replacement", ["directory", "symlink"])
def test_recovery_refuses_replacement_of_recorded_directory(
    tmp_path: Path, operation_state: Path, replacement: str
) -> None:
    journal, leaf = _removed_tree(tmp_path)
    parent = leaf.parent.parent
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "user.txt"
    sentinel.write_bytes(b"untouched\n")
    if replacement == "symlink":
        parent.symlink_to(outside, target_is_directory=True)
    else:
        parent.mkdir(mode=0o751)

    with pytest.raises(SetforgeError, match="parent changed before recovery"):
        operations.recover_files(journal)

    assert sentinel.read_bytes() == b"untouched\n"
    assert not leaf.exists()
    assert operations.load("p") == journal
