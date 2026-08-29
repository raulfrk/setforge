from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import setforge.project_visibility as visibility_module
from setforge.cli import app
from setforge.config import ProjectVisibility
from setforge.git_overlay import OverlayGitPlan
from setforge.project_injection import manifest_path
from setforge.project_overlay import build_overlay, write_overlay
from setforge.project_visibility import (
    apply_project_visibility,
    plan_project_visibility,
)


@pytest.fixture(autouse=True)
def _candidate_filter_entrypoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary_dir = tmp_path / "candidate-bin"
    binary_dir.mkdir()
    entrypoint = binary_dir / "setforge"
    entrypoint.write_text(
        f"#!{sys.executable}\nfrom setforge.cli import main\nmain()\n"
    )
    entrypoint.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parents[1]))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))


def _git(path: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=check,
        text=True,
        capture_output=True,
    ).stdout


def _config(tmp_path: Path, *, two_files: bool = False) -> Path:
    root = tmp_path / "config"
    source = root / "project/demo/AGENTS.md"
    source.parent.mkdir(parents=True)
    source.write_text("team\nprofile\n")
    extra = ""
    if two_files:
        (source.parent / "guide.md").write_text("guide\n")
        extra = "      guide: {src: guide.md, dst: guide.md}\n"
    config = root / "setforge.yaml"
    config.write_text(
        "tracked_files: {}\nprofiles: {}\nproject_profiles:\n"
        "  demo:\n    default_visibility: hidden\n    files:\n"
        "      instructions: {src: AGENTS.md, dst: AGENTS.md}\n" + extra
    )
    _git(root, "init", "-q")
    return config


def _target(tmp_path: Path, *, tracked: bool = False) -> Path:
    target = tmp_path / "target"
    target.mkdir()
    _git(target, "init", "-q")
    _git(target, "config", "user.name", "SetForge Test")
    _git(target, "config", "user.email", "test@example.invalid")
    if tracked:
        (target / "AGENTS.md").write_text("team\n")
        _git(target, "add", "AGENTS.md")
        _git(target, "commit", "-q", "-m", "base")
    return target


