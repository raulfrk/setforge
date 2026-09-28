"""CLI integration tests for explicit interrupted-operation recovery."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from setforge import locking, operations, transitions
from setforge.cli import app
from setforge.cli import recover as recover_cli
from setforge.errors import SetforgeError


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


@pytest.mark.parametrize(
    "boundary",
    ["planned", "intermediate", "removed", "unchanged", "stale", "legacy", "context"],
)
def test_public_mcp_recovery_uses_exact_evidence_before_effects(
    runner: CliRunner,
    tmp_path: Path,
    recovery_state: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    import json

    from setforge import mcp_servers

    assert recovery_state.resolve().is_relative_to(tmp_path.resolve())
    assert locking._user_global_locks_dir().resolve().is_relative_to(tmp_path.resolve())
    old = (["old", "argument with spaces", "--flag"], "user")
    new = (["new", 'quote"', "slash\\"], "project")
    middle = (["middle"], "local")
    control = (["keep"], "user")
    current = {"control": control}
    if boundary != "removed":
        current["named"] = (
            old
            if boundary == "unchanged"
            else middle
            if boundary == "intermediate"
            else (["unexpected"], "project")
            if boundary == "stale"
            else new
        )
    effects: list[tuple[str, str]] = []

    def remove(name: str, *, scope: str) -> None:
        assert current[name][1] == scope
        effects.append(("remove", name))
        current.pop(name)

    def add(name: str, ref) -> None:
        assert name not in current
        effects.append(("add", name))
        current[name] = (list(ref.command), ref.scope)

    monkeypatch.setattr(mcp_servers, "mcp_get_command", current.get)
    monkeypatch.setattr(mcp_servers, "mcp_remove", remove)
    monkeypatch.setattr(mcp_servers, "mcp_add", add)
    context = mcp_servers.inventory_context()
    row: dict[str, Any] = {
        "name": "named",
        "prior": old,
        "planned": [new, middle],
        "context": context,
    }
    if boundary == "legacy":
        row.pop("context")
    elif boundary == "context":
        row["context"] = ("/different-cwd", context[1], "/different-cwd")
    journal = operations.prepare(
        command="revert",
        profile="p",
        config_dir=tmp_path,
        resources_lock=True,
        command_line=(),
        paths=(),
        adapters=(
            operations.AdapterSnapshot(operations.AdapterKind.MCP, json.dumps([row])),
        ),
    )
    journal = operations.begin_checkpoint(
        journal,
        name="mcp",
        kind=operations.CheckpointKind.COMPENSATABLE,
        recovery="restore MCP",
        adapters=(operations.AdapterKind.MCP,),
    )
    before = dict(current)
    result = runner.invoke(app, ["recover", "--profile=p", "--apply", "--yes"])
    if boundary in {"stale", "legacy", "context"}:
        assert result.exit_code != 0
        assert current == before
        assert effects == []
        assert operations.active("p") is not None
    else:
        assert result.exit_code == 0, (result.output, result.exception)
        assert current == {"named": old, "control": control}
        assert operations.active("p") is None
        if boundary == "unchanged":
            assert effects == []


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


@pytest.mark.parametrize(
    "parent_state",
    ["replaced", "missing", "absent-file", "created-then-replaced", "inspect-error"],
)
def test_recover_rejects_changed_install_parent_before_adapter_effects(
    runner: CliRunner,
    tmp_path: Path,
    recovery_state: Path,
    monkeypatch: pytest.MonkeyPatch,
    parent_state: str,
) -> None:
    assert recovery_state.resolve().is_relative_to(tmp_path.resolve())
    parent = tmp_path / "live"
    parent.mkdir()
    path = parent / "file"
    path.write_text("before", encoding="utf-8")
    info = parent.stat()
    absent_parent = tmp_path / "a-absent"
    absent_path = absent_parent / "newfile"
    assert absent_path.resolve().is_relative_to(tmp_path.resolve())
    journal = operations.prepare(
        command="install",
        profile="p",
        config_dir=tmp_path,
        resources_lock=False,
        command_line=("install", "--profile=p"),
        paths=(absent_path, path),
        path_guards=(
            operations.PathGuard(absent_parent, None, None, None),
            operations.PathGuard(parent, info.st_dev, info.st_ino, info.st_mode),
        ),
    )
    operations.begin_checkpoint(
        journal,
        name="files",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore files",
    )
    original = tmp_path / "original-live"
    if parent_state == "inspect-error":
        original_lstat = Path.lstat

        def unreadable(candidate: Path) -> os.stat_result:
            if candidate == parent:
                raise PermissionError("injected parent inspection failure")
            return original_lstat(candidate)

        monkeypatch.setattr(Path, "lstat", unreadable)
    elif parent_state == "absent-file":
        absent_parent.write_text("foreign replacement", encoding="utf-8")
    else:
        if parent_state == "created-then-replaced":
            absent_parent.mkdir()
        parent.rename(original)
        if parent_state in {"replaced", "created-then-replaced"}:
            parent.mkdir()
            path.write_text("unrelated replacement", encoding="utf-8")
    adapter_calls: list[bool] = []
    monkeypatch.setattr(
        operations, "recover_adapters", lambda _journal: adapter_calls.append(True)
    )

    result = runner.invoke(app, ["recover", "--profile=p", "--apply", "--yes"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SetforgeError)
    expected = (
        "journaled absent parent changed before recovery"
        if parent_state == "absent-file"
        else "journaled path parent changed before recovery"
    )
    assert expected in str(result.exception)
    assert adapter_calls == []
    if parent_state in {"replaced", "created-then-replaced"}:
        assert path.read_text(encoding="utf-8") == "unrelated replacement"
        if parent_state == "created-then-replaced":
            assert absent_parent.is_dir()
    elif parent_state == "missing":
        assert not parent.exists()
    else:
        if parent_state == "absent-file":
            assert absent_parent.read_text(encoding="utf-8") == "foreign replacement"
        assert path.read_text(encoding="utf-8") == "before"
    if parent_state not in {"absent-file", "inspect-error"}:
        assert (original / "file").read_text(encoding="utf-8") == "before"
    assert operations.active("p") is not None


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
