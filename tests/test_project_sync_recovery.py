from __future__ import annotations

import base64
import json
import stat
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge import operations
from setforge.cli import app
from setforge.project_overlay import overlay_path
from setforge.project_sync import (
    AutoResolution,
    apply_sync,
    plan_sync,
    resolve_sync_plan,
)
from tests.test_project_sync import _config, _git_repo


def _file_state(path: Path) -> tuple[bytes, int] | None:
    if not path.exists():
        return None
    return path.read_bytes(), stat.S_IMODE(path.stat().st_mode)


def _private_files(state: Path) -> dict[Path, tuple[bytes, int] | None]:
    return {
        path.relative_to(state): _file_state(path)
        for path in state.rglob("*")
        if path.is_file() and "locks" not in path.relative_to(state).parts
    }


@pytest.mark.parametrize("change", ["add", "update", "remove"])
def test_late_sync_failure_restores_overlays_and_git_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    config = _config(tmp_path)
    original_config = config.read_text()
    extra_members = (
        "      extra:\n        src: EXTRA.md\n        dst: EXTRA.md\n"
        "      note:\n        src: NOTE.md\n        dst: NOTE.md\n"
    )
    source = config.parent / "project" / "demo"
    for name in ("EXTRA.md", "NOTE.md"):
        (source / name).write_text(f"old managed {name}\n")
        (source / name).chmod(0o640)
    if change != "add":
        config.write_text(original_config + extra_members)
    target = _git_repo(tmp_path / "target")
    (target / "EXTRA.md").write_text("team content\n")
    (target / "EXTRA.md").chmod(0o644)
    subprocess.run(["git", "add", "EXTRA.md"], cwd=target, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "team baseline",
        ],
        cwd=target,
        check=True,
    )
    injected = CliRunner().invoke(
        app,
        [
            "project",
            "inject",
            "demo",
            str(target),
            "--config",
            str(config),
            "--auto=use-profile",
            "--yes",
        ],
    )
    assert injected.exit_code == 0, injected.exception
    config.write_text(
        original_config if change == "remove" else original_config + extra_members
    )
    for name in ("EXTRA.md", "NOTE.md"):
        (source / name).write_text(f"new managed {name}\n")
        (source / name).chmod(0o750)
    plan = resolve_sync_plan(plan_sync(target), auto=AutoResolution.USE_PROFILE)
    assert plan is not None
    tracked_paths = (
        target / "AGENTS.md",
        target / "EXTRA.md",
        target / "NOTE.md",
        target / ".git" / "config",
        target / ".git" / "index",
        target / ".git" / "info" / "attributes",
        target / ".git" / "info" / "exclude",
    )
    before_files = {path: _file_state(path) for path in tracked_paths}
    before_state = _private_files(state)
    overlay = overlay_path(target, Path("EXTRA.md"))
    overlay_parent_existed = overlay.parent.exists()
    journals: list[operations.OperationJournal] = []

    def fail_after_effects(journal: operations.OperationJournal) -> None:
        journals.append(operations.load(journal.profile))
        if change == "remove":
            assert not overlay.exists()
            assert (target / "EXTRA.md").read_bytes() == b"team content\n"
            assert not (target / "NOTE.md").exists()
        else:
            payload = json.loads(overlay.read_bytes())
            assert base64.b64decode(payload["local"]) == b"new managed EXTRA.md\n"
            assert stat.S_IMODE(overlay.stat().st_mode) == 0o600
            assert _file_state(target / "EXTRA.md") == (
                b"new managed EXTRA.md\n",
                0o750,
            )
        if change != "update":
            exclude = target / ".git" / "info" / "exclude"
            assert _file_state(exclude) != before_files[exclude]
        raise OSError("injected late sync failure")

    monkeypatch.setattr(operations, "finish_checkpoint", fail_after_effects)

    with pytest.raises(OSError, match="injected late sync failure") as failure:
        apply_sync(plan)

    assert not getattr(failure.value, "__notes__", ())
    assert len(journals) == 1
    assert operations.active(journals[0].profile) is None
    assert {path: _file_state(path) for path in tracked_paths} == before_files
    assert _private_files(state) == before_state
    assert overlay.parent.exists() is overlay_parent_existed