def _inject(config: Path, target: Path, *extra: str) -> None:
    result = CliRunner().invoke(
        app,
        [
            "project",
            "inject",
            "demo",
            str(target),
            "--config",
            str(config),
            *extra,
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output


def test_list_and_ordinary_visibility_round_trip(tmp_path: Path) -> None:
    config = _config(tmp_path)
    target = _target(tmp_path)
    _inject(config, target)

    listed = CliRunner().invoke(app, ["project", "list"])
    assert listed.exit_code == 0
    assert f"{target}  [demo]" in listed.output
    assert "hidden: AGENTS.md" in listed.output

    tracked = CliRunner().invoke(
        app,
        ["project", "visibility", str(target), "AGENTS.md", "--tracked", "--yes"],
    )
    assert tracked.exit_code == 0, tracked.output
    assert _git(target, "status", "--short") == "?? AGENTS.md\n"
    raw = json.loads(manifest_path(target, "demo").read_text())
    assert raw["schema"] == 3
    assert raw["files"][0]["visibility"] == "tracked"

    _git(target, "add", "AGENTS.md")
    _git(target, "commit", "-q", "-m", "track")
    hidden = CliRunner().invoke(
        app,
        ["project", "visibility", str(target), "AGENTS.md", "--hidden", "--yes"],
    )
    assert hidden.exit_code == 0, hidden.output
    assert _git(target, "ls-files", "--error-unmatch", "AGENTS.md", check=False) == ""
    assert (target / "AGENTS.md").read_text() == "team\nprofile\n"
    assert _git(target, "check-ignore", "AGENTS.md").strip() == "AGENTS.md"


def test_tracked_overlay_visibility_exposes_and_rehides_only_injected_hunk(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    target = _target(tmp_path, tracked=True)
    _inject(config, target, "--auto=use-profile")

    listed = CliRunner().invoke(app, ["project", "list"])
    assert listed.exit_code == 0, listed.output
    assert "tracked-overlay: AGENTS.md" in listed.output
    assert _git(target, "diff", "--", "AGENTS.md") == ""

    exposed = CliRunner().invoke(
        app,
        ["project", "visibility", str(target), "AGENTS.md", "--tracked", "--yes"],
    )
    assert exposed.exit_code == 0, exposed.output
    assert "+profile" in _git(target, "diff", "--", "AGENTS.md")
    assert _git(target, "ls-files", "AGENTS.md") == "AGENTS.md\n"

    hidden = CliRunner().invoke(
        app,
        ["project", "visibility", str(target), "AGENTS.md", "--hidden", "--yes"],
    )
    assert hidden.exit_code == 0, hidden.output
    assert _git(target, "diff", "--", "AGENTS.md") == ""
    assert (target / "AGENTS.md").read_text() == "team\nprofile\n"


def test_overlay_visibility_accepts_unrelated_live_edits(tmp_path: Path) -> None:
    config = _config(tmp_path)
    target = _target(tmp_path, tracked=True)
    _inject(config, target, "--auto=use-profile")
    (target / "AGENTS.md").write_text("team edited\nprofile\n")

    listed = CliRunner().invoke(app, ["project", "list"])
    assert listed.exit_code == 0, listed.output
    assert "tracked-overlay: AGENTS.md" in listed.output
    exposed = CliRunner().invoke(
        app,
        ["project", "visibility", str(target), "AGENTS.md", "--tracked", "--yes"],
    )
    assert exposed.exit_code == 0, exposed.output
    diff = _git(target, "diff", "--", "AGENTS.md")
    assert "+team edited" in diff
    assert "+profile" in diff


def test_list_rejects_overlay_payload_mismatched_with_manifest(tmp_path: Path) -> None:
    config = _config(tmp_path)
    target = _target(tmp_path, tracked=True)
    _inject(config, target, "--auto=use-profile")
    write_overlay(
        build_overlay(
            target, Path("AGENTS.md"), b"different\n", b"different\nprofile\n"
        )
    )

    result = CliRunner().invoke(app, ["project", "list"])
    assert result.exit_code == 1
    assert "tracked project overlay has drifted: AGENTS.md" in result.output


def test_tracked_overlay_injection_stays_visible_and_removes_cleanly(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    target = _target(tmp_path, tracked=True)
    _inject(config, target, "--git-tracked", "--auto=use-profile")

    listed = CliRunner().invoke(app, ["project", "list"])
    assert listed.exit_code == 0, listed.output
    assert "tracked: AGENTS.md" in listed.output
    assert "+profile" in _git(target, "diff", "--", "AGENTS.md")

    removed = CliRunner().invoke(
        app,
        [
            "project",
            "remove",
            "demo",
            str(target),
            "--config",
            str(config),
            "--yes",
        ],
    )
    assert removed.exit_code == 0, removed.output
    assert (target / "AGENTS.md").read_text() == "team\n"


def test_visibility_refuses_linked_worktree_intent_conflict(tmp_path: Path) -> None:
    config = _config(tmp_path)
    target = _target(tmp_path)
    (target / "seed").write_text("seed\n")
    _git(target, "add", "seed")
    _git(target, "commit", "-q", "-m", "seed")
    sibling = tmp_path / "sibling"
    _git(target, "worktree", "add", "-q", "-b", "sibling", str(sibling))
    _inject(config, target)
    _inject(config, sibling)

    changed = CliRunner().invoke(
        app,
        ["project", "visibility", str(target), "AGENTS.md", "--tracked", "--yes"],
    )
    assert changed.exit_code == 1
    assert "conflicts across linked worktrees" in str(changed.exception)
    assert _git(target, "check-ignore", "AGENTS.md").strip() == "AGENTS.md"


def test_plain_target_visibility_is_exact_no_op(tmp_path: Path) -> None:
    config = _config(tmp_path)
    target = tmp_path / "plain"
    target.mkdir()
    _inject(config, target)
    record = manifest_path(target, "demo")
    before = hashlib.sha256(record.read_bytes()).digest()

    result = CliRunner().invoke(
        app,
        ["project", "visibility", str(target), "AGENTS.md", "--tracked", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert "not applicable" in result.output
    assert hashlib.sha256(record.read_bytes()).digest() == before
    assert not (target / ".git").exists()


def test_visibility_expands_schema_two_without_losing_other_file_defaults(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, two_files=True)
    target = _target(tmp_path)
    _inject(config, target)
    record = manifest_path(target, "demo")
    raw = json.loads(record.read_text())
    raw["schema"] = 2
    for entry in raw["files"]:
        entry.pop("visibility")
    record.write_text(json.dumps(raw, separators=(",", ":"), sort_keys=True) + "\n")

    result = CliRunner().invoke(
        app,
        ["project", "visibility", str(target), "AGENTS.md", "--tracked", "--yes"],
    )
    assert result.exit_code == 0, result.output
    migrated = json.loads(record.read_text())
    assert migrated["schema"] == 3
    assert {
        entry["destination"]: entry["visibility"] for entry in migrated["files"]
    } == {"AGENTS.md": "tracked", "guide.md": "hidden"}


def test_schema_two_mixed_files_reconcile_all_visibility_plumbing(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, two_files=True)
    target = _target(tmp_path, tracked=True)
    _inject(config, target, "--auto=use-profile")
    record = manifest_path(target, "demo")
    ordinary_tracked = CliRunner().invoke(
        app,
        ["project", "visibility", str(target), "guide.md", "--tracked", "--yes"],
    )
    assert ordinary_tracked.exit_code == 0, ordinary_tracked.output
    assert _git(target, "check-ignore", "guide.md", check=False) == ""
    attributes = target / ".git" / "info" / "attributes"
    assert "/AGENTS.md filter=setforge-project" in attributes.read_text()
    raw = json.loads(record.read_text())
    raw["schema"] = 2
    raw["visibility"] = "tracked"
    for entry in raw["files"]:
        entry.pop("visibility")
    record.write_text(json.dumps(raw, separators=(",", ":"), sort_keys=True) + "\n")

    changed = CliRunner().invoke(
        app,
        ["project", "visibility", str(target), "guide.md", "--tracked", "--yes"],
    )
    assert changed.exit_code == 0, changed.output
    migrated = json.loads(record.read_text())
    assert {
        entry["destination"]: entry["visibility"] for entry in migrated["files"]
    } == {"AGENTS.md": "tracked", "guide.md": "tracked"}
    assert "/AGENTS.md filter=setforge-project" not in attributes.read_text()
    assert "+profile" in _git(target, "diff", "--", "AGENTS.md")
    listed = CliRunner().invoke(app, ["project", "list"])
    assert listed.exit_code == 0, listed.output
    assert "tracked: AGENTS.md" in listed.output
    assert "tracked: guide.md" in listed.output


def test_list_retains_corrupt_record_and_exits_nonzero(tmp_path: Path) -> None:
    records = tmp_path / "state/project-injections"
    records.mkdir(parents=True)
    (records / "broken.json").write_text("{broken")

    result = CliRunner().invoke(app, ["project", "list"])
    assert result.exit_code == 1
    assert "error:" in result.output
    assert "broken.json" in result.output


def test_list_reports_live_content_drift_as_error(tmp_path: Path) -> None:
    config = _config(tmp_path)
    target = _target(tmp_path)
    _inject(config, target)
    (target / "AGENTS.md").write_text("local drift\n")

    result = CliRunner().invoke(app, ["project", "list"])
    assert result.exit_code == 1
    assert "injected project file has drifted: AGENTS.md" in result.output


def test_list_retains_healthy_sibling_after_file_drift(tmp_path: Path) -> None:
    config = _config(tmp_path, two_files=True)
    target = _target(tmp_path)
    _inject(config, target)
    (target / "AGENTS.md").write_text("local drift\n")

    result = CliRunner().invoke(app, ["project", "list"])
    assert result.exit_code == 1
    assert "error: AGENTS.md: injected project file has drifted" in result.output
    assert "hidden: guide.md" in result.output


def test_malformed_record_is_not_migrated(tmp_path: Path) -> None:
    config = _config(tmp_path)
    target = _target(tmp_path)
    _inject(config, target)
    record = manifest_path(target, "demo")
    raw = json.loads(record.read_text())
    raw["schema"] = 2
    raw["files"][0].pop("visibility")
    raw["files"][0].pop("source_digest")
    record.write_text(json.dumps(raw, separators=(",", ":"), sort_keys=True) + "\n")
    before = record.read_bytes()

    result = CliRunner().invoke(
        app,
        ["project", "visibility", str(target), "AGENTS.md", "--tracked", "--yes"],
    )
    assert result.exit_code == 1
    assert record.read_bytes() == before


def test_list_rejects_stale_git_identity(tmp_path: Path) -> None:
    config = _config(tmp_path)
    target = _target(tmp_path)
    _inject(config, target)
    record = manifest_path(target, "demo")
    raw = json.loads(record.read_text())
    raw["git_dir"] = str(tmp_path / "wrong.git")
    record.write_text(json.dumps(raw, separators=(",", ":"), sort_keys=True) + "\n")

    result = CliRunner().invoke(app, ["project", "list"])
    assert result.exit_code == 1
    assert "target identity" in result.output


def test_list_rejects_hidden_file_present_in_index(tmp_path: Path) -> None:
    config = _config(tmp_path)
    target = _target(tmp_path)
    _inject(config, target)
    _git(target, "add", "-f", "AGENTS.md")

    result = CliRunner().invoke(app, ["project", "list"])
    assert result.exit_code == 1
    assert "hidden project file is present in the Git index" in result.output


def test_visibility_failure_restores_index_private_state_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    target = _target(tmp_path)
    _inject(config, target)
    tracked = CliRunner().invoke(
        app,
        ["project", "visibility", str(target), "AGENTS.md", "--tracked", "--yes"],
    )
    assert tracked.exit_code == 0, tracked.output
    _git(target, "add", "AGENTS.md")
    _git(target, "commit", "-q", "-m", "track")
    plan = plan_project_visibility(target, Path("AGENTS.md"), ProjectVisibility.HIDDEN)
    index_path = Path(_git(target, "rev-parse", "--git-path", "index").strip())
    if not index_path.is_absolute():
        index_path = target / index_path
    state_root = tmp_path / "state"

    def state() -> dict[Path, bytes]:
        return {
            path.relative_to(state_root): path.read_bytes()
            for path in state_root.rglob("*")
            if path.is_file()
        }

    before = (
        index_path.read_bytes(),
        (target / "AGENTS.md").read_bytes(),
        plan.manifest_path.read_bytes(),
        plan.visibility_plan.exclude_path.read_bytes()
        if plan.visibility_plan is not None
        and plan.visibility_plan.exclude_path.exists()
        else None,
        state(),
    )

    def fail_claims(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected claim failure")

    monkeypatch.setattr(visibility_module, "apply_claims", fail_claims)
    with pytest.raises(RuntimeError, match="injected claim failure"):
        apply_project_visibility(plan)
    after = (
        index_path.read_bytes(),
        (target / "AGENTS.md").read_bytes(),
        plan.manifest_path.read_bytes(),
        plan.visibility_plan.exclude_path.read_bytes()
        if plan.visibility_plan is not None
        and plan.visibility_plan.exclude_path.exists()
        else None,
        state(),
    )
    assert after == before


def test_visibility_failure_restores_overlay_filter_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    target = _target(tmp_path, tracked=True)
    _inject(config, target, "--auto=use-profile")
    plan = plan_project_visibility(target, Path("AGENTS.md"), ProjectVisibility.TRACKED)
    assert plan.overlay_git_plan is not None
    paths = (
        plan.manifest_path,
        plan.overlay_git_plan.config_path,
        plan.overlay_git_plan.attributes_path,
    )

    def snapshot() -> tuple[bytes | None, ...]:
        return tuple(path.read_bytes() if path.exists() else None for path in paths)

    before = snapshot()
    real_apply = visibility_module.apply_overlay_git

    def fail_after_filter(overlay_plan: OverlayGitPlan) -> None:
        real_apply(overlay_plan)
        raise RuntimeError("injected filter failure")

    monkeypatch.setattr(visibility_module, "apply_overlay_git", fail_after_filter)
    with pytest.raises(RuntimeError, match="injected filter failure"):
        apply_project_visibility(plan)
    assert snapshot() == before
    assert _git(target, "diff", "--", "AGENTS.md") == ""


def test_visibility_preview_non_tty_noop_and_invalid_selectors_are_exact_noops(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    target = _target(tmp_path)
    _inject(config, target)
    record = manifest_path(target, "demo")
    before = record.read_bytes()
    exclude = Path(_git(target, "rev-parse", "--git-path", "info/exclude").strip())
    if not exclude.is_absolute():
        exclude = target / exclude
    exclude_before = exclude.read_bytes()

    noop = CliRunner().invoke(
        app,
        ["project", "visibility", str(target), "AGENTS.md", "--hidden", "--yes"],
    )
    preview = CliRunner().invoke(
        app,
        ["project", "visibility", str(target), "AGENTS.md", "--tracked", "--dry-run"],
    )
    non_tty = CliRunner().invoke(
        app,
        ["project", "visibility", str(target), "AGENTS.md", "--tracked"],
    )
    unknown = CliRunner().invoke(
        app,
        ["project", "visibility", str(target), "missing.md", "--tracked", "--yes"],
    )
    unsafe = CliRunner().invoke(
        app,
        ["project", "visibility", str(target), "../AGENTS.md", "--tracked", "--yes"],
    )
    assert noop.exit_code == 0
    assert preview.exit_code == 0
    assert non_tty.exit_code == 1
    assert "requires --yes" in str(non_tty.exception)
    assert unknown.exit_code == 1
    assert "project file is not recorded at this target" in str(unknown.exception)
    assert unsafe.exit_code == 1
    assert record.read_bytes() == before
    assert exclude.read_bytes() == exclude_before


def test_visibility_interactive_decline_is_an_exact_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    target = _target(tmp_path)
    _inject(config, target)
    record = manifest_path(target, "demo")
    before = record.read_bytes()
    exclude = Path(_git(target, "rev-parse", "--git-path", "info/exclude").strip())
    if not exclude.is_absolute():
        exclude = target / exclude
    exclude_before = exclude.read_bytes()
    git_config = Path(_git(target, "rev-parse", "--git-path", "config").strip())
    if not git_config.is_absolute():
        git_config = target / git_config
    attributes = Path(
        _git(target, "rev-parse", "--git-path", "info/attributes").strip()
    )
    if not attributes.is_absolute():
        attributes = target / attributes
    index = Path(_git(target, "rev-parse", "--git-path", "index").strip())
    if not index.is_absolute():
        index = target / index
    state = tmp_path / "state"

    def snapshot() -> tuple[
        bytes, bytes, bytes | None, bytes, bytes | None, dict[Path, bytes]
    ]:
        return (
            record.read_bytes(),
            exclude.read_bytes(),
            attributes.read_bytes() if attributes.exists() else None,
            git_config.read_bytes(),
            index.read_bytes() if index.exists() else None,
            {
                path.relative_to(state): path.read_bytes()
                for path in state.rglob("*")
                if path.is_file()
            },
        )

    exact_before = snapshot()
    monkeypatch.setattr(
        "setforge.cli.project.sys",
        SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True)),
    )

    result = CliRunner().invoke(
        app,
        ["project", "visibility", str(target), "AGENTS.md", "--tracked"],
        input="n\n",
    )

    assert result.exit_code == 0, result.output
    assert "aborted: no changes applied" in result.output
    assert snapshot() == exact_before
    assert record.read_bytes() == before
    assert exclude.read_bytes() == exclude_before
    assert _git(target, "ls-files") == ""


def test_visibility_refuses_ambiguous_recorded_ownership(tmp_path: Path) -> None:
    config = _config(tmp_path)
    target = _target(tmp_path)
    _inject(config, target)
    record = manifest_path(target, "demo")
    duplicate = record.with_name("duplicate.json")
    duplicate.write_bytes(record.read_bytes())
    before = record.read_bytes()

    result = CliRunner().invoke(
        app,
        ["project", "visibility", str(target), "AGENTS.md", "--tracked", "--yes"],
    )
    assert result.exit_code == 1
    assert "ambiguous recorded ownership" in str(result.exception)
    assert record.read_bytes() == before
    assert duplicate.read_bytes() == before
