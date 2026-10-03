from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge import operations
from setforge.cli import app
from setforge.ownership import resolve_owner_common_dir
from setforge.project_injection import manifest_path
from tests.test_project_sync import _config, _git_repo
from tests.test_project_sync_recovery import _file_state, _private_files
from tests.test_project_visibility import (
    _candidate_filter_entrypoint as _candidate_filter_entrypoint,
)
from tests.test_project_visibility import _git


@pytest.mark.parametrize("git_target", [False, True])
@pytest.mark.parametrize("entry", ["directory", "fifo", "ancestor-symlink"])
def test_injection_rejects_unsafe_destination_before_any_write(
    tmp_path: Path, git_target: bool, entry: str
) -> None:
    config = _config(tmp_path)
    config.write_text(
        config.read_text()
        + "      unsafe:\n        src: AGENTS.md\n        dst: unsafe/deeper/file\n"
    )
    target = tmp_path / "target"
    if git_target:
        _git_repo(target)
    else:
        target.mkdir()
    outside = tmp_path / "outside"
    (outside / "deeper").mkdir(parents=True)
    (outside / "deeper/file").write_bytes(b"outside must survive\n")
    if entry == "ancestor-symlink":
        (target / "unsafe").symlink_to(outside, target_is_directory=True)
    else:
        (target / "unsafe/deeper").mkdir(parents=True)
        if entry == "directory":
            (target / "unsafe/deeper/file").mkdir()
        else:
            os.mkfifo(target / "unsafe/deeper/file")
    state = tmp_path / "state"
    private_before = _private_files(state)
    git_before = _private_files(target / ".git") if git_target else {}
    owner_path = resolve_owner_common_dir(config.parent) / "setforge/owner-id"
    assert not owner_path.exists()

    result = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )

    assert result.exit_code == 1, result.output
    assert not (target / "AGENTS.md").exists()
    assert (outside / "deeper/file").read_bytes() == b"outside must survive\n"
    assert not owner_path.exists()
    assert _private_files(state) == private_before
    if git_target:
        assert _private_files(target / ".git") == git_before


