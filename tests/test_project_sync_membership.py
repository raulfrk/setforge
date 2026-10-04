from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import app
from setforge.errors import SetforgeError
from setforge.project_injection import manifest_path
from setforge.project_overlay import build_overlay, overlay_path, write_overlay
from setforge.project_sync import apply_sync, plan_sync
from tests.test_project_sync import _config, _git_repo
from tests.test_project_visibility import (
    _candidate_filter_entrypoint as _candidate_filter_entrypoint,
)
from tests.test_project_visibility import _git


def _inject_overlay(
    tmp_path: Path,
    visibility: str = "hidden",
    *,
    source_payload: bytes | None = None,
) -> tuple[Path, Path]:
    config = _config(tmp_path)
    if source_payload is not None:
        (config.parent / "project/demo/AGENTS.md").write_bytes(source_payload)
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


@pytest.mark.parametrize("drift", ["missing-live", "missing-overlay", "mismatch"])
def test_sync_plan_refuses_changed_tracked_overlay_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    state = tmp_path / "state"
    assert state.resolve().is_relative_to(tmp_path.resolve())
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    _config_path, target = _inject_overlay(tmp_path)
    live = target / "AGENTS.md"
    private = overlay_path(target, Path("AGENTS.md"))
    assert live.resolve().is_relative_to(tmp_path.resolve())
    assert private.resolve().is_relative_to(tmp_path.resolve())
    if drift == "missing-live":
        live.unlink()
        expected = "tracked project overlay is absent: AGENTS.md"
    elif drift == "missing-overlay":
        private.unlink()
        expected = "tracked project overlay is missing or mismatched: AGENTS.md"
    else:
        write_overlay(
            build_overlay(target, Path("AGENTS.md"), b"team\n", b"different\n")
        )
        expected = "tracked project overlay is missing or mismatched: AGENTS.md"

    with pytest.raises(SetforgeError) as failure:
        plan_sync(target)
    assert str(failure.value) == expected


def test_overlay_sync_preserves_local_and_profile_prefix_insertions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    assert state.resolve().is_relative_to(tmp_path.resolve())
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    config, target = _inject_overlay(tmp_path)
    source = config.parent / "project" / "demo" / "AGENTS.md"
    live = target / "AGENTS.md"
    assert source.resolve().is_relative_to(tmp_path.resolve())
    assert live.resolve().is_relative_to(tmp_path.resolve())
    source.write_bytes(b"profile\nmanaged\n")
    live.write_bytes(b"local\nmanaged\n")

    plan = plan_sync(target)

    assert plan.conflicts == 0
    assert plan.files[0].result.merged() == b"local\nprofile\nmanaged\n"
    synced = CliRunner().invoke(app, ["project", "sync", str(target), "--yes"])
    assert synced.exit_code == 0, synced.output
    assert live.read_bytes() == b"local\nprofile\nmanaged\n"


def test_overlay_sync_conflicting_edits_use_resolvable_merge_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    assert state.resolve().is_relative_to(tmp_path.resolve())
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    config, target = _inject_overlay(tmp_path, source_payload=b"team\n\nprivate v1\n")
    source = config.parent / "project" / "demo" / "AGENTS.md"
    live = target / "AGENTS.md"
    assert source.resolve().is_relative_to(tmp_path.resolve())
    assert live.resolve().is_relative_to(tmp_path.resolve())
    source.write_bytes(b"profile team\n\nprivate v1\n")
    live.write_bytes(b"local team\n\nprivate v1\n")

    plan = plan_sync(target)

    assert plan.conflicts == 1
    synced = CliRunner().invoke(
        app, ["project", "sync", str(target), "--auto=keep-live", "--yes"]
    )
    assert synced.exit_code == 0, synced.output
    assert live.read_bytes() == b"local team\n\nprivate v1\n"


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


