from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import app
from setforge.errors import SetforgeError
from setforge.ownership import resolve_owner_common_dir
from setforge.project_sync import (
    AutoResolution,
    _merge_mode,
    apply_sync,
    discover_injections,
    legacy_two_way_merge,
    merge_project_content,
    plan_sync,
    render_sync_manifests,
    resolve_automatically,
    resolve_sync_plan,
)
from setforge.reconcile.merge_model import ABSENT, Clean, Conflict, MergeResult
from setforge.reconcile.wizard import WizardResult


def _git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    return path


def _config(tmp_path: Path) -> Path:
    config_root = tmp_path / "config"
    source = config_root / "project" / "demo"
    source.mkdir(parents=True)
    (source / "AGENTS.md").write_text("managed\n")
    (source / "AGENTS.md").chmod(0o644)
    config = config_root / "setforge.yaml"
    config.write_text(
        "tracked_files: {}\nprofiles: {}\nproject_profiles:\n  demo:\n"
        "    files:\n      agents:\n        src: AGENTS.md\n        dst: AGENTS.md\n"
    )
    subprocess.run(["git", "init", "-q", "-b", "main", str(config_root)], check=True)
    return config


def _named_config(tmp_path: Path, name: str, destination: str) -> Path:
    config_root = tmp_path / f"config-{name}"
    source = config_root / "project" / name
    source.mkdir(parents=True)
    (source / destination).write_text(f"{name}-initial\n")
    (source / destination).chmod(0o644)
    config = config_root / "setforge.yaml"
    config.write_text(
        "tracked_files: {}\nprofiles: {}\nproject_profiles:\n"
        f"  {name}:\n    files:\n      managed:\n"
        f"        src: {destination}\n        dst: {destination}\n"
    )
    subprocess.run(["git", "init", "-q", "-b", "main", str(config_root)], check=True)
    return config


def test_discover_injections_binds_schema_two_to_exact_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    result = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert result.exit_code == 0, result.exception

    assert discover_injections(target)[0].config_path == config


def test_discover_injections_uses_canonical_config_for_legacy_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    result = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert result.exit_code == 0, result.exception
    record_path = next((tmp_path / "state" / "project-injections").glob("*.json"))
    payload = json.loads(record_path.read_text())
    payload["schema"] = 1
    del payload["config_path"]
    for file_record in payload["files"]:
        del file_record["visibility"]
        del file_record["applied_payload"]
        del file_record["upstream_mode"]
        del file_record["upstream_payload"]
    record_path.write_text(json.dumps(payload))

    assert discover_injections(target)[0].config_path == config


def test_discover_injections_rejects_non_mapping_file_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    result = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert result.exit_code == 0, result.exception
    record_path = next((tmp_path / "state" / "project-injections").glob("*.json"))
    payload = json.loads(record_path.read_text())
    payload["files"][0] = list(payload["files"][0])
    record_path.write_text(json.dumps(payload))

    with pytest.raises(SetforgeError, match="invalid file fields"):
        plan_sync(target)


def test_discover_injections_rejects_non_string_schema_two_config_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    (config.parent / "123").write_text(config.read_text())
    target = _git_repo(tmp_path / "target")
    result = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert result.exit_code == 0, result.exception
    record_path = next((tmp_path / "state" / "project-injections").glob("*.json"))
    payload = json.loads(record_path.read_text())
    payload["config_path"] = 123
    record_path.write_text(json.dumps(payload))

    with pytest.raises(SetforgeError, match="config paths are invalid"):
        discover_injections(target)


@pytest.mark.parametrize(
    ("field", "value"), [("applied_mode", -1), ("upstream_mode", 0o10000)]
)
def test_plan_sync_rejects_out_of_range_persisted_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: int,
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    result = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert result.exit_code == 0, result.exception
    record_path = next((tmp_path / "state" / "project-injections").glob("*.json"))
    payload = json.loads(record_path.read_text())
    payload["files"][0][field] = value
    record_path.write_text(json.dumps(payload))

    with pytest.raises(SetforgeError, match="file record"):
        plan_sync(target)


