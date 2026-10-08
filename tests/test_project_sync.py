from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import stat
import subprocess
import uuid
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from setforge import locking, transitions
from setforge.cli import app
from setforge.errors import SetforgeError
from setforge.locking import profile_lock
from setforge.ownership import (
    OwnershipStore,
    ownership_claim_to_json,
    resolve_owner_common_dir,
)
from setforge.project_injection import ProjectFileAction
from setforge.project_sync import (
    AutoResolution,
    SyncFileKind,
    _merge_mode,
    apply_sync,
    discover_injections,
    merge_project_content,
    missing_locally,
    plan_sync,
    render_sync_manifests,
    resolve_automatically,
    resolve_sync_plan,
    two_way_merge,
)
from setforge.reconcile import file_id as reconcile_file_id
from setforge.reconcile import record as record_base
from setforge.reconcile.merge_model import ABSENT, Clean, Conflict, MergeResult
from setforge.reconcile.structured_units import structured_format
from setforge.reconcile.wizard import WizardResult
from setforge.reconcile_apply import reconcile_file
from tests.project_helpers import _config, _git_repo


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


def _recorded_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, dict[str, Any]]:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    result = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert result.exit_code == 0, result.output
    record = next((tmp_path / "state" / "project-injections").glob("*.json"))
    return config, target, record, json.loads(record.read_text())


@pytest.mark.parametrize("change", ["inode", "git-dir"])
def test_discover_injections_refuses_changed_identity_with_the_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    _, target, record, payload = _recorded_injection(tmp_path, monkeypatch)
    if change == "inode":
        payload["target_inode"] += 1
        remedy = f"run `setforge project remove demo {target}` to drop the stale record"
    else:
        payload["git_dir"] = None
        remedy = (
            "the Git directory changed since injection (recorded none, now "
            f"{target / '.git'}); run `setforge project remove demo {target}` to "
            "remove the injection, then inject again"
        )
    record.write_text(json.dumps(payload))

    with pytest.raises(SetforgeError) as failure:
        discover_injections(target)

    assert str(failure.value) == (
        f"project injection state does not match target identity: {record}; {remedy}"
    )


@pytest.mark.parametrize("problem", ["empty", "non-string", "duplicate"])
def test_discover_injections_refuses_unusable_or_repeated_profile_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    _, target, record, payload = _recorded_injection(tmp_path, monkeypatch)
    rejected = record
    if problem == "duplicate":
        rejected = record.with_name("zz-second-record.json")
        rejected.write_bytes(record.read_bytes())
    else:
        payload["profile"] = "" if problem == "empty" else 123
        record.write_text(json.dumps(payload))

    with pytest.raises(SetforgeError) as failure:
        discover_injections(target)

    assert str(failure.value) == (
        f"project injection state has duplicate profile: {rejected}"
    )


@pytest.mark.parametrize(
    "problem", ["root-through-missing-directory", "config-missing"]
)
def test_discover_injections_refuses_config_paths_that_do_not_resolve_strictly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    config, target, record, payload = _recorded_injection(tmp_path, monkeypatch)
    if problem == "root-through-missing-directory":
        payload["config_root"] = str(
            config.parent.parent / "missing" / ".." / config.parent.name
        )
    else:
        payload["config_path"] = str(config.parent / "gone.yaml")
    record.write_text(json.dumps(payload))

    with pytest.raises(SetforgeError) as failure:
        discover_injections(target)

    assert str(failure.value) == (
        f"project injection config cannot be resolved safely: {record}"
    )


def test_discover_injections_refuses_config_path_that_is_not_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, target, record, payload = _recorded_injection(tmp_path, monkeypatch)
    payload["config_path"] = str(config.parent / "project")
    record.write_text(json.dumps(payload))

    with pytest.raises(SetforgeError) as failure:
        discover_injections(target)

    assert str(failure.value) == (
        f"project injection config is not a regular file: {config.parent / 'project'}"
    )


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


