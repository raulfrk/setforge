"""CLI integration tests for explicit interrupted-operation recovery."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from setforge import locking, operations, transitions
from setforge.cli import app
from setforge.cli import recover as recover_cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def recovery_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "state"
    monkeypatch.setattr(transitions, "state_root", lambda: root)
    monkeypatch.setattr("setforge.locking.state_root", lambda: root)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return root


def _prepare(tmp_path: Path, path: Path) -> operations.OperationJournal:
    journal = operations.prepare(
        command="sync",
        profile="p",
        config_dir=tmp_path,
        resources_lock=False,
        command_line=("sync", "--profile=p"),
        paths=(path,),
    )
    return operations.begin_checkpoint(
        journal,
        name="files",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore files",
    )


def test_recover_inspection_is_read_only(
    runner: CliRunner, tmp_path: Path, recovery_state: Path
) -> None:
    path = tmp_path / "tracked"
    path.write_text("before", encoding="utf-8")
    journal = _prepare(tmp_path, path)
    before = operations.journal_path("p").read_bytes()

    result = runner.invoke(app, ["recover", "--profile", "p"])

    assert result.exit_code == 0, result.output
    assert journal.operation_id in result.output
    assert "recover with:" in result.output
    assert operations.journal_path("p").read_bytes() == before


def test_recover_apply_restores_and_clears_journal(
    runner: CliRunner, tmp_path: Path, recovery_state: Path
) -> None:
    path = tmp_path / "tracked"
    path.write_text("before", encoding="utf-8")
    journal = _prepare(tmp_path, path)
    path.write_text("after", encoding="utf-8")

    result = runner.invoke(app, ["recover", "--profile", "p", "--apply", "--yes"])

    assert result.exit_code == 0, result.output
    assert f"recovered operation {journal.operation_id}" in result.output
    assert path.read_text(encoding="utf-8") == "before"
    assert operations.active("p") is None


def test_recover_locks_every_profile_named_by_state_snapshots(
    tmp_path: Path,
    recovery_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = transitions.StateSnapshotEntry(
        store=transitions.SnapshotStore.BASE,
        profile="actual",
        key="file",
        payload=None,
    )
    journal = operations.prepare(
        command="revert",
        profile="migrate",
        config_dir=tmp_path,
        config_dirs=(tmp_path / "host-local",),
        resources_lock=False,
        command_line=("revert",),
        paths=(),
        state_snapshots=(state,),
    )
    journal = operations.begin_checkpoint(
        journal,
        name="stores",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore stores",
    )
    acquired: list[tuple[tuple[str, ...], tuple[Path, ...]]] = []
    real_locks = locking.mutation_locks

    @contextmanager
    def recording_locks(**kwargs: Any) -> Iterator[None]:
        acquired.append((kwargs["profiles"], kwargs["config_dirs"]))
        with real_locks(**kwargs):
            yield

    monkeypatch.setattr(recover_cli, "mutation_locks", recording_locks)

    recover_cli._apply_recovery(journal)

    assert acquired == [
        (
            ("actual", "migrate"),
            tuple(
                sorted(
                    (tmp_path.resolve(), (tmp_path / "host-local").resolve()),
                    key=str,
                )
            ),
        )
    ]
    assert operations.active("migrate") is None


def test_recover_requires_yes_without_tty(
    runner: CliRunner, tmp_path: Path, recovery_state: Path
) -> None:
    path = tmp_path / "tracked"
    path.write_text("before", encoding="utf-8")
    _prepare(tmp_path, path)

    result = runner.invoke(app, ["recover", "--profile", "p", "--apply"])

    assert result.exit_code == 1
    assert result.exception is not None
    assert "requires --yes" in str(result.exception)
    assert operations.active("p") is not None


@pytest.mark.parametrize("completed", [False, True])
def test_irreversible_checkpoint_retains_manual_journal(
    runner: CliRunner,
    tmp_path: Path,
    recovery_state: Path,
    completed: bool,
) -> None:
    path = tmp_path / "tracked"
    path.write_text("before", encoding="utf-8")
    journal = operations.finish_checkpoint(_prepare(tmp_path, path))
    journal = operations.begin_checkpoint(
        journal,
        name="packages",
        kind=operations.CheckpointKind.IRREVERSIBLE,
        recovery="inspect package receipts",
    )
    if completed:
        operations.finish_checkpoint(journal)
    path.write_text("after", encoding="utf-8")

    result = runner.invoke(app, ["recover", "--profile", "p", "--apply", "--yes"])

    assert result.exit_code == 1
    assert "manual remediation remains" in result.output
    assert "inspect package receipts" in result.output
    assert path.read_text(encoding="utf-8") == "before"
    assert operations.load("p").phase is operations.OperationPhase.MANUAL


def test_manual_recovery_requires_explicit_acknowledgement(
    runner: CliRunner, tmp_path: Path, recovery_state: Path
) -> None:
    path = tmp_path / "tracked"
    path.write_text("before", encoding="utf-8")
    journal = _prepare(tmp_path, path)
    operations.mark_manual(journal)

    repeated = runner.invoke(app, ["recover", "--profile", "p", "--apply", "--yes"])
    assert repeated.exit_code == 1
    assert "already completed" in str(repeated.exception)

    refused = runner.invoke(app, ["recover", "--profile", "p", "--acknowledge-manual"])
    assert refused.exit_code == 1
    assert operations.active("p") is not None

    accepted = runner.invoke(
        app,
        ["recover", "--profile", "p", "--acknowledge-manual", "--yes"],
    )
    assert accepted.exit_code == 0, accepted.output
    assert "acknowledged manual recovery" in accepted.output
    assert operations.active("p") is None