@pytest.mark.parametrize("change", ["remove", "rename"])
def test_missing_recorded_profile_refuses_sync_but_allows_explicit_removal(
    tmp_path: Path, change: str
) -> None:
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    original = config.read_text()
    runner = CliRunner()
    injected = runner.invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.output
    config.write_text(
        "tracked_files: {}\nprofiles: {}\nproject_profiles: {}\n"
        if change == "remove"
        else original.replace("  demo:", "  renamed:")
    )
    state_before = _private_files(tmp_path / "state")
    assert state_before
    git_before = _private_files(target / ".git")

    synced = runner.invoke(app, ["project", "sync", str(target), "--yes"])

    assert synced.exit_code == 1, synced.output
    assert "demo" in str(synced.exception)
    assert (target / "AGENTS.md").read_text() == "managed\n"
    assert _private_files(tmp_path / "state") == state_before
    assert _private_files(target / ".git") == git_before
    removed = runner.invoke(
        app,
        ["project", "remove", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert removed.exit_code == 0, removed.output
    assert not (target / "AGENTS.md").exists()
    assert not manifest_path(target, "demo").exists()


@pytest.mark.parametrize("initially_empty", [False, True])
def test_project_list_retains_injections_with_no_members(
    tmp_path: Path, initially_empty: bool
) -> None:
    config = _config(tmp_path)
    empty_config = (
        "tracked_files: {}\nprofiles: {}\nproject_profiles:\n  demo:\n    files: {}\n"
    )
    if initially_empty:
        config.write_text(empty_config)
    target = _git_repo(tmp_path / "target")
    runner = CliRunner()
    injected = runner.invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.output
    if not initially_empty:
        config.write_text(empty_config)
        synced = runner.invoke(app, ["project", "sync", str(target), "--yes"])
        assert synced.exit_code == 0, synced.output
    before = _private_files(tmp_path / "state")
    assert before

    listed = runner.invoke(app, ["project", "list"])

    assert listed.exit_code == 0, listed.output
    assert f"{target}  [demo]" in listed.output
    assert "no files" in listed.output
    assert _private_files(tmp_path / "state") == before
    removed = runner.invoke(
        app,
        ["project", "remove", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert removed.exit_code == 0, removed.output
    assert not manifest_path(target, "demo").exists()
    assert runner.invoke(app, ["project", "list"]).output == (
        "no project injections recorded\n"
    )


@pytest.mark.parametrize("alias", ["target", "parent"])
def test_project_commands_accept_symlinked_path_and_use_the_real_target(
    tmp_path: Path, alias: str
) -> None:
    config = _config(tmp_path)
    (tmp_path / "real").mkdir()
    target = _git_repo(tmp_path / "real" / "target")
    if alias == "target":
        spelled = tmp_path / "alias"
        spelled.symlink_to(target, target_is_directory=True)
    else:
        (tmp_path / "work").symlink_to(tmp_path / "real", target_is_directory=True)
        spelled = tmp_path / "work" / "target"
    runner = CliRunner()

    injected = runner.invoke(
        app,
        ["project", "inject", "demo", str(spelled), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.output
    assert f"target: {target}\n" in injected.output
    assert manifest_path(target, "demo").exists()
    assert not manifest_path(spelled, "demo").exists()
    listed = runner.invoke(app, ["project", "list"])
    assert listed.output == f"{target}  [demo]\n  hidden: AGENTS.md\n"
    synced = runner.invoke(app, ["project", "sync", str(spelled), "--dry-run"])
    assert synced.exit_code == 0, synced.output
    assert f"target: {target}\n" in synced.output
    visible = runner.invoke(
        app, ["project", "visibility", str(spelled), "AGENTS.md", "--tracked", "--yes"]
    )
    assert visible.exit_code == 0, visible.output
    removed = runner.invoke(
        app,
        ["project", "remove", "demo", str(spelled), "--config", str(config), "--yes"],
    )
    assert removed.exit_code == 0, removed.output
    assert not (target / "AGENTS.md").exists()
    assert not manifest_path(target, "demo").exists()


@pytest.mark.parametrize("path_kind", ["subdirectory", "unrecorded-worktree"])
def test_project_commands_refuse_wrong_target_without_touching_recorded_project(
    tmp_path: Path, path_kind: str
) -> None:
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    runner = CliRunner()
    injected = runner.invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.output
    if path_kind == "subdirectory":
        invalid = target / "subdirectory"
        invalid.mkdir()
    else:
        _git(
            target,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "base",
        )
        invalid = tmp_path / "sibling"
        _git(target, "worktree", "add", "-q", "-b", "sibling", str(invalid))
    state_before = _private_files(tmp_path / "state")
    assert state_before
    git_before = _private_files(target / ".git")
    commands = [
        ["project", "sync", str(invalid), "--yes"],
        ["project", "remove", "demo", str(invalid), "--config", str(config), "--yes"],
        ["project", "visibility", str(invalid), "AGENTS.md", "--tracked", "--yes"],
    ]
    if path_kind != "unrecorded-worktree":
        commands.append(
            [
                "project",
                "inject",
                "demo",
                str(invalid),
                "--config",
                str(config),
                "--yes",
            ]
        )

    for command in commands:
        result = runner.invoke(app, command)
        assert result.exit_code == 1, (command, result.output)
        assert (target / "AGENTS.md").read_text() == "managed\n"
        assert _private_files(tmp_path / "state") == state_before
        assert _private_files(target / ".git") == git_before
    listed = runner.invoke(app, ["project", "list"])
    assert listed.exit_code == 0, listed.output
    assert "hidden" in listed.output


@pytest.mark.parametrize("interrupt_recovery", [False, True])
def test_project_sync_recovers_after_uncatchable_process_exit(
    tmp_path: Path, interrupt_recovery: bool
) -> None:
    config = _config(tmp_path)
    ordinary_config = config.read_text()
    overlay_member = "      extra: {src: EXTRA.md, dst: EXTRA.md}\n"
    config.write_text(
        ordinary_config
        + overlay_member
        + "      old: {src: OLD.md, dst: retired/deeper/OLD.md}\n"
    )
    source = config.parent / "project/demo"
    (source / "EXTRA.md").write_text("team\n\nprivate v1\n")
    (source / "OLD.md").write_text("old member\n")
    target = _git_repo(tmp_path / "target")
    (target / "EXTRA.md").write_text("team\n")
    _git(target, "add", "EXTRA.md")
    _git(
        target,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "base",
    )
    (target / ".git/info/attributes").write_text("*.keep -text\n")
    _git(target, "config", "test.recovery", "preserved")
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
    assert injected.exit_code == 0, injected.output
    (target / "retired").chmod(0o751)
    (target / "retired/deeper").chmod(0o710)
    watched = (
        target / "AGENTS.md",
        target / "EXTRA.md",
        target / "retired/deeper/OLD.md",
        target / "added/deeper/NEW.md",
        target / ".git/config",
        target / ".git/index",
        target / ".git/info/exclude",
        target / ".git/info/attributes",
    )
    files_before = {path: _file_state(path) for path in watched}
    state_before = _private_files(tmp_path / "state")
    assert state_before
    config.write_text(
        ordinary_config
        + overlay_member
        + "      new: {src: NEW.md, dst: added/deeper/NEW.md}\n"
    )
    (source / "AGENTS.md").write_text("updated member\n")
    (source / "EXTRA.md").write_text("team\n\nprivate v2\n")
    (source / "NEW.md").write_text("new member\n")
    root = Path(__file__).resolve().parents[1]
    crash = """
import os
from pathlib import Path
import setforge
from setforge import operations
from setforge.cli import main

assert Path(setforge.__file__).resolve().is_relative_to(Path.cwd())
def crash_after_effects(journal):
    os._exit(79)
operations.finish_checkpoint = crash_after_effects
main()
"""
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            crash,
            "project",
            "sync",
            str(target),
            "--auto=use-profile",
            "--yes",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert child.returncode == 79, (child.stdout, child.stderr)
    assert (target / "AGENTS.md").read_text() == "updated member\n"
    assert (target / "EXTRA.md").read_text() == "team\n\nprivate v2\n"
    assert not (target / "retired").exists()
    assert (target / "added/deeper/NEW.md").read_text() == "new member\n"
    journals = list(operations.journals_root().glob("*.json"))
    assert len(journals) == 1
    profile = json.loads(journals[0].read_bytes())["profile"]

    if interrupt_recovery:
        crash_recovery = """
import os
from pathlib import Path
import setforge
from setforge import operations
from setforge.cli import main

assert Path(setforge.__file__).resolve().is_relative_to(Path.cwd())
restore_path = operations._restore_path
def crash_after_restoring_member(snapshot, **kwargs):
    restored = restore_path(snapshot, **kwargs)
    if snapshot.path.name == "OLD.md":
        assert restored
        os._exit(81)
    return restored
operations._restore_path = crash_after_restoring_member
main()
"""
        interrupted = subprocess.run(
            [
                sys.executable,
                "-c",
                crash_recovery,
                "recover",
                f"--profile={profile}",
                "--apply",
                "--yes",
            ],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert interrupted.returncode == 81, (interrupted.stdout, interrupted.stderr)
        restored_member = target / "retired/deeper/OLD.md"
        assert _file_state(restored_member) == files_before[restored_member]
        assert (target / "AGENTS.md").read_text() == "updated member\n"
        assert journals[0].is_file()

    recovered = subprocess.run(
        [
            sys.executable,
            "-m",
            "setforge.cli",
            "recover",
            f"--profile={profile}",
            "--apply",
            "--yes",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)
    assert {path: _file_state(path) for path in watched} == files_before
    assert _private_files(tmp_path / "state") == state_before
    assert (target / "retired").stat().st_mode & 0o7777 == 0o751
    assert (target / "retired/deeper").stat().st_mode & 0o7777 == 0o710
    assert not (target / "added").exists()
    assert not list(operations.journals_root().glob("*.json"))