def test_sync_keeps_a_layout_only_yaml_edit_as_install_does(
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
    destination = target / "settings.yaml"
    destination.write_text("section:\n    flag: true\n")

    assert apply_sync(plan_sync(target)) is True

    assert destination.read_text() == "section:\n    flag: true\n"
    assert apply_sync(plan_sync(target)) is False


@pytest.mark.parametrize("git_target", [False, True])
def test_sync_removes_members_with_shared_created_parents(
    tmp_path: Path, git_target: bool
) -> None:
    config = _config(tmp_path)
    config.write_text(
        config.read_text().replace("dst: AGENTS.md", "dst: nested/deeper/AGENTS.md")
        + "      extra:\n        src: AGENTS.md\n        dst: nested/deeper/EXTRA.md\n"
    )
    target = tmp_path / "target"
    if git_target:
        _git_repo(target)
    else:
        target.mkdir()
    unrelated = target / "user.txt"
    unrelated.write_bytes(b"user content\x00\n")
    exclude = target / ".git/info/exclude"
    exclude_before = exclude.read_bytes() if git_target else None
    runner = CliRunner()
    injected = runner.invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.output
    assert sorted(path.name for path in (target / "nested/deeper").iterdir()) == [
        "AGENTS.md",
        "EXTRA.md",
    ]
    config.write_text(
        "tracked_files: {}\nprofiles: {}\nproject_profiles:\n  demo:\n    files: {}\n"
    )

    synced = runner.invoke(app, ["project", "sync", str(target), "--yes"])

    assert synced.exit_code == 0, synced.output
    assert not (target / "nested").exists()
    assert unrelated.read_bytes() == b"user content\x00\n"
    assert json.loads(manifest_path(target, "demo").read_bytes())["files"] == []
    assert apply_sync(plan_sync(target)) is False
    if git_target:
        assert exclude.read_bytes() == exclude_before
        assert _git(target, "ls-files", "--stage") == ""
        assert _git(target, "status", "--porcelain", "--untracked-files=all") == (
            "?? user.txt\n"
        )


@pytest.mark.parametrize("git_target", [False, True])
@pytest.mark.parametrize("retire", ["sync", "remove"])
def test_retire_locally_deleted_member_and_created_directories(
    tmp_path: Path, git_target: bool, retire: str
) -> None:
    config = _config(tmp_path)
    config.write_text(
        config.read_text().replace("dst: AGENTS.md", "dst: nested/deeper/AGENTS.md")
    )
    target = tmp_path / "target"
    if git_target:
        _git_repo(target)
    else:
        target.mkdir()
    exclude = target / ".git/info/exclude"
    exclude_before = exclude.read_bytes() if git_target else None
    runner = CliRunner()
    injected = runner.invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.output
    destination = target / "nested/deeper/AGENTS.md"
    destination.unlink()
    destination.parent.rmdir()
    destination.parent.parent.rmdir()
    synced = runner.invoke(app, ["project", "sync", str(target), "--yes"])
    assert synced.exit_code == 0, synced.output
    assert not (target / "nested").exists()
    if retire == "sync":
        config.write_text(
            "tracked_files: {}\nprofiles: {}\nproject_profiles:\n"
            "  demo:\n    files: {}\n"
        )
        command = ["project", "sync", str(target), "--yes"]
    else:
        command = [
            "project",
            "remove",
            "demo",
            str(target),
            "--config",
            str(config),
            "--yes",
        ]

    retired = runner.invoke(app, command)

    assert retired.exit_code == 0, retired.output
    assert not (target / "nested").exists()
    if retire == "sync":
        assert json.loads(manifest_path(target, "demo").read_bytes())["files"] == []
        assert apply_sync(plan_sync(target)) is False
    else:
        assert not manifest_path(target, "demo").exists()
    if git_target:
        assert exclude.read_bytes() == exclude_before
        assert _git(target, "status", "--porcelain", "--untracked-files=all") == ""


@pytest.mark.parametrize("visibility", ["hidden", "tracked"])
@pytest.mark.parametrize("overlay", [False, True])
def test_sync_refuses_new_member_with_opposing_linked_worktree_visibility(
    tmp_path: Path, visibility: str, overlay: bool
) -> None:
    config = _config(tmp_path)
    full_config = config.read_text()
    config.write_text(
        "tracked_files: {}\nprofiles: {}\nproject_profiles:\n  demo:\n    files: {}\n"
    )
    target = _git_repo(tmp_path / "target")
    tracked_name = "AGENTS.md" if overlay else "seed"
    (target / tracked_name).write_text("team\n")
    _git(target, "add", tracked_name)
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
    sibling = tmp_path / "sibling"
    _git(target, "worktree", "add", "-q", "-b", "sibling", str(sibling))
    runner = CliRunner()
    injected = runner.invoke(
        app,
        [
            "project",
            "inject",
            "demo",
            str(target),
            "--config",
            str(config),
            f"--git-{visibility}",
            "--yes",
        ],
    )
    assert injected.exit_code == 0, injected.output
    config.write_text(full_config)
    opposite = "tracked" if visibility == "hidden" else "hidden"
    sibling_injected = runner.invoke(
        app,
        [
            "project",
            "inject",
            "demo",
            str(sibling),
            "--config",
            str(config),
            f"--git-{opposite}",
            "--auto=use-profile",
            "--yes",
        ],
    )
    assert sibling_injected.exit_code == 0, sibling_injected.output
    preserved_paths = [
        target / ".git/config",
        target / ".git/info/exclude",
        target / ".git/index",
        target / ".git/info/attributes",
        manifest_path(target, "demo"),
        manifest_path(sibling, "demo"),
    ]
    before = {
        path: path.read_bytes() if path.exists() else None for path in preserved_paths
    }

    synced = runner.invoke(
        app, ["project", "sync", str(target), "--auto=use-profile", "--yes"]
    )

    assert synced.exit_code == 1, synced.output
    assert "conflicts with another injection in this repository" in str(
        synced.exception
    )
    assert {
        path: path.read_bytes() if path.exists() else None for path in preserved_paths
    } == before
    assert (sibling / "AGENTS.md").read_text() == "managed\n"
    if overlay:
        assert (target / "AGENTS.md").read_text() == "team\n"
    else:
        assert not (target / "AGENTS.md").exists()


def test_overlay_membership_and_visibility_preserve_unmanaged_worktree_edits(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    member_config = config.read_text()
    empty_config = (
        "tracked_files: {}\nprofiles: {}\nproject_profiles:\n  demo:\n    files: {}\n"
    )
    config.write_text(empty_config)
    source = config.parent / "project/demo/AGENTS.md"
    source.write_text("team\n\nprivate v1\n")
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
    sibling = tmp_path / "sibling"
    _git(target, "worktree", "add", "-q", "-b", "sibling", str(sibling))
    attributes = target / ".git/info/attributes"
    attributes.write_text("*.keep -text\n")
    _git(target, "config", "test.membership", "preserved")
    metadata = [target / ".git/config", target / ".git/info/exclude", attributes]
    metadata_before = {path: path.read_bytes() for path in metadata}
    index_before = _git(target, "ls-files", "--stage")
    sibling_index_before = _git(sibling, "ls-files", "--stage")
    runner = CliRunner()
    injected = runner.invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.output
    config.write_text(member_config)
    synced = runner.invoke(
        app, ["project", "sync", str(target), "--auto=use-profile", "--yes"]
    )
    assert synced.exit_code == 0, synced.output
    assert (target / "AGENTS.md").read_text() == "team\n\nprivate v1\n"
    assert _git(target, "diff", "--", "AGENTS.md") == ""
    (sibling / "AGENTS.md").write_text("sibling edit\n")
    assert "+sibling edit\n" in _git(sibling, "diff", "--", "AGENTS.md")

    visible = runner.invoke(
        app,
        ["project", "visibility", str(target), "AGENTS.md", "--tracked", "--yes"],
    )
    assert visible.exit_code == 0, visible.output
    assert "+private v1\n" in _git(target, "diff", "--", "AGENTS.md")
    (target / "AGENTS.md").write_text("target edit\n\nprivate v1\n")
    source.write_text("team\n\nprivate v2\n")
    synced = runner.invoke(app, ["project", "sync", str(target), "--yes"])
    assert synced.exit_code == 0, synced.output
    assert (target / "AGENTS.md").read_text() == "target edit\n\nprivate v2\n"
    hidden = runner.invoke(
        app,
        ["project", "visibility", str(target), "AGENTS.md", "--hidden", "--yes"],
    )
    assert hidden.exit_code == 0, hidden.output
    diff = _git(target, "diff", "--", "AGENTS.md")
    assert "+target edit\n" in diff
    assert "private" not in diff

    config.write_text(empty_config)
    removed = runner.invoke(app, ["project", "sync", str(target), "--yes"])
    assert removed.exit_code == 0, removed.output
    assert (target / "AGENTS.md").read_text() == "target edit\n"
    assert (sibling / "AGENTS.md").read_text() == "sibling edit\n"
    assert _git(target, "ls-files", "--stage") == index_before
    assert _git(sibling, "ls-files", "--stage") == sibling_index_before
    assert {path: path.read_bytes() for path in metadata} == metadata_before
    assert not overlay_path(target, Path("AGENTS.md")).exists()