@pytest.mark.parametrize("value", [-1, 0o10000])
def test_plan_sync_rejects_out_of_range_legacy_applied_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: int
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    result = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert result.exit_code == 0, result.exception
    record_path = next((tmp_path / "state" / "project-injections").glob("*.json"))
    payload = json.loads(record_path.read_text())
    payload["schema"] = 1
    del payload["config_path"]
    file_record = payload["files"][0]
    del file_record["visibility"]
    del file_record["applied_payload"]
    del file_record["upstream_mode"]
    del file_record["upstream_payload"]
    file_record["applied_mode"] = value
    record_path.write_text(json.dumps(payload))

    with pytest.raises(SetforgeError, match="file record"):
        plan_sync(target)


def test_plan_sync_rejects_created_parent_outside_destination_ancestry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    unrelated = target / "unrelated"
    unrelated.mkdir()
    result = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert result.exit_code == 0, result.exception
    record_path = next((tmp_path / "state" / "project-injections").glob("*.json"))
    payload = json.loads(record_path.read_text())
    payload["files"][0]["created_parents"] = ["unrelated"]
    record_path.write_text(json.dumps(payload))

    with pytest.raises(SetforgeError, match="invalid parent record"):
        plan_sync(target)
    assert unrelated.is_dir()


@pytest.mark.parametrize(
    ("base", "ours", "theirs", "expected"),
    [
        (0o644, 0o600, 0o600, (0o600, False)),
        (0o644, 0o644, 0o755, (0o755, False)),
        (0o644, 0o600, 0o644, (0o600, False)),
        (0o644, 0o600, 0o755, (0o600, True)),
    ],
)
def test_merge_mode_truth_table(
    base: int, ours: int, theirs: int, expected: tuple[int, bool]
) -> None:
    assert _merge_mode(base, ours, theirs) == expected


def test_plan_sync_reports_unrecorded_target(tmp_path: Path) -> None:
    target = _git_repo(tmp_path / "target")

    with pytest.raises(
        SetforgeError, match=f"no project injections are recorded for: {target}"
    ):
        plan_sync(target)


def test_legacy_two_way_merge_splits_independent_differences() -> None:
    result = legacy_two_way_merge(
        b"one-local\nshared-a\nshared-b\nthree-local\n",
        b"one-profile\nshared-a\nshared-b\nthree-profile\n",
    )

    assert result.segments == (
        Conflict(b"", b"one-local\n", b"one-profile\n"),
        Clean(b"shared-a\nshared-b\n"),
        Conflict(b"", b"three-local\n", b"three-profile\n"),
    )


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (AutoResolution.KEEP_LIVE, b"one-local\nshared\ntwo-local\n"),
        (AutoResolution.USE_PROFILE, b"one-profile\nshared\ntwo-profile\n"),
    ],
)
def test_legacy_two_way_merge_resolves_every_hunk_explicitly(
    policy: AutoResolution, expected: bytes
) -> None:
    result = legacy_two_way_merge(
        b"one-local\nshared\ntwo-local\n",
        b"one-profile\nshared\ntwo-profile\n",
    )

    resolved = resolve_automatically(result, policy)

    assert resolved.clean
    assert resolved.merged() == expected


def test_legacy_two_way_merge_is_byte_exact_without_final_newline() -> None:
    result = legacy_two_way_merge(b"same\nlocal", b"same\nprofile")

    assert result.segments == (
        Clean(b"same\n"),
        Conflict(b"", b"local", b"profile"),
    )


def test_merge_project_content_uses_key_aware_merge_for_yaml() -> None:
    result = merge_project_content(
        Path("settings.yaml"),
        b"alpha: old\nbeta: old\n",
        b"alpha: local\nbeta: old\n",
        b"alpha: old\nbeta: profile\n",
    )

    assert result.clean
    assert result.merged() == b"alpha: local\nbeta: profile\n"


@pytest.mark.parametrize("suffix", ["yaml", "json"])
def test_merge_project_content_falls_back_for_invalid_structured_bytes(
    suffix: str,
) -> None:
    result = merge_project_content(
        Path(f"settings.{suffix}"),
        b"common\nbase\n\xff",
        b"common\nlocal\n\xff",
        b"common\nbase\nprofile\n\xff",
    )

    assert result.segments == (
        Clean(b"common\n"),
        Conflict(b"base\n", b"local\n", b"base\nprofile\n"),
        Clean(b"\xff"),
    )


def test_merge_project_content_falls_back_for_incompatible_root_shapes() -> None:
    result = merge_project_content(
        Path("settings.yaml"),
        b"base\n",
        b"local: keep\n",
        b"upstream\n",
    )

    assert result.segments == (Conflict(b"base\n", b"local: keep\n", b"upstream\n"),)


