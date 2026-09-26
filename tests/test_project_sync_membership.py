from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import app
from setforge.project_injection import manifest_path
from setforge.project_overlay import overlay_path
from setforge.project_sync import apply_sync, plan_sync
from tests.test_project_sync import _config, _git_repo
from tests.test_project_visibility import (
    _candidate_filter_entrypoint as _candidate_filter_entrypoint,
)
from tests.test_project_visibility import _git


def _inject_overlay(tmp_path: Path, visibility: str = "hidden") -> tuple[Path, Path]:
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    (target / "AGENTS.md").write_text("team\n")
    _git(target, "add", "AGENTS.md")
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
    _git(target, "config", "test.membership", "preserved")
    (target / ".git/info/attributes").write_text("*.keep -text\n")
    injected = CliRunner().invoke(
        app,
        [
            "project",
            "inject",
            "demo",
            str(target),
            "--config",
            str(config),
            f"--git-{visibility}",
            "--auto=use-profile",
            "--yes",
        ],
    )
    assert injected.exit_code == 0, injected.output
    return config, target


def test_sync_reconciles_hidden_members_after_an_ordinary_visible_member(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    original_config = config.read_text()
    target = _git_repo(tmp_path / "target")
    exclude = target / ".git/info/exclude"
    exclude.write_text("# user excludes\n*.private\n")
    runner = CliRunner()
    injected = runner.invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.output
    tracked = runner.invoke(
        app,
        ["project", "visibility", str(target), "AGENTS.md", "--tracked", "--yes"],
    )
    assert tracked.exit_code == 0, tracked.output
    (config.parent / "project/demo/ZZZ.md").write_text("hidden member\n")
    config.write_text(
        original_config + "      extra:\n        src: ZZZ.md\n        dst: ZZZ.md\n"
    )

    assert apply_sync(plan_sync(target)) is True
    assert (target / "ZZZ.md").read_text() == "hidden member\n"
    assert _git(target, "status", "--porcelain", "--untracked-files=all") == (
        "?? AGENTS.md\n"
    )
    assert apply_sync(plan_sync(target)) is False

    config.write_text(original_config)
    assert apply_sync(plan_sync(target)) is True
    assert not (target / "ZZZ.md").exists()
    assert apply_sync(plan_sync(target)) is False
    (target / "ZZZ.md").write_text("new user file\n")
    (target / "user.private").write_text("still ignored\n")
    assert _git(target, "status", "--porcelain", "--untracked-files=all") == (
        "?? AGENTS.md\n?? ZZZ.md\n"
    )
    assert exclude.read_text() == "# user excludes\n*.private\n"


@pytest.mark.parametrize("visibility", ["hidden", "tracked"])
def test_sync_removes_overlay_state_and_preserves_unrelated_git_settings(
    tmp_path: Path, visibility: str
) -> None:
    config, target = _inject_overlay(tmp_path, visibility)
    private_overlay = overlay_path(target, Path("AGENTS.md"))
    assert private_overlay.exists()
    assert (target / "AGENTS.md").read_text() == "managed\n"
    if visibility == "hidden":
        assert _git(target, "diff", "--", "AGENTS.md") == ""
    else:
        assert "+managed\n" in _git(target, "diff", "--", "AGENTS.md")
    config.write_text(
        "tracked_files: {}\nprofiles: {}\nproject_profiles:\n  demo:\n    files: {}\n"
    )

    assert apply_sync(plan_sync(target)) is True

    assert (target / "AGENTS.md").read_text() == "team\n"
    assert not private_overlay.exists()
    assert _git(target, "diff", "--", "AGENTS.md") == ""
    assert (target / ".git/info/attributes").read_text() == "*.keep -text\n"
    assert _git(target, "config", "--get", "test.membership") == "preserved\n"
    assert json.loads(manifest_path(target, "demo").read_bytes())["files"] == []
    assert apply_sync(plan_sync(target)) is False


def test_sync_migrates_legacy_tracked_overlay_to_visible_git_content(
    tmp_path: Path,
) -> None:
    _config_path, target = _inject_overlay(tmp_path)
    record = manifest_path(target, "demo")
    raw = json.loads(record.read_bytes())
    raw["schema"] = 2
    raw["visibility"] = "tracked"
    for entry in raw["files"]:
        entry.pop("visibility")
    record.write_text(json.dumps(raw, separators=(",", ":"), sort_keys=True) + "\n")
    assert _git(target, "diff", "--", "AGENTS.md") == ""

    assert apply_sync(plan_sync(target)) is True

    migrated = json.loads(record.read_bytes())
    assert migrated["schema"] == 3
    assert migrated["files"][0]["visibility"] == "tracked"
    assert (target / "AGENTS.md").read_text() == "managed\n"
    assert "+managed\n" in _git(target, "diff", "--", "AGENTS.md")
    assert overlay_path(target, Path("AGENTS.md")).exists()
    assert (target / ".git/info/attributes").read_text() == "*.keep -text\n"
    assert _git(target, "config", "--get", "test.membership") == "preserved\n"


def test_sync_without_overlays_preserves_unused_filter_settings(tmp_path: Path) -> None:
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    attributes = target / ".git/info/attributes"
    attributes.write_text("*.keep -text\n")
    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.output
    _git(target, "config", "filter.setforge-project.process", "user-owned-filter")
    config_before = (target / ".git/config").read_bytes()
    (config.parent / "project/demo/AGENTS.md").write_text("updated\n")

    assert apply_sync(plan_sync(target)) is True
    assert (target / "AGENTS.md").read_text() == "updated\n"
    assert apply_sync(plan_sync(target)) is False
    assert (target / ".git/config").read_bytes() == config_before
    assert attributes.read_text() == "*.keep -text\n"
    assert _git(target, "status", "--porcelain", "--untracked-files=all") == ""


def test_sync_retains_empty_parents_of_a_locally_deleted_member(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.write_text(
        config.read_text().replace("dst: AGENTS.md", "dst: nested/deeper/AGENTS.md")
    )
    target = _git_repo(tmp_path / "target")
    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.output
    destination = target / "nested/deeper/AGENTS.md"
    destination.unlink()

    assert apply_sync(plan_sync(target)) is True
    assert not destination.exists()
    assert destination.parent.is_dir()
    assert apply_sync(plan_sync(target)) is False
    assert destination.parent.is_dir()

    config.write_text(
        "tracked_files: {}\nprofiles: {}\nproject_profiles:\n  demo:\n    files: {}\n"
    )
    assert apply_sync(plan_sync(target)) is True
    assert not (target / "nested").exists()
    assert apply_sync(plan_sync(target)) is False


@pytest.mark.parametrize("git_target", [False, True])
def test_sync_reports_content_mode_removal_and_repeated_noops(
    tmp_path: Path, git_target: bool
) -> None:
    config = _config(tmp_path)
    source = config.parent / "project/demo/AGENTS.md"
    target = tmp_path / "target"
    if git_target:
        _git_repo(target)
    else:
        target.mkdir()
    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.output
    assert apply_sync(plan_sync(target)) is False

    source.write_text("updated\n")
    assert apply_sync(plan_sync(target)) is True
    assert (target / "AGENTS.md").read_text() == "updated\n"
    assert apply_sync(plan_sync(target)) is False

    source.chmod(0o755)
    assert apply_sync(plan_sync(target)) is True
    assert stat.S_IMODE((target / "AGENTS.md").stat().st_mode) == 0o755
    assert apply_sync(plan_sync(target)) is False

    config.write_text(
        "tracked_files: {}\nprofiles: {}\nproject_profiles:\n  demo:\n    files: {}\n"
    )
    assert apply_sync(plan_sync(target)) is True
    assert not (target / "AGENTS.md").exists()
    assert apply_sync(plan_sync(target)) is False


def test_sync_reports_repair_of_private_overlay_permissions(tmp_path: Path) -> None:
    _config_path, target = _inject_overlay(tmp_path)
    apply_sync(plan_sync(target))
    record = manifest_path(target, "demo")
    manifest_before = record.read_bytes()
    private_overlay = overlay_path(target, Path("AGENTS.md"))
    overlay_before = private_overlay.read_bytes()
    private_overlay.chmod(0o644)

    assert apply_sync(plan_sync(target)) is True

    assert stat.S_IMODE(private_overlay.stat().st_mode) == 0o600
    assert private_overlay.read_bytes() == overlay_before
    assert record.read_bytes() == manifest_before
    assert (target / "AGENTS.md").read_text() == "managed\n"
    assert _git(target, "diff", "--", "AGENTS.md") == ""


def test_sync_reports_yaml_reformatting_without_a_manifest_change(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.write_text(
        config.read_text().replace("dst: AGENTS.md", "dst: settings.yaml")
    )
    canonical = "section:\n  flag: true\n"
    (config.parent / "project/demo/AGENTS.md").write_text(canonical)
    target = _git_repo(tmp_path / "target")
    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.output
    assert apply_sync(plan_sync(target)) is False
    record = manifest_path(target, "demo")
    manifest_before = record.read_bytes()
    destination = target / "settings.yaml"
    destination.write_text("section:\n    flag: true\n")

    assert apply_sync(plan_sync(target)) is True

    assert destination.read_text() == canonical
    assert record.read_bytes() == manifest_before
    assert apply_sync(plan_sync(target)) is False