def test_membership_failure_restores_files_and_limits_directory_recovery_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    config = _config(tmp_path)
    kept_config = config.read_text().replace(
        "dst: AGENTS.md", "dst: kept/deeper/AGENTS.md"
    )
    config.write_text(
        kept_config
        + "      extra:\n        src: EXTRA.md\n        dst: removed/deeper/EXTRA.md\n"
    )
    source = config.parent / "project" / "demo"
    (source / "EXTRA.md").write_text("removed member\n")
    (source / "EXTRA.md").chmod(0o640)
    target = _git_repo(tmp_path / "target")
    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.exception
    removed_parents = (target / "removed", target / "removed" / "deeper")
    for parent, mode in zip(removed_parents, (0o751, 0o710), strict=True):
        parent.chmod(mode)
    config.write_text(
        kept_config
        + "      fresh:\n        src: FRESH.md\n        dst: added/deeper/FRESH.md\n"
    )
    (source / "AGENTS.md").write_text("updated kept member\n")
    (source / "FRESH.md").write_text("new member\n")
    (source / "FRESH.md").chmod(0o750)
    plan = plan_sync(target)
    tracked_paths = (
        target / "kept" / "deeper" / "AGENTS.md",
        target / "removed" / "deeper" / "EXTRA.md",
        target / ".git" / "info" / "exclude",
    )
    before_files = {path: _file_state(path) for path in tracked_paths}
    before_state = _private_files(state)
    journals: list[operations.OperationJournal] = []

    def fail_before_parent_removal(_guard: object, relative: Path) -> None:
        assert target / relative == removed_parents[-1]
        assert not (target / "removed" / "deeper" / "EXTRA.md").exists()
        assert (target / "added" / "deeper" / "FRESH.md").read_bytes() == (
            b"new member\n"
        )
        journals.extend(
            operations.conflicting_journals(
                resources=True, config_dir=None, profile=None
            )
        )
        raise OSError("injected parent cleanup failure")

    monkeypatch.setattr(
        "setforge.project_sync._remove_created_parent", fail_before_parent_removal
    )

    with pytest.raises(OSError, match="injected parent cleanup failure") as failure:
        apply_sync(plan)

    assert not getattr(failure.value, "__notes__", ())
    assert len(journals) == 1
    journal = journals[0]
    directory_snapshots = {
        item.path: item.mode
        for item in journal.paths
        if item.kind is operations.SnapshotKind.DIRECTORY
    }
    assert directory_snapshots == dict(
        zip(removed_parents, (0o751, 0o710), strict=True)
    )
    assert set(map(str, removed_parents)) <= set(journal.checkpoints[0].paths)
    assert {path: _file_state(path) for path in tracked_paths} == before_files
    assert _private_files(state) == before_state
    assert not (target / "added").exists()
    assert {
        parent: stat.S_IMODE(parent.stat().st_mode) for parent in removed_parents
    } == directory_snapshots
    assert operations.active(journal.profile) is None


def test_sync_recovery_preserves_replacement_parent_and_retains_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    config = _config(tmp_path)
    config.write_text(
        config.read_text().replace("dst: AGENTS.md", "dst: nested/AGENTS.md")
    )
    target = _git_repo(tmp_path / "target")
    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.exception
    (config.parent / "project" / "demo" / "AGENTS.md").write_text("updated\n")
    plan = plan_sync(target)
    moved_parent = target / "preserved-parent"
    replacement = target / "nested" / "AGENTS.md"
    journals: list[operations.OperationJournal] = []
    after_state: dict[Path, tuple[bytes, int] | None] = {}

    def replace_parent_then_fail(journal: operations.OperationJournal) -> None:
        journals.append(operations.load(journal.profile))
        after_state.update(_private_files(state))
        replacement.parent.rename(moved_parent)
        replacement.parent.mkdir()
        replacement.write_bytes(b"unrelated replacement\n")
        replacement.chmod(0o600)
        raise OSError("injected failure after parent replacement")

    monkeypatch.setattr(operations, "finish_checkpoint", replace_parent_then_fail)

    with pytest.raises(OSError, match="failure after parent replacement") as failure:
        apply_sync(plan)

    assert len(journals) == 1
    assert any(
        "journaled path parent changed before recovery" in note
        for note in getattr(failure.value, "__notes__", ())
    )
    assert _file_state(replacement) == (b"unrelated replacement\n", 0o600)
    assert _file_state(moved_parent / "AGENTS.md") == (b"updated\n", 0o644)
    assert _private_files(state) == after_state
    assert operations.active(journals[0].profile) == journals[0]
    snapshot = next(item for item in journals[0].paths if item.path == replacement)
    assert snapshot.payload == b"managed\n"
    assert snapshot.mode == 0o644