def test_plan_sync_three_way_preserves_independent_local_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    source = config.parent / "project" / "demo" / "AGENTS.md"
    source.write_text("alpha\nbeta\ngamma\n")
    target = _git_repo(tmp_path / "target")
    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.exception
    (target / "AGENTS.md").write_text("alpha-local\nbeta\ngamma\n")
    source.write_text("alpha\nbeta\ngamma-profile\n")

    plan = plan_sync(target)

    assert plan.conflicts == 0
    assert plan.files[0].result.merged() == b"alpha-local\nbeta\ngamma-profile\n"
    assert apply_sync(plan)
    assert (target / "AGENTS.md").read_bytes() == (
        b"alpha-local\nbeta\ngamma-profile\n"
    )
    assert not apply_sync(plan_sync(target))
    second = CliRunner().invoke(app, ["project", "sync", str(target), "--dry-run"])
    assert second.exit_code == 0, second.exception
    assert "demo: unchanged: AGENTS.md" in second.output
    removed = CliRunner().invoke(
        app,
        ["project", "remove", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert removed.exit_code == 0, removed.exception
    assert not (target / "AGENTS.md").exists()


def test_plan_sync_legacy_drift_uses_multiple_hunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    source = config.parent / "project" / "demo" / "AGENTS.md"
    source.write_text("one\nshared-a\nshared-b\nthree\n")
    target = _git_repo(tmp_path / "target")
    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.exception
    record_path = next((tmp_path / "state" / "project-injections").glob("*.json"))
    payload = json.loads(record_path.read_text())
    payload["schema"] = 1
    del payload["config_path"]
    for file_record in payload["files"]:
        del file_record["visibility"]
        del file_record["applied_payload"]
        del file_record["upstream_mode"]
        del file_record["upstream_payload"]
    record_path.write_text(json.dumps(payload))
    (target / "AGENTS.md").write_text("one-local\nshared-a\nshared-b\nthree-local\n")
    source.write_text("one-profile\nshared-a\nshared-b\nthree-profile\n")

    plan = plan_sync(target)

    assert plan.conflicts == 2
    legacy_file = plan.files[0]
    assert legacy_file.legacy
    assert legacy_file.profile == "demo"
    assert legacy_file.file_id == "agents"
    assert legacy_file.declaring_profile == "demo"
    assert legacy_file.relative_destination == Path("AGENTS.md")
    assert legacy_file.live == (b"one-local\nshared-a\nshared-b\nthree-local\n")
    assert legacy_file.desired_upstream == (
        b"one-profile\nshared-a\nshared-b\nthree-profile\n"
    )
    assert legacy_file.live_mode == 0o644
    assert legacy_file.desired_mode == 0o644
    assert legacy_file.result_mode == 0o644
    assert legacy_file.stored is not None
    assert legacy_file.addition is not None
    before_manifest = record_path.read_bytes()
    refused = CliRunner().invoke(app, ["project", "sync", str(target), "--yes"])
    assert refused.exit_code == 1
    assert refused.exception is not None
    assert "unresolved conflicts" in str(refused.exception)
    assert record_path.read_bytes() == before_manifest
    assert (target / "AGENTS.md").read_bytes() == (
        b"one-local\nshared-a\nshared-b\nthree-local\n"
    )
    with pytest.raises(SetforgeError, match="unresolved conflicts"):
        resolve_sync_plan(plan)
    kept = resolve_sync_plan(plan, auto=AutoResolution.KEEP_LIVE)
    adopted = resolve_sync_plan(plan, auto=AutoResolution.USE_PROFILE)
    assert kept is not None
    assert adopted is not None
    assert kept.files[0].result.merged() == (
        b"one-local\nshared-a\nshared-b\nthree-local\n"
    )
    assert adopted.files[0].result.merged() == (
        b"one-profile\nshared-a\nshared-b\nthree-profile\n"
    )
    manifests = render_sync_manifests(kept)
    migrated = json.loads(next(iter(manifests.values())))
    assert migrated["schema"] == 3
    assert migrated["files"][0]["visibility"] == "hidden"
    assert base64.b64decode(migrated["files"][0]["applied_payload"]) == (
        b"one-local\nshared-a\nshared-b\nthree-local\n"
    )
    assert base64.b64decode(migrated["files"][0]["upstream_payload"]) == (
        b"one-profile\nshared-a\nshared-b\nthree-profile\n"
    )
    assert apply_sync(kept)
    next_plan = plan_sync(target)
    assert not next_plan.files[0].legacy
    assert next_plan.conflicts == 0


def test_legacy_sync_requires_explicit_resolution_when_live_matches_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.exception
    record_path = next((tmp_path / "state" / "project-injections").glob("*.json"))
    payload = json.loads(record_path.read_text())
    payload["schema"] = 1
    del payload["config_path"]
    for file_record in payload["files"]:
        del file_record["visibility"]
        del file_record["applied_payload"]
        del file_record["upstream_mode"]
        del file_record["upstream_payload"]
    record_path.write_text(json.dumps(payload))
    before = record_path.read_bytes()
    (config.parent / "project" / "demo" / "AGENTS.md").write_text("profile-new\n")

    plan = plan_sync(target)

    assert plan.conflicts == 1
    with pytest.raises(SetforgeError, match="unresolved conflicts"):
        resolve_sync_plan(plan)
    assert record_path.read_bytes() == before
    adopted = resolve_sync_plan(plan, auto=AutoResolution.USE_PROFILE)
    assert adopted is not None
    assert apply_sync(adopted)
    assert (target / "AGENTS.md").read_text() == "profile-new\n"
    migrated = json.loads(record_path.read_text())
    assert migrated["schema"] == 3
    assert migrated["files"][0]["visibility"] == "hidden"


def test_sync_interactive_absence_empty_uses_recorded_wizard_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.exception
    (target / "AGENTS.md").unlink()
    (config.parent / "project" / "demo" / "AGENTS.md").write_bytes(b"")
    plan = plan_sync(target)
    assert plan.conflicts == 1

    monkeypatch.setattr(
        "setforge.reconcile.wizard.resolve_conflicts",
        lambda *args, **kwargs: WizardResult(
            MergeResult((Clean(b""),)), False, ("ours",)
        ),
    )
    interactive_absent = resolve_sync_plan(plan, interactive=True)
    assert interactive_absent is not None
    assert interactive_absent.files[0].result.absent

    monkeypatch.setattr(
        "setforge.reconcile.wizard.resolve_conflicts",
        lambda *args, **kwargs: WizardResult(
            MergeResult((Clean(b""),)), False, ("theirs",)
        ),
    )
    interactive_empty = resolve_sync_plan(plan, interactive=True)
    assert interactive_empty is not None
    assert not interactive_empty.files[0].result.absent
    assert interactive_empty.files[0].result.merged() == b""

    kept = resolve_sync_plan(plan, auto=AutoResolution.KEEP_LIVE)
    adopted = resolve_sync_plan(plan, auto=AutoResolution.USE_PROFILE)
    assert kept is not None
    assert kept.files[0].result.absent
    assert adopted is not None
    assert adopted.files[0].result.merged() == b""


def test_unresolved_plan_refuses_render_and_apply_with_exact_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.exception
    (target / "AGENTS.md").write_text("local\n")
    (config.parent / "project" / "demo" / "AGENTS.md").write_text("profile\n")
    plan = plan_sync(target)
    assert plan.conflicts == 1

    with pytest.raises(
        SetforgeError, match="cannot render unresolved project sync manifests"
    ):
        render_sync_manifests(plan)
    with pytest.raises(
        SetforgeError, match="cannot apply an unresolved project sync plan"
    ):
        apply_sync(plan)


def test_resolve_sync_plan_handles_clean_content_mode_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    source = config.parent / "project" / "demo" / "AGENTS.md"
    target = _git_repo(tmp_path / "target")
    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.exception
    (target / "AGENTS.md").chmod(0o600)
    source.chmod(0o755)
    plan = plan_sync(target)
    assert plan.files[0].result.clean
    assert plan.files[0].mode_conflict

    kept = resolve_sync_plan(plan, auto=AutoResolution.KEEP_LIVE)
    adopted = resolve_sync_plan(plan, auto=AutoResolution.USE_PROFILE)

    assert kept is not None
    assert adopted is not None
    assert kept.files[0].result_mode == 0o600
    assert adopted.files[0].result_mode == 0o755
    assert not kept.files[0].mode_conflict
    assert not adopted.files[0].mode_conflict


def test_apply_sync_adds_and_removes_profile_membership_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    original_config = config.read_text()
    target = _git_repo(tmp_path / "target")
    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.exception
    (config.parent / "project" / "demo" / "EXTRA.md").write_text("extra\n")
    (config.parent / "project" / "demo" / "EXTRA.md").chmod(0o644)
    config.write_text(
        original_config
        + "      extra:\n        src: EXTRA.md\n        dst: nested/deeper/EXTRA.md\n"
    )

    add_plan = plan_sync(target)
    assert any(item.kind.value == "add" for item in add_plan.files)
    assert apply_sync(add_plan)
    assert (target / "nested" / "deeper" / "EXTRA.md").read_text() == "extra\n"
    assert (
        subprocess.run(
            ["git", "-C", str(target), "status", "--short"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        == ""
    )

    config.write_text(original_config)
    remove_plan = plan_sync(target)
    removed_file = next(
        item for item in remove_plan.files if item.kind.value == "remove"
    )
    assert removed_file.profile == "demo"
    assert removed_file.file_id == "extra"
    assert removed_file.declaring_profile == "demo"
    assert removed_file.relative_destination == Path("nested/deeper/EXTRA.md")
    assert removed_file.live == b"extra\n"
    assert removed_file.live_mode == 0o644
    assert removed_file.desired_upstream is ABSENT
    assert removed_file.desired_mode is None
    assert removed_file.result_mode is None
    assert removed_file.result.absent
    assert removed_file.legacy is False
    assert removed_file.stored is not None
    assert removed_file.addition is None
    assert apply_sync(remove_plan)
    assert not (target / "nested").exists()

    config.write_text(
        original_config
        + "      extra:\n        src: EXTRA.md\n        dst: nested/deeper/EXTRA.md\n"
    )
    restore_plan = plan_sync(target)
    assert apply_sync(restore_plan)
    assert (target / "nested" / "deeper" / "EXTRA.md").read_text() == "extra\n"
    assert (
        subprocess.run(
            ["git", "-C", str(target), "status", "--short"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        == ""
    )


def test_sync_preserves_overlay_visibility_and_reconciles_hidden_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    original_config = config.read_text()
    target = _git_repo(tmp_path / "target")
    subprocess.run(["git", "config", "user.name", "Test"], cwd=target, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=target,
        check=True,
    )
    (target / "AGENTS.md").write_text("team\n")
    (target / "EXTRA.md").write_text("extra-team\n")
    subprocess.run(["git", "add", "AGENTS.md", "EXTRA.md"], cwd=target, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=target, check=True)
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
    source = config.parent / "project" / "demo" / "EXTRA.md"
    source.write_text("extra-managed\n")
    config.write_text(
        original_config + "      extra:\n        src: EXTRA.md\n        dst: EXTRA.md\n"
    )

    add_plan = resolve_sync_plan(plan_sync(target), auto=AutoResolution.USE_PROFILE)
    assert add_plan is not None
    assert apply_sync(add_plan)
    attributes = target / ".git" / "info" / "attributes"
    assert "/EXTRA.md filter=setforge-project" in attributes.read_text()
    tracked = CliRunner().invoke(
        app,
        ["project", "visibility", str(target), "EXTRA.md", "--tracked", "--yes"],
    )
    assert tracked.exit_code == 0, tracked.output
    assert "/EXTRA.md filter=setforge-project" not in attributes.read_text()
    source.write_text("extra-updated\n")
    update_plan = plan_sync(target)
    extra = next(
        item
        for item in update_plan.files
        if item.relative_destination == Path("EXTRA.md")
    )
    assert extra.stored is not None
    assert extra.stored.visibility.value == "tracked"
    rendered = json.loads(next(iter(render_sync_manifests(update_plan).values())))
    rendered_extra = next(
        entry for entry in rendered["files"] if entry["destination"] == "EXTRA.md"
    )
    assert rendered_extra["visibility"] == "tracked"
    assert apply_sync(update_plan)
    hidden = CliRunner().invoke(
        app,
        ["project", "visibility", str(target), "EXTRA.md", "--hidden", "--yes"],
    )
    assert hidden.exit_code == 0, hidden.output
    assert "/EXTRA.md filter=setforge-project" in attributes.read_text()
    config.write_text(original_config)

    assert apply_sync(plan_sync(target))
    assert "/EXTRA.md filter=setforge-project" not in attributes.read_text()


def test_plan_sync_legacy_membership_removal_preserves_explicit_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.exception
    record_path = next((tmp_path / "state" / "project-injections").glob("*.json"))
    payload = json.loads(record_path.read_text())
    payload["schema"] = 1
    del payload["config_path"]
    for file_record in payload["files"]:
        del file_record["visibility"]
        del file_record["applied_payload"]
        del file_record["upstream_mode"]
        del file_record["upstream_payload"]
    record_path.write_text(json.dumps(payload))
    config.write_text(
        "tracked_files: {}\nprofiles: {}\nproject_profiles:\n  demo:\n    files: {}\n"
    )

    plan = plan_sync(target)

    removed = plan.files[0]
    assert removed.kind.value == "remove"
    assert removed.live == b"managed\n"
    assert removed.live_mode == 0o644
    assert removed.desired_upstream is ABSENT
    assert removed.legacy is True
    assert plan.conflicts == 1
    adopted = resolve_sync_plan(plan, auto=AutoResolution.USE_PROFILE)
    assert adopted is not None
    assert adopted.files[0].result.absent


def test_sync_membership_add_collision_requires_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    original_config = config.read_text()
    target = _git_repo(tmp_path / "target")
    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.exception
    (target / "EXTRA.md").write_text("local\n")
    (config.parent / "project" / "demo" / "EXTRA.md").write_text("profile\n")
    (target / "EXTRA.md").chmod(0o644)
    (config.parent / "project" / "demo" / "EXTRA.md").chmod(0o644)
    config.write_text(
        original_config + "      extra:\n        src: EXTRA.md\n        dst: EXTRA.md\n"
    )
    before = (target / "EXTRA.md").read_bytes()

    plan = plan_sync(target)

    added = next(
        item for item in plan.files if item.relative_destination == Path("EXTRA.md")
    )
    assert added.kind.value == "add"
    assert added.profile == "demo"
    assert added.file_id == "extra"
    assert added.declaring_profile == "demo"
    assert added.live == b"local\n"
    assert added.live_mode == 0o644
    assert added.desired_upstream == b"profile\n"
    assert added.desired_mode == 0o644
    assert added.result_mode == 0o644
    assert not added.legacy
    assert added.addition is not None
    assert not added.result.clean
    with pytest.raises(SetforgeError, match="unresolved conflicts"):
        resolve_sync_plan(plan)
    assert (target / "EXTRA.md").read_bytes() == before
    adopted = resolve_sync_plan(plan, auto=AutoResolution.USE_PROFILE)
    assert adopted is not None
    assert apply_sync(adopted)
    assert (target / "EXTRA.md").read_text() == "profile\n"


def test_sync_membership_add_rejects_mismatched_released_tombstone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    original_config = config.read_text()
    target = _git_repo(tmp_path / "target")
    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.exception
    config.write_text(
        "tracked_files: {}\nprofiles: {}\nproject_profiles:\n  demo:\n    files: {}\n"
    )
    assert apply_sync(plan_sync(target))
    assert not (target / "AGENTS.md").exists()
    (config.parent / "project" / "demo" / "OTHER.md").write_text("other\n")
    config.write_text(
        original_config.replace("agents:", "other:").replace(
            "src: AGENTS.md", "src: OTHER.md"
        )
    )

    plan = plan_sync(target)
    with pytest.raises(SetforgeError, match="active ownership claim"):
        apply_sync(plan)
    assert not (target / "AGENTS.md").exists()


def test_project_sync_cli_dry_run_then_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.exception
    source = config.parent / "project" / "demo" / "AGENTS.md"
    source.write_text("updated\n")
    state_path = next((tmp_path / "state" / "project-injections").glob("*.json"))
    before_state = state_path.read_bytes()

    preview = CliRunner().invoke(app, ["project", "sync", str(target), "--dry-run"])
    assert preview.exit_code == 0, preview.exception
    assert "update: AGENTS.md" in preview.output
    assert "dry run: no changes applied" in preview.output
    assert (target / "AGENTS.md").read_text() == "managed\n"
    assert state_path.read_bytes() == before_state

    applied = CliRunner().invoke(app, ["project", "sync", str(target), "--yes"])
    assert applied.exit_code == 0, applied.exception
    assert "sync complete" in applied.output
    assert (target / "AGENTS.md").read_text() == "updated\n"


def test_apply_sync_locks_and_updates_multiple_config_repositories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    target = _git_repo(tmp_path / "target")
    alpha = _named_config(tmp_path, "alpha", "ALPHA.md")
    beta = _named_config(tmp_path, "beta", "BETA.md")
    runner = CliRunner()
    for profile, config in (("alpha", alpha), ("beta", beta)):
        result = runner.invoke(
            app,
            [
                "project",
                "inject",
                profile,
                str(target),
                "--config",
                str(config),
                "--yes",
            ],
        )
        assert result.exit_code == 0, result.exception
    (alpha.parent / "project" / "alpha" / "ALPHA.md").write_text("alpha-updated\n")
    (beta.parent / "project" / "beta" / "BETA.md").write_text("beta-updated\n")

    plan = plan_sync(target)

    assert [item.profile for item in plan.injections] == ["alpha", "beta"]
    identity_dirs = {
        resolve_owner_common_dir(item.config_root) for item in plan.injections
    }
    assert len(identity_dirs) == 2
    assert apply_sync(plan)
    assert (target / "ALPHA.md").read_text() == "alpha-updated\n"
    assert (target / "BETA.md").read_text() == "beta-updated\n"


def test_apply_sync_refuses_visibility_change_after_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.exception
    plan = plan_sync(target)
    manifest = plan.injections[0].manifest_path
    before_live = (target / "AGENTS.md").read_bytes()
    raw = json.loads(manifest.read_bytes())
    raw["visibility"] = "tracked"
    manifest.write_text(json.dumps(raw, separators=(",", ":"), sort_keys=True) + "\n")
    changed_manifest = manifest.read_bytes()

    with pytest.raises(SetforgeError, match="plan changed before apply"):
        apply_sync(plan)

    assert (target / "AGENTS.md").read_bytes() == before_live
    assert manifest.read_bytes() == changed_manifest


def test_apply_sync_fault_restores_entire_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    original = config.read_text()
    (config.parent / "project" / "demo" / "EXTRA.md").write_text("extra-old\n")
    config.write_text(
        original + "      extra:\n        src: EXTRA.md\n        dst: EXTRA.md\n"
    )
    target = _git_repo(tmp_path / "target")
    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.exception
    (config.parent / "project" / "demo" / "AGENTS.md").write_text("agents-new\n")
    (config.parent / "project" / "demo" / "EXTRA.md").write_text("extra-new\n")
    plan = plan_sync(target)
    state_files = [
        path
        for path in (tmp_path / "state").rglob("*")
        if path.is_file() and "locks" not in path.parts
    ]
    before_state = {path: path.read_bytes() for path in state_files}
    exclude = Path(
        subprocess.run(
            ["git", "-C", str(target), "rev-parse", "--git-path", "info/exclude"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
    )
    if not exclude.is_absolute():
        exclude = target / exclude
    before_exclude = exclude.read_bytes()
    before_files = {
        name: (target / name).read_bytes() for name in ("AGENTS.md", "EXTRA.md")
    }
    real_write = __import__("setforge.project_sync", fromlist=["_write_project_file"])
    original_write = real_write._write_project_file
    calls = 0

    def fail_second(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second-write failure")
        original_write(*args, **kwargs)

    monkeypatch.setattr("setforge.project_sync._write_project_file", fail_second)

    with pytest.raises(OSError, match="second-write failure"):
        apply_sync(plan)

    assert {
        name: (target / name).read_bytes() for name in ("AGENTS.md", "EXTRA.md")
    } == before_files
    assert exclude.read_bytes() == before_exclude
    assert all(path.read_bytes() == payload for path, payload in before_state.items())


def test_sync_preserves_local_deletion_and_remove_releases_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    runner = CliRunner()
    injected = runner.invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.exception
    (target / "AGENTS.md").unlink()
    (config.parent / "project" / "demo" / "AGENTS.md").write_text("profile-new\n")
    plan = plan_sync(target)
    assert plan.conflicts == 1
    resolved = resolve_sync_plan(plan, auto=AutoResolution.KEEP_LIVE)
    assert resolved is not None

    assert apply_sync(resolved)
    assert not (target / "AGENTS.md").exists()
    removed = runner.invoke(
        app,
        ["project", "remove", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert removed.exit_code == 0, removed.exception
    assert not (target / "AGENTS.md").exists()