def test_two_way_merge_splits_independent_differences() -> None:
    result = two_way_merge(
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
def test_two_way_merge_resolves_every_hunk_explicitly(
    policy: AutoResolution, expected: bytes
) -> None:
    result = two_way_merge(
        b"one-local\nshared\ntwo-local\n",
        b"one-profile\nshared\ntwo-profile\n",
    )

    resolved = resolve_automatically(result, policy)

    assert resolved.clean
    assert resolved.merged() == expected


def test_two_way_merge_is_byte_exact_without_final_newline() -> None:
    result = two_way_merge(b"same\nlocal", b"same\nprofile")

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

    assert result.segments == (Clean(b"alpha: local\nbeta: profile\n"),)


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


def test_merge_project_content_falls_back_for_a_duplicate_json_key() -> None:
    result = merge_project_content(
        Path("settings.json"), b"{}", b'{"a":1}', b'{"a":1,"a":2}'
    )

    assert result.segments == (Conflict(b"{}", b'{"a":1}', b'{"a":1,"a":2}'),)


@pytest.mark.parametrize(
    ("base", "local", "profile", "expected"),
    [
        (b"a:   1", b"a:   1", b"a: 2\n", b"a: 2\n"),
        (b"a: 1\n", b"a:    1\n", b"a: 1\n", b"a:    1\n"),
        (b"a: 1\n", b"a:    2\n", b"a:    2\n", b"a:    2\n"),
    ],
    ids=["local-unchanged", "profile-unchanged", "both-agree"],
)
def test_merge_project_content_keeps_the_moved_side_verbatim(
    base: bytes, local: bytes, profile: bytes, expected: bytes
) -> None:
    result = merge_project_content(Path("settings.yaml"), base, local, profile)

    assert result.segments == (Clean(expected),)


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
    assert removed.exit_code == 1
    assert "drifted" in str(removed.exception)
    assert (target / "AGENTS.md").read_bytes() == (
        b"alpha-local\nbeta\ngamma-profile\n"
    )


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
        SetforgeError, match=r"^cannot render unresolved project sync manifests$"
    ):
        render_sync_manifests(plan)
    with pytest.raises(
        SetforgeError, match=r"^cannot apply an unresolved project sync plan$"
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
    with pytest.raises(SetforgeError) as unresolved:
        resolve_sync_plan(plan)
    assert str(unresolved.value) == (
        "project sync has an unresolved mode conflict in AGENTS.md; use --auto"
    )


def _injected_two_members(tmp_path: Path) -> tuple[Path, Path]:
    config = _config(tmp_path)
    (config.parent / "project" / "demo" / "EXTRA.md").write_text("extra\n")
    config.write_text(
        config.read_text()
        + "      extra:\n        src: EXTRA.md\n        dst: EXTRA.md\n"
    )
    target = _git_repo(tmp_path / "target")
    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.output
    return config, target


def test_missing_locally_is_true_only_for_a_cleanly_kept_deletion_of_a_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config, target = _injected_two_members(tmp_path)
    (target / "AGENTS.md").unlink()
    (target / "EXTRA.md").unlink()
    (config.parent / "project" / "demo" / "EXTRA.md").write_text("extra v2\n")

    kept, conflicted = plan_sync(target).files

    assert (kept.kind, kept.live, kept.result) == (
        SyncFileKind.UPDATE,
        ABSENT,
        MergeResult((), absent=True),
    )
    assert missing_locally(kept) is True
    assert (conflicted.kind, conflicted.live) == (SyncFileKind.UPDATE, ABSENT)
    assert not conflicted.result.clean
    assert missing_locally(conflicted) is False

    (target / "AGENTS.md").write_text("managed\n")
    config.write_text(
        config.read_text().split("      agents:")[0] + "      extra:\n"
        "        src: EXTRA.md\n        dst: EXTRA.md\n"
    )
    removed = plan_sync(target).files[0]
    assert (removed.kind, removed.result) == (
        SyncFileKind.REMOVE,
        MergeResult((), absent=True),
    )
    assert missing_locally(removed) is False


def test_use_profile_restores_a_missing_member_and_resolves_the_members_after_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config, target = _injected_two_members(tmp_path)
    (config.parent / "project" / "demo" / "AGENTS.md").chmod(0o755)
    (target / "AGENTS.md").unlink()
    plan = plan_sync(target)

    resolved = resolve_sync_plan(plan, auto=AutoResolution.USE_PROFILE)

    assert resolved is not None
    restored, untouched = resolved.files
    assert restored.relative_destination == Path("AGENTS.md")
    assert restored.result == MergeResult((Clean(b"managed\n"),))
    assert restored.result_mode == 0o755
    assert untouched == plan.files[1]
    assert untouched.relative_destination == Path("EXTRA.md")


def test_interactive_resolution_passes_the_conflict_to_the_wizard_and_can_defer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge.reconcile.types import file_id as reconcile_file_id
    from setforge.ui.primitives import CANCEL

    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.output
    (target / "AGENTS.md").write_text("local\n")
    (target / "AGENTS.md").chmod(0o600)
    (config.parent / "project" / "demo" / "AGENTS.md").write_text("profile\n")
    plan = plan_sync(target)
    assert plan.conflicts == 1
    assert (plan.files[0].result_mode, plan.files[0].desired_mode) == (0o600, 0o644)
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    answers: list[object] = [
        WizardResult(MergeResult((Clean(b"chosen\n"),)), False, ("edit",)),
        WizardResult(MergeResult((Clean(b"chosen\n"),)), True, (None,)),
        CANCEL,
    ]

    def wizard(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        return answers[len(calls) - 1]

    monkeypatch.setattr("setforge.reconcile.wizard.resolve_conflicts", wizard)

    expected_call = (
        (reconcile_file_id("demo/agents"), plan.files[0].result),
        {"display_path": "AGENTS.md"},
    )
    resolved = resolve_sync_plan(plan, interactive=True)
    assert calls == [expected_call]
    assert resolved is not None
    assert resolved.files[0].result == MergeResult((Clean(b"chosen\n"),))
    assert resolved.files[0].result_mode == 0o600
    assert resolve_sync_plan(plan, interactive=True) is None
    assert resolve_sync_plan(plan, interactive=True) is None
    assert calls == [expected_call] * 3


@pytest.mark.parametrize(("selection", "absent"), [("theirs", True), ("ours", False)])
def test_interactive_removal_of_an_edited_member_records_the_chosen_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str, absent: bool
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.output
    (target / "AGENTS.md").write_text("local edit\n")
    config.write_text(
        "tracked_files: {}\nprofiles: {}\nproject_profiles:\n  demo:\n    files: {}\n"
    )
    plan = plan_sync(target)
    assert plan.files[0].kind is SyncFileKind.REMOVE
    assert plan.files[0].desired_upstream is ABSENT
    assert plan.conflicts == 1
    monkeypatch.setattr(
        "setforge.reconcile.wizard.resolve_conflicts",
        lambda *args, **kwargs: WizardResult(
            MergeResult((Clean(b""),)), False, (selection,)
        ),
    )

    resolved = resolve_sync_plan(plan, interactive=True)

    assert resolved is not None
    assert resolved.files[0].result == (
        MergeResult((), absent=True) if absent else MergeResult((Clean(b""),))
    )


@pytest.mark.parametrize("git_target", [False, True])
def test_apply_sync_adds_and_removes_profile_membership_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_target: bool
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    original_config = config.read_text()
    target = tmp_path / "target"
    assert target.resolve().is_relative_to(tmp_path.resolve())
    if git_target:
        _git_repo(target)
    else:
        target.mkdir()
    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.exception
    (config.parent / "project" / "demo" / "EXTRA.md").write_text("extra\n")
    (config.parent / "project" / "demo" / "EXTRA.md").chmod(0o644)
    (config.parent / "project" / "demo" / "NEXT.md").write_text("next\n")
    (config.parent / "project" / "demo" / "NEXT.md").chmod(0o644)
    expanded_config = (
        original_config
        + "      extra:\n        src: EXTRA.md\n        dst: nested/deeper/EXTRA.md\n"
        + "      next:\n        src: NEXT.md\n        dst: nested-deeper/NEXT.md\n"
    )
    config.write_text(expanded_config)

    add_plan = plan_sync(target)
    assert [
        item.relative_destination for item in add_plan.files if item.kind.value == "add"
    ] == [Path("nested-deeper/NEXT.md"), Path("nested/deeper/EXTRA.md")]
    assert apply_sync(add_plan)
    assert (target / "nested" / "deeper" / "EXTRA.md").read_text() == "extra\n"
    assert (target / "nested-deeper" / "NEXT.md").read_text() == "next\n"
    if git_target:
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
        item
        for item in remove_plan.files
        if item.relative_destination == Path("nested/deeper/EXTRA.md")
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
    assert removed_file.stored is not None
    assert removed_file.addition is None
    assert apply_sync(remove_plan)
    assert not (target / "nested").exists()
    assert not (target / "nested-deeper").exists()

    config.write_text(expanded_config)
    restore_plan = plan_sync(target)
    assert apply_sync(restore_plan)
    assert (target / "nested" / "deeper" / "EXTRA.md").read_text() == "extra\n"
    assert (target / "nested-deeper" / "NEXT.md").read_text() == "next\n"
    if git_target:
        assert (
            subprocess.run(
                ["git", "-C", str(target), "status", "--short"],
                check=True,
                text=True,
                capture_output=True,
            ).stdout
            == ""
        )
        subprocess.run(
            ["git", "-C", str(target), "add", "-f", "nested/deeper/EXTRA.md"],
            check=True,
        )
        with pytest.raises(SetforgeError, match="unexpectedly became tracked"):
            plan_sync(target)


@pytest.mark.parametrize("local_edit", [False, True])
def test_sync_removing_replacement_restores_prior_file_or_keeps_local_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_edit: bool
) -> None:
    state = tmp_path / "state"
    target_path = tmp_path / "target"
    for path in (state, target_path):
        assert path.resolve().is_relative_to(tmp_path.resolve())
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    config = _config(tmp_path)
    original_config = config.read_text()
    target = _git_repo(target_path)
    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.output
    live = target / "EXTRA.md"
    live.write_bytes(b"prior\n")
    live.chmod(0o600)
    source = config.parent / "project" / "demo" / "EXTRA.md"
    assert source.resolve().is_relative_to(tmp_path.resolve())
    source.write_bytes(b"profile\n")
    source.chmod(0o644)
    config.write_text(
        original_config + "      extra:\n        src: EXTRA.md\n        dst: EXTRA.md\n"
    )
    addition = resolve_sync_plan(plan_sync(target), auto=AutoResolution.USE_PROFILE)
    assert addition is not None
    assert apply_sync(addition)
    assert live.read_bytes() == b"profile\n"
    assert stat.S_IMODE(live.stat().st_mode) == 0o644
    if local_edit:
        live.write_bytes(b"local edit\n")

    config.write_text(original_config)
    removal = plan_sync(target)
    removed = next(
        item for item in removal.files if item.relative_destination == Path("EXTRA.md")
    )
    assert removed.desired_upstream == b"prior\n"
    assert removed.desired_mode == 0o600
    assert removed.result_mode == 0o600
    assert bool(removal.conflicts) is local_edit
    synced = CliRunner().invoke(
        app,
        [
            "project",
            "sync",
            str(target),
            *(["--auto=keep-live"] if local_edit else []),
            "--yes",
        ],
    )
    assert synced.exit_code == 0, synced.output
    assert live.read_bytes() == (b"local edit\n" if local_edit else b"prior\n")
    assert stat.S_IMODE(live.stat().st_mode) == 0o600


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
    assert extra.addition is not None
    assert extra.addition.action is ProjectFileAction.OVERLAY
    assert extra.addition.applied_payload is None
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
    (config.parent / "project" / "demo" / "EXTRA.md").chmod(0o755)
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
    assert added.desired_mode == 0o755
    assert added.result_mode == 0o644
    assert added.mode_conflict
    assert added.addition is not None
    assert not added.result.clean
    with pytest.raises(SetforgeError, match="unresolved conflicts"):
        resolve_sync_plan(plan)
    assert (target / "EXTRA.md").read_bytes() == before
    kept = resolve_sync_plan(plan, auto=AutoResolution.KEEP_LIVE)
    assert kept is not None
    kept_extra = next(
        item for item in kept.files if item.relative_destination == Path("EXTRA.md")
    )
    assert kept_extra.result.merged() == b"local\n"
    assert kept_extra.result_mode == 0o644
    adopted = resolve_sync_plan(plan, auto=AutoResolution.USE_PROFILE)
    assert adopted is not None
    adopted_extra = next(
        item for item in adopted.files if item.relative_destination == Path("EXTRA.md")
    )
    assert adopted_extra.result.merged() == b"profile\n"
    assert adopted_extra.result_mode == 0o755
    assert apply_sync(adopted)
    assert (target / "EXTRA.md").read_text() == "profile\n"
    assert stat.S_IMODE((target / "EXTRA.md").stat().st_mode) == 0o755


@pytest.mark.parametrize("foreign", [False, True])
def test_sync_membership_add_reclaims_only_this_checkouts_released_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, foreign: bool
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

    store = OwnershipStore()
    (released,) = store.list_claims()
    claim_path = store.claim_path(released.resource_id)
    if foreign:
        other_owner = uuid.uuid4()
        claim_path.write_text(
            json.dumps(
                ownership_claim_to_json(
                    replace(
                        released,
                        owner_id=other_owner,
                        history=tuple(
                            replace(event, owner_id=other_owner)
                            for event in released.history
                        ),
                    )
                )
            )
            + "\n"
        )
    before = claim_path.read_bytes()

    plan = plan_sync(target)
    if foreign:
        with pytest.raises(SetforgeError) as failure:
            apply_sync(plan)
        assert str(failure.value) == (
            "a project destination has a released ownership claim from another "
            "config checkout: AGENTS.md was injected by project profile 'demo' at "
            f"{target} (owner {other_owner}); inject it from that checkout, or "
            "inspect the claim with `setforge ownership list`"
        )
        assert not (target / "AGENTS.md").exists()
        assert claim_path.read_bytes() == before
    else:
        assert apply_sync(plan)
        assert (target / "AGENTS.md").read_text() == "other\n"
        (claim,) = store.list_claims()
        assert claim.lifecycle.value == "claimed"
        assert claim.owner_id == released.owner_id
        assert claim.declaration_refs == ("project-profile:demo:other",)


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


@pytest.mark.parametrize("operation", ["sync", "remove"])
def test_project_apply_refuses_tracked_claim_published_after_planning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    from setforge.file_ownership import file_resource_id, observe_file
    from setforge.locking import mutation_locks
    from setforge.ownership import (
        OwnershipStore,
        load_or_create_owner_id,
        read_owner_id,
    )
    from setforge.project_injection import apply_removal, plan_removal

    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.exception
    live = target / "AGENTS.md"
    if operation == "sync":
        (config.parent / "project/demo/AGENTS.md").write_text("updated\n")
        sync_plan = plan_sync(target)
    else:
        removal_plan = plan_removal(profile="demo", target=target, config_path=config)
    other = _git_repo(tmp_path / "other-config")
    owner = load_or_create_owner_id(other)
    assert owner != read_owner_id(config.parent)
    store = OwnershipStore()
    observation = observe_file(live)
    with mutation_locks(resources=True):
        claim = store.claim_locked(
            resource_id=observation.resource_id,
            owner_id=owner,
            declaration_refs=("tracked_files.agents",),
            provenance=(),
            locator=str(live),
            fingerprint=observation.fingerprint,
            expected_generation=None,
        )
    before = {path: path.read_bytes() for path in state.rglob("*.json")}

    if operation == "sync":
        with pytest.raises(SetforgeError, match="active tracked-file ownership claim"):
            apply_sync(sync_plan)
    else:
        with pytest.raises(SetforgeError, match="active tracked-file ownership claim"):
            apply_removal(removal_plan)

    assert live.read_text() == "managed\n"
    assert store.read(file_resource_id(live)) == claim
    assert {path: path.read_bytes() for path in state.rglob("*.json")} == before


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
    from setforge import locking, operations
    from setforge import project_sync as sync_module

    expected_profile = (
        "project-sync-" + hashlib.sha256(str(target).encode()).hexdigest()[:24]
    )
    expected_configs = tuple(sorted((alpha.parent, beta.parent), key=str))
    original_refuse = sync_module.refuse_active_file_claims
    original_prepare = operations.prepare
    original_checkpoint = operations.begin_checkpoint
    journals: list[operations.OperationJournal] = []
    checkpoints: list[operations.OperationCheckpoint] = []
    checked_claims: list[bool] = []

    def check_claims(destinations: Iterable[Path]) -> None:
        held = locking._HELD_RANKS.get()
        locking.require_resources_lock()
        assert (locking.LockRank.PROFILE, expected_profile) in held
        checked_claims.append(True)
        return original_refuse(destinations)

    def capture_journal(**kwargs: Any) -> operations.OperationJournal:
        journal = original_prepare(**kwargs)
        journals.append(journal)
        return journal

    def capture_checkpoint(
        journal: operations.OperationJournal, **kwargs: Any
    ) -> operations.OperationJournal:
        result = original_checkpoint(journal, **kwargs)
        checkpoints.extend(result.checkpoints)
        return result

    monkeypatch.setattr(sync_module, "refuse_active_file_claims", check_claims)
    monkeypatch.setattr(operations, "prepare", capture_journal)
    monkeypatch.setattr(operations, "begin_checkpoint", capture_checkpoint)
    assert apply_sync(plan)
    assert checked_claims == [True]
    assert len(journals) == 1
    journal = journals[0]
    assert journal.profile == expected_profile
    assert journal.command == "project-sync"
    assert journal.resources_lock is True
    assert journal.reserved_config_dirs == expected_configs
    assert journal.reserved_profiles == (expected_profile,)
    assert len(checkpoints) == 1
    checkpoint = checkpoints[0]
    assert checkpoint.name == "synchronize-project-files-and-state"
    assert checkpoint.kind is operations.CheckpointKind.REVERSIBLE
    assert (
        checkpoint.recovery
        == "restore all project files, manifests, ownership, and visibility"
    )
    assert checkpoint.restore_state is False
    assert checkpoint.restore_transitions is False
    assert (target / "ALPHA.md").read_text() == "alpha-updated\n"
    assert (target / "BETA.md").read_text() == "beta-updated\n"

    beta.write_text(beta.read_text().replace("dst: BETA.md", "dst: ALPHA.md"))
    with pytest.raises(SetforgeError) as collision:
        plan_sync(target)
    assert str(collision.value) == (
        "project profiles 'alpha' and 'beta' both claim ALPHA.md"
    )
    assert (target / "ALPHA.md").read_text() == "alpha-updated\n"
    assert (target / "BETA.md").read_text() == "beta-updated\n"


@pytest.mark.parametrize("changed_side", ["live", "source"])
def test_apply_sync_refuses_file_drift_without_manifest_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_side: str
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
    live = target / "AGENTS.md"
    source = config.parent / "project/demo/AGENTS.md"
    (live if changed_side == "live" else source).write_text("concurrent edit\n")
    before = {path: path.read_bytes() for path in (tmp_path / "state").rglob("*.json")}
    before_live = live.read_bytes()
    with pytest.raises(
        SetforgeError, match=r"^project sync plan changed before apply; retry$"
    ):
        apply_sync(plan)
    assert live.read_bytes() == before_live
    assert {path: path.read_bytes() for path in before} == before


def test_public_sync_refuses_mismatched_existing_member_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge.locking import mutation_locks
    from setforge.ownership import OwnershipStore

    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.exception
    live = target / "AGENTS.md"
    (config.parent / "project/demo/AGENTS.md").write_text("updated\n")
    store = OwnershipStore()
    claim = next(item for item in store.list_claims() if item.locator == str(live))
    with mutation_locks(resources=True):
        changed = store.claim_locked(
            resource_id=claim.resource_id,
            owner_id=claim.owner_id,
            declaration_refs=claim.declaration_refs,
            provenance=claim.provenance,
            locator=claim.locator,
            fingerprint="different recorded fingerprint",
            expected_generation=claim.generation,
        )
    before = {path: path.read_bytes() for path in (tmp_path / "state").rglob("*.json")}
    refused = CliRunner().invoke(app, ["project", "sync", str(target), "--yes"])
    assert refused.exit_code != 0
    assert isinstance(refused.exception, SetforgeError)
    assert (
        str(refused.exception)
        == "project injection ownership state is missing or mismatched"
    )
    assert live.read_bytes() == b"managed\n"
    assert store.read(claim.resource_id) == changed
    assert {path: path.read_bytes() for path in before} == before


def test_sync_manifest_mode_is_private_independent_of_umask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge import atomicio

    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.exception
    (config.parent / "project/demo/AGENTS.md").write_text("updated\n")
    plan = plan_sync(target)
    manifest = plan.injections[0].manifest_path
    original_write = atomicio.atomic_write_bytes

    def restricted_write(path: Path, payload: bytes, **kwargs: Any) -> Path | None:
        if path != manifest:
            return original_write(path, payload, **kwargs)
        prior = os.umask(0o777)
        try:
            return original_write(path, payload, **kwargs)
        finally:
            os.umask(prior)

    monkeypatch.setattr(atomicio, "atomic_write_bytes", restricted_write)
    assert apply_sync(plan)
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600


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


@pytest.mark.parametrize(
    ("owner", "message"),
    [
        ("invalid", "project injection has invalid config ownership state"),
        (
            "00000000-0000-0000-0000-000000000001",
            "project injection belongs to a different config checkout",
        ),
    ],
)
def test_apply_sync_refuses_invalid_or_foreign_config_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: str, message: str
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.exception
    manifest = plan_sync(target).injections[0].manifest_path
    raw = json.loads(manifest.read_bytes())
    raw["config_owner_id"] = owner
    manifest.write_text(json.dumps(raw) + "\n")
    (config.parent / "project/demo/AGENTS.md").write_text("updated\n")
    plan = plan_sync(target)
    before_state = {path: path.read_bytes() for path in state.rglob("*.json")}

    with pytest.raises(SetforgeError) as failure:
        apply_sync(plan)

    assert str(failure.value) == message
    assert (target / "AGENTS.md").read_bytes() == b"managed\n"
    assert {path: path.read_bytes() for path in state.rglob("*.json")} == before_state


def test_apply_sync_rechecks_manifest_after_fresh_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge import project_sync

    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.exception
    (config.parent / "project/demo/AGENTS.md").write_text("updated\n")
    plan = plan_sync(target)
    manifest = plan.injections[0].manifest_path
    changed_manifest = manifest.read_bytes() + b"\n"
    before_state = {path: path.read_bytes() for path in state.rglob("*.json")}
    before_state[manifest] = changed_manifest

    def plan_then_external_write(target: Path) -> project_sync.ProjectSyncPlan:
        fresh = plan_sync(target)
        manifest.write_bytes(changed_manifest)
        return fresh

    monkeypatch.setattr(project_sync, "plan_sync", plan_then_external_write)
    with pytest.raises(SetforgeError) as failure:
        apply_sync(plan)

    assert str(failure.value) == "project sync plan changed before apply; retry"
    assert (target / "AGENTS.md").read_bytes() == b"managed\n"
    assert {path: path.read_bytes() for path in state.rglob("*.json")} == before_state


def test_apply_sync_refuses_addition_with_surviving_project_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge.locking import mutation_locks
    from setforge.ownership import OwnershipStore
    from setforge.project_injection import _resource_id

    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.exception
    (config.parent / "project/demo/EXTRA.md").write_text("extra\n")
    config.write_text(
        config.read_text()
        + "      extra:\n        src: EXTRA.md\n        dst: EXTRA.md\n"
    )
    plan = plan_sync(target)
    store = OwnershipStore()
    existing = next(iter(store.list_claims()))
    resource = _resource_id(
        target.stat().st_dev, target.stat().st_ino, Path("EXTRA.md")
    )
    with mutation_locks(resources=True):
        claim = store.claim_locked(
            resource_id=resource,
            owner_id=existing.owner_id,
            declaration_refs=("project-profile:demo:extra",),
            provenance=existing.provenance,
            locator=str(target / "EXTRA.md"),
            fingerprint="surviving claim after external file deletion",
            expected_generation=None,
        )
    before_state = {path: path.read_bytes() for path in state.rglob("*.json")}

    with pytest.raises(SetforgeError) as failure:
        apply_sync(plan)

    assert str(failure.value) == (
        "a project destination already has an active ownership claim: EXTRA.md is "
        f"injected by project profile 'demo' at {target}; run `setforge project "
        f"remove demo {target}` first"
    )
    assert not (target / "EXTRA.md").exists()
    assert store.read(resource) == claim
    assert {path: path.read_bytes() for path in state.rglob("*.json")} == before_state


@pytest.mark.parametrize("restore", [False, True])
def test_sync_reports_a_missing_member_and_keeps_or_restores_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restore: bool
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    (config.parent / "project/demo/AGENTS.md").chmod(0o755)
    target = _git_repo(tmp_path / "target")
    runner = CliRunner()
    injected = runner.invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.output
    live = target / "AGENTS.md"
    live.unlink()
    reported = (
        "  demo: missing locally (kept; --auto=use-profile restores it): AGENTS.md\n"
    )

    plan = plan_sync(target)
    assert plan.conflicts == 0
    assert plan.files[0].result == MergeResult((), absent=True)
    preview = runner.invoke(app, ["project", "sync", str(target), "--dry-run"])
    assert preview.exit_code == 0, preview.output
    assert reported in preview.output
    assert not live.exists()

    synced = runner.invoke(
        app,
        [
            "project",
            "sync",
            str(target),
            *(["--auto=use-profile"] if restore else []),
            "--yes",
        ],
    )
    assert synced.exit_code == 0, synced.output
    assert "\nsync complete\n" in synced.output
    listed = runner.invoke(app, ["project", "list"])
    assert listed.exit_code == 0, listed.output
    again = runner.invoke(app, ["project", "sync", str(target), "--dry-run"])
    if restore:
        assert live.read_bytes() == b"managed\n"
        assert stat.S_IMODE(live.stat().st_mode) == 0o755
        assert listed.output == f"{target}  [demo]\n  hidden: AGENTS.md\n"
        assert "  demo: unchanged: AGENTS.md\n" in again.output
    else:
        assert not live.exists()
        assert listed.output == f"{target}  [demo]\n  deleted-locally: AGENTS.md\n"
        assert reported in again.output
    assert not apply_sync(plan_sync(target))
    removed = runner.invoke(
        app,
        ["project", "remove", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert removed.exit_code == 0, removed.output
    assert not live.exists()


def test_sync_restores_a_missing_private_exclude_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    exclude = target / ".git" / "info" / "exclude"
    pristine = exclude.read_bytes()
    runner = CliRunner()
    injected = runner.invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.output
    claimed = exclude.read_bytes()
    assert claimed != pristine
    intact = runner.invoke(app, ["project", "sync", str(target), "--dry-run"])
    assert "  demo: unchanged: AGENTS.md\n" in intact.output
    exclude.write_bytes(pristine)
    assert runner.invoke(app, ["project", "list"]).exit_code == 1

    assert plan_sync(target).files[0].restore_hidden_claim
    preview = runner.invoke(app, ["project", "sync", str(target), "--dry-run"])
    assert preview.exit_code == 0, preview.output
    assert (
        "  demo: update (restore private exclude claim): AGENTS.md\n" in preview.output
    )
    assert exclude.read_bytes() == pristine
    synced = runner.invoke(app, ["project", "sync", str(target), "--yes"])

    assert synced.exit_code == 0, synced.output
    assert "\nsync complete\n" in synced.output
    assert exclude.read_bytes() == claimed
    assert runner.invoke(app, ["project", "list"]).exit_code == 0
    assert not plan_sync(target).files[0].restore_hidden_claim
    again = runner.invoke(app, ["project", "sync", str(target), "--yes"])
    assert "\nno changes: project is already current\n" in again.output


def test_sync_refuses_resolved_content_without_a_file_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.exception
    (target / "AGENTS.md").unlink()
    (config.parent / "project/demo/AGENTS.md").write_text("new\n")
    resolved = resolve_sync_plan(plan_sync(target), auto=AutoResolution.USE_PROFILE)
    assert resolved is not None
    assert resolved.conflicts == 0
    assert resolved.files[0].result_mode == 0o644
    resolved = replace(resolved, files=(replace(resolved.files[0], result_mode=None),))
    before_state = {path: path.read_bytes() for path in state.rglob("*.json")}

    with pytest.raises(SetforgeError) as failure:
        apply_sync(resolved)

    assert str(failure.value) == "project sync result has no file mode"
    assert not (target / "AGENTS.md").exists()
    assert {path: path.read_bytes() for path in state.rglob("*.json")} == before_state


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("file_id", ""),
        ("file_id", 7),
        ("declaring_profile", ""),
        ("declaring_profile", 7),
        ("source", None),
        ("source_digest", None),
        ("source_digest", "0" * 64),
        ("created_parents", None),
        ("created_parents", [7]),
        ("created_parents", ["."]),
        ("created_parents", ["../parent"]),
        ("destination", "."),
        ("destination", "../outside"),
        ("previous_payload", "%%%"),
        ("previous_payload", "cHJpb3I="),
        ("previous_mode", 0o640),
        ("previous_mode", True),
        ("applied_payload", "%%%"),
        ("applied_payload", None),
        ("applied_digest", None),
        ("applied_digest", "0" * 64),
        ("applied_mode", None),
        ("upstream_payload", "%%%"),
        ("upstream_payload", None),
        ("upstream_mode", True),
        ("upstream_mode", None),
        ("action", "unknown"),
        ("duplicate_destination", None),
        ("duplicate_id", None),
        ("destination_absolute", None),
        ("absolute_parent", None),
        ("partial_retained_preimage", None),
        ("legacy_digest", None),
    ],
)
@pytest.mark.parametrize("dry_run", [False, True])
def test_public_sync_rejects_malformed_file_record_without_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
    dry_run: bool,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    assert transitions.state_root().is_relative_to(tmp_path)
    assert OwnershipStore().root.is_relative_to(tmp_path)
    assert locking._user_global_locks_dir().is_relative_to(tmp_path)
    runner = CliRunner()
    injected = runner.invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, (injected.output, injected.exception)
    record = next((tmp_path / "state/project-injections").glob("*.json"))
    document = json.loads(record.read_text())
    entry = document["files"][0]
    if field.startswith("duplicate"):
        other = copy.deepcopy(entry)
        other["file_id" if field == "duplicate_destination" else "destination"] = (
            "OTHER"
        )
        document["files"].append(other)
    elif field == "destination_absolute":
        entry["destination"] = str(tmp_path / "outside")
    elif field == "absolute_parent":
        entry["destination"] = "nested/AGENTS.md"
        entry["created_parents"] = [str(target / "nested")]
    elif field == "partial_retained_preimage":
        entry["action"] = "retain-identical"
        entry["previous_payload"] = base64.b64encode(b"managed\n").decode()
    elif field == "legacy_digest":
        document["schema"] = 1
        del document["config_path"]
        for key in (
            "visibility",
            "applied_payload",
            "upstream_payload",
            "upstream_mode",
        ):
            del entry[key]
        entry["applied_digest"] = "0" * 64
    else:
        entry[field] = value
    record.write_text(json.dumps(document))
    before = {
        p.relative_to(tmp_path): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
        for p in tmp_path.rglob("*")
        if p.is_file() and "locks" not in p.parts
    }
    args = ["project", "sync", str(target), "--yes"]
    if dry_run:
        args.append("--dry-run")
    result = runner.invoke(app, args)
    assert result.exit_code != 0
    assert isinstance(result.exception, SetforgeError), result.exception
    assert str(result.exception) not in ("", "None")
    after = {
        p.relative_to(tmp_path): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
        for p in tmp_path.rglob("*")
        if p.is_file() and "locks" not in p.parts
    }
    assert after == before


@pytest.mark.parametrize("schema", [1, 2, 3])
@pytest.mark.parametrize("preexisting", [False, True])
def test_public_sync_source_and_membership_preserve_exact_retirement_preimages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema: int, preexisting: bool
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    assert transitions.state_root().is_relative_to(tmp_path)
    assert OwnershipStore().root.is_relative_to(tmp_path)
    assert locking._user_global_locks_dir().is_relative_to(tmp_path)
    original = b"prior user content\n"
    live = target / "AGENTS.md"
    if preexisting:
        live.write_bytes(original)
        live.chmod(0o640)
    control = target / "CONTROL.txt"
    control.write_bytes(b"unrelated\n")
    control.chmod(0o600)
    control_inode = control.stat().st_ino
    subprocess.run(["git", "add", "CONTROL.txt"], cwd=target, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "control",
        ],
        cwd=target,
        check=True,
    )
    index_before = (target / ".git/index").read_bytes()
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
            "--auto=use-profile",
            "--yes",
        ],
    )
    assert injected.exit_code == 0, (injected.output, injected.exception)
    record = next((state / "project-injections").glob("*.json"))
    document = json.loads(record.read_text())
    if schema < 3:
        document["schema"] = schema
        for entry in document["files"]:
            del entry["visibility"]
            if schema == 1:
                for field in ("applied_payload", "upstream_payload", "upstream_mode"):
                    del entry[field]
        if schema == 1:
            del document["config_path"]
        record.write_text(json.dumps(document))
    source = config.parent / "project/demo/NEW.md"
    source.write_bytes(b"new profile\n")
    source.chmod(0o750)
    (source.parent / "EXTRA.md").write_bytes(b"new member\n")
    config.write_text(
        config.read_text().replace("src: AGENTS.md", "src: NEW.md")
        + "      extra:\n        src: EXTRA.md\n        dst: nested/EXTRA.md\n"
    )
    updated = runner.invoke(
        app, ["project", "sync", str(target), "--auto=use-profile", "--yes"]
    )
    assert updated.exit_code == 0, (updated.output, updated.exception)
    assert live.read_bytes() == b"new profile\n"
    assert stat.S_IMODE(live.stat().st_mode) == 0o750
    assert (target / "nested/EXTRA.md").read_bytes() == b"new member\n"
    document = json.loads(record.read_text())
    agents = next(e for e in document["files"] if e["file_id"] == "agents")
    assert document["schema"] == 3
    assert agents["source"] == str(source)
    assert agents["previous_payload"] == (
        base64.b64encode(original).decode() if preexisting else None
    )
    assert agents["previous_mode"] == (0o640 if preexisting else None)
    claim = next(c for c in OwnershipStore().list_claims() if c.locator == str(live))
    expected = json.dumps(
        {
            "digest": hashlib.sha256(b"new profile\n").hexdigest(),
            "mode": 0o750,
            "path": "AGENTS.md",
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    assert claim.fingerprint == hashlib.sha256(expected).hexdigest()
    assert claim.declaration_refs == ("project-profile:demo:agents",)
    repeated = runner.invoke(app, ["project", "sync", str(target), "--yes"])
    assert repeated.exit_code == 0, (repeated.output, repeated.exception)
    assert "already current" in repeated.output
    removed = runner.invoke(
        app,
        ["project", "remove", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert removed.exit_code == 0, (removed.output, removed.exception)
    if preexisting:
        assert live.read_bytes() == original
        assert stat.S_IMODE(live.stat().st_mode) == 0o640
    else:
        assert not live.exists()
    assert not (target / "nested").exists()
    assert not record.exists()
    assert control.read_bytes() == b"unrelated\n"
    assert stat.S_IMODE(control.stat().st_mode) == 0o600
    assert control.stat().st_ino == control_inode
    assert (target / ".git/index").read_bytes() == index_before


@pytest.mark.parametrize(
    ("change", "extension"),
    [("signed-fields", "json"), ("root-list", "json"), ("root-list", "yaml")],
)
def test_public_jsonc_project_sync_preserves_signed_values_and_root_replacements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str, extension: str
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    config.write_text(config.read_text().replace("AGENTS.md", f"settings.{extension}"))
    source = config.parent / f"project/demo/settings.{extension}"
    base = (
        b'{"n": -1, "left": 0, "right": 0}\n'
        if change == "signed-fields"
        else b'{"key": 1}\n'
    )
    source.write_bytes(base)
    target = _git_repo(tmp_path / "target")
    assert transitions.state_root().is_relative_to(tmp_path)
    assert OwnershipStore().root.is_relative_to(tmp_path)
    assert locking._user_global_locks_dir().is_relative_to(tmp_path)
    runner = CliRunner()
    initial = runner.invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert initial.exit_code == 0, (initial.output, initial.exception)
    live = target / f"settings.{extension}"
    if change == "signed-fields":
        live.write_bytes(b'{"n": -1, "left": 2, "right": 0}\n')
        source.write_bytes(b'{"n": -1, "left": 0, "right": 3}\n')
        expected: object = {"n": -1, "left": 2, "right": 3}
    else:
        source.write_bytes(b"[1, 2]\n")
        expected = [1, 2]
    synced = runner.invoke(app, ["project", "sync", str(target), "--yes"])
    assert synced.exit_code == 0, (synced.output, synced.exception)
    if extension == "yaml":
        from ruamel.yaml import YAML

        assert YAML().load(live.read_bytes()) == expected
    else:
        assert json.loads(live.read_bytes()) == expected


@pytest.mark.parametrize(
    ("extension", "base", "local", "profile", "expected"),
    [
        (
            "yaml",
            b"a:   1\nb:  [1,2]   # keep\nc: 'x'\n",
            b"a:   1\nb:  [1,2]   # keep\nc: 'x'\nhost: true\n",
            b"a:   2\nb:  [1,2]   # keep\nc: 'x'\n",
            b"a:   2\nb:  [1,2]   # keep\nc: 'x'\nhost: true\n",
        ),
        (
            "json",
            b'{\n    "a": 1,\n    "b": [1,2],   // keep\n    "c": "x"\n}\n',
            b'{\n    "a": 1,\n    "b": [1,2],   // keep\n    "c": "x",\n'
            b'    "host": true\n}\n',
            b'{\n    "a": 1,\n    "b": [1,2],   // keep\n    "c": "y"\n}\n',
            b'{\n    "a": 1,\n    "b": [1,2],   // keep\n    "c": "y",\n'
            b'    "host": true\n}\n',
        ),
        (
            "json",
            b'[\n  {"n": 1},\n  {"n": 2}\n]\n',
            b'[\n  {"n": 1},\n  {"n": 2},\n  {"n": 9}\n]\n',
            b'[\n  {"n": 7},\n  {"n": 2}\n]\n',
            b'[\n  {"n": 7},\n  {"n": 2},\n  {"n": 9}\n]\n',
        ),
    ],
    ids=["yaml", "json-object", "json-array-root"],
)
def test_public_sync_merges_a_structured_member_to_the_bytes_install_gives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extension: str,
    base: bytes,
    local: bytes,
    profile: bytes,
    expected: bytes,
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    config.write_text(config.read_text().replace("AGENTS.md", f"settings.{extension}"))
    source = config.parent / f"project/demo/settings.{extension}"
    source.write_bytes(base)
    target = _git_repo(tmp_path / "target")
    runner = CliRunner()
    injected = runner.invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, (injected.output, injected.exception)
    live = target / f"settings.{extension}"
    live.write_bytes(local)
    source.write_bytes(profile)

    synced = runner.invoke(app, ["project", "sync", str(target), "--yes"])

    assert synced.exit_code == 0, (synced.output, synced.exception)
    assert live.read_bytes() == expected
    fid = reconcile_file_id("settings")
    with profile_lock("install"):
        record_base("install", fid, base=base, local=local)
    fmt = structured_format(live)
    assert fmt is not None
    installed = reconcile_file("install", fid, live=local, tracked=profile, fmt=fmt)
    assert installed.content == expected


def test_sync_keeps_recording_a_member_listed_after_a_removed_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    one_member = config.read_text().replace("dst: AGENTS.md", "dst: ZETA.md")
    config.write_text(
        config.read_text()
        + "      zeta:\n        src: AGENTS.md\n        dst: ZETA.md\n"
    )
    target = _git_repo(tmp_path / "target")
    runner = CliRunner()
    injected = runner.invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.output
    record = next((tmp_path / "state" / "project-injections").glob("*.json"))
    config.write_text(one_member)

    synced = runner.invoke(app, ["project", "sync", str(target), "--yes"])

    assert synced.exit_code == 0, synced.output
    assert not (target / "AGENTS.md").exists()
    assert (target / "ZETA.md").read_text() == "managed\n"
    assert [
        entry["destination"] for entry in json.loads(record.read_text())["files"]
    ] == ["ZETA.md"]


def test_rendered_record_has_sorted_keys_whatever_order_the_stored_one_had(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, target, record, payload = _recorded_injection(tmp_path, monkeypatch)
    canonical = record.read_bytes()
    record.write_text(json.dumps(dict(reversed(payload.items()))))
    assert record.read_bytes() != canonical

    assert render_sync_manifests(plan_sync(target)) == {record: canonical}


def test_ordinary_member_with_adjacent_edits_on_both_sides_is_a_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a file overlaid on tracked content replays profile edits by line."""
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    source = config.parent / "project" / "demo" / "AGENTS.md"
    source.write_text("one\ntwo\n")
    target = _git_repo(tmp_path / "target")
    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.output
    (target / "AGENTS.md").write_text("ONE\ntwo\n")
    source.write_text("one\nTWO\n")

    plan = plan_sync(target)

    assert plan.conflicts == 1
    assert plan.files[0].result.segments == (
        Conflict(base=b"one\ntwo\n", ours=b"ONE\ntwo\n", theirs=b"one\nTWO\n"),
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("previous_mode", -1, "an invalid file record"),
        ("previous_payload", "not base64", "invalid previous payload"),
        ("applied_payload", "not base64", "invalid applied payload"),
        ("upstream_payload", "not base64", "invalid upstream payload"),
        ("source_digest", "0" * 64, "an inconsistent file record"),
        ("previous_mode", 0o644, "an inconsistent file record"),
        ("created_parents", [1], "an invalid parent record"),
    ],
)
def test_project_sync_names_what_is_wrong_with_a_damaged_file_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
    message: str,
) -> None:
    _, target, record, payload = _recorded_injection(tmp_path, monkeypatch)
    payload["files"][0][field] = value
    record.write_text(json.dumps(payload))
    damaged = record.read_bytes()

    refused = CliRunner().invoke(app, ["project", "sync", str(target), "--yes"])

    assert refused.exit_code == 1
    assert str(refused.exception) == f"project injection state has {message}"
    assert record.read_bytes() == damaged
    assert (target / "AGENTS.md").read_text() == "managed\n"


def test_sync_plan_reports_the_recorded_profile_source_of_each_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, target, _, _ = _recorded_injection(tmp_path, monkeypatch)

    stored = plan_sync(target).files[0].stored

    assert stored is not None
    assert stored.source == config.parent / "project" / "demo" / "AGENTS.md"


def test_sync_plan_refuses_a_record_whose_file_id_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, target, record, payload = _recorded_injection(tmp_path, monkeypatch)
    payload["files"][0]["file_id"] = ""
    record.write_text(json.dumps(payload))

    with pytest.raises(SetforgeError) as failure:
        plan_sync(target)

    assert str(failure.value) == "project injection state has an invalid file record"
