from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from setforge.cli import app
from setforge.git_visibility import (
    apply_claims,
    info_exclude_path,
    plan_claims,
    read_claims,
)
from setforge.locking import TargetLockGuard
from setforge.ownership import (
    Authority,
    ClaimEvent,
    ClaimLifecycle,
    OwnershipClaim,
    OwnershipStore,
    ownership_claim_to_json,
    resolve_owner_common_dir,
)
from setforge.project_injection import ProjectInjectionPlan, manifest_path


@pytest.fixture(autouse=True)
def _candidate_filter_entrypoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Make Git filter children execute this exact source candidate."""
    binary_dir = tmp_path / "candidate-bin"
    binary_dir.mkdir()
    entrypoint = binary_dir / "setforge"
    entrypoint.write_text(
        f"#!{sys.executable}\nfrom setforge.cli import main\nmain()\n"
    )
    entrypoint.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parents[1]))


def _git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def _config(tmp_path: Path) -> Path:
    config_repo = tmp_path / "config"
    source = config_repo / "project" / "demo" / "AGENTS.md"
    source.parent.mkdir(parents=True)
    source.write_text("managed instructions\n")
    config = config_repo / "setforge.yaml"
    config.write_text(
        "tracked_files: {}\n"
        "profiles: {}\n"
        "project_profiles:\n"
        "  demo:\n"
        "    files:\n"
        "      instructions:\n"
        "        src: AGENTS.md\n"
        "        dst: AGENTS.md\n"
    )
    subprocess.run(["git", "init", "-q", str(config_repo)], check=True)
    return config


def test_project_inject_and_remove_round_trip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")

    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.output
    assert (target / "AGENTS.md").read_text() == "managed instructions\n"
    assert "Git visibility: hidden" in injected.output
    assert (
        subprocess.run(
            ["git", "-C", str(target), "status", "--short"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        == ""
    )

    removed = CliRunner().invoke(
        app,
        ["project", "remove", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert removed.exit_code == 0, removed.output
    assert not (target / "AGENTS.md").exists()


def test_project_inject_and_remove_in_plain_directory(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = tmp_path / "plain-target"
    target.mkdir()

    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )

    assert injected.exit_code == 0, injected.exception
    assert (target / "AGENTS.md").read_text() == "managed instructions\n"
    assert "Git visibility: not applicable" in injected.output
    assert not (target / ".git").exists()

    (config.parent / "project" / "demo" / "AGENTS.md").write_text("updated\n")
    synced = CliRunner().invoke(
        app,
        ["project", "sync", str(target), "--auto", "use-profile", "--yes"],
    )
    repeated = CliRunner().invoke(
        app,
        ["project", "sync", str(target), "--auto", "use-profile", "--yes"],
    )
    assert synced.exit_code == repeated.exit_code == 0
    assert (target / "AGENTS.md").read_text() == "updated\n"
    assert "no changes" in repeated.output
    assert not (target / ".git").exists()

    removed = CliRunner().invoke(
        app,
        ["project", "remove", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert removed.exit_code == 0, removed.exception
    assert not (target / "AGENTS.md").exists()
    assert not (target / ".git").exists()


def test_tracked_injection_uses_interactive_wizard_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge.reconcile.merge_model import Clean, MergeResult
    from setforge.reconcile.wizard import WizardResult

    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        "setforge.cli.project.sys",
        SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True)),
    )
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    destination = target / "AGENTS.md"
    destination.write_text("local\n")
    subprocess.run(["git", "-C", str(target), "add", "AGENTS.md"], check=True)
    monkeypatch.setattr(
        "setforge.reconcile.wizard.resolve_conflicts",
        lambda *args, **kwargs: WizardResult(
            MergeResult((Clean(b"wizard selected\n"),)), False, ("theirs",)
        ),
    )

    result = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )

    assert result.exit_code == 0, result.exception
    assert destination.read_bytes() == b"wizard selected\n"


def test_tracked_injection_wizard_cancel_and_non_tty_leave_state_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge.reconcile.merge_model import Clean, MergeResult
    from setforge.reconcile.wizard import WizardResult
    from setforge.ui.primitives import CANCEL

    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    destination = target / "AGENTS.md"
    destination.write_text("local\n")
    subprocess.run(["git", "-C", str(target), "add", "AGENTS.md"], check=True)
    command_sys = SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("setforge.cli.project.sys", command_sys)
    monkeypatch.setattr(
        "setforge.reconcile.wizard.resolve_conflicts", lambda *args, **kwargs: CANCEL
    )
    command = [
        "project",
        "inject",
        "demo",
        str(target),
        "--config",
        str(config),
        "--yes",
    ]

    cancelled = CliRunner().invoke(app, command)
    assert cancelled.exit_code == 0
    assert "aborted" in cancelled.output
    assert destination.read_text() == "local\n"
    assert not manifest_path(target, "demo").exists()

    monkeypatch.setattr(
        "setforge.reconcile.wizard.resolve_conflicts",
        lambda *args, **kwargs: WizardResult(
            MergeResult((Clean(b"local\n"),)), True, ("skip",)
        ),
    )
    deferred = CliRunner().invoke(app, command)
    assert deferred.exit_code == 0
    assert "aborted" in deferred.output
    assert destination.read_text() == "local\n"
    assert not manifest_path(target, "demo").exists()
    assert (
        subprocess.run(
            [
                "git",
                "-C",
                str(target),
                "config",
                "--local",
                "--get-regexp",
                "^filter\\.setforge-project\\.",
            ],
            check=False,
            capture_output=True,
        ).returncode
        == 1
    )
    assert not (_git_dir_for_test(target) / "info" / "attributes").exists()
    assert not OwnershipStore().list_claims()

    command_sys.stdin = SimpleNamespace(isatty=lambda: False)
    non_tty = CliRunner().invoke(app, command)
    assert non_tty.exit_code == 1
    assert non_tty.exception is not None
    assert "use a TTY or --auto" in str(non_tty.exception)
    assert destination.read_text() == "local\n"
    assert not manifest_path(target, "demo").exists()


def _git_dir_for_test(target: Path) -> Path:
    raw = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "--git-dir"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    git_dir = Path(raw)
    return (target / git_dir).resolve() if not git_dir.is_absolute() else git_dir


def test_project_inject_tracks_only_unrelated_edits_and_removes_local_hunk(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    (config.parent / "project" / "demo" / "AGENTS.md").write_text(
        "team instructions\nmanaged instructions\n"
    )
    target = _git_repo(tmp_path / "target")
    destination = target / "AGENTS.md"
    destination.write_text("team instructions\n")
    subprocess.run(["git", "-C", str(target), "add", "AGENTS.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(target),
            "-c",
            "user.name=SetForge Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "base",
        ],
        check=True,
    )

    result = CliRunner().invoke(
        app,
        [
            "project",
            "inject",
            "demo",
            str(target),
            "--config",
            str(config),
            "--auto",
            "use-profile",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.exception
    assert destination.read_text() == "team instructions\nmanaged instructions\n"
    assert (
        subprocess.run(
            ["git", "-C", str(target), "diff", "--", "AGENTS.md"],
            check=True,
            capture_output=True,
        ).stdout
        == b""
    )

    destination.write_text("team instructions edited\nmanaged instructions\n")
    repeated = CliRunner().invoke(
        app,
        [
            "project",
            "inject",
            "demo",
            str(target),
            "--config",
            str(config),
            "--auto",
            "use-profile",
            "--yes",
        ],
    )
    assert repeated.exit_code == 0, repeated.exception
    assert destination.read_text() == "team instructions edited\nmanaged instructions\n"
    diff = subprocess.run(
        ["git", "-C", str(target), "diff", "--", "AGENTS.md"],
        check=True,
        capture_output=True,
    ).stdout
    assert b"team instructions edited" in diff
    assert b"managed instructions" not in diff

    (config.parent / "project" / "demo" / "AGENTS.md").write_text(
        "team instructions\nmanaged instructions v2\n"
    )
    synced = CliRunner().invoke(
        app,
        ["project", "sync", str(target), "--auto", "use-profile", "--yes"],
    )
    assert synced.exit_code == 0, synced.exception
    assert destination.read_text() == (
        "team instructions edited\nmanaged instructions v2\n"
    )
    synced_diff = subprocess.run(
        ["git", "-C", str(target), "diff", "--", "AGENTS.md"],
        check=True,
        capture_output=True,
    ).stdout
    assert b"team instructions edited" in synced_diff
    assert b"managed instructions v2" not in synced_diff

    removed = CliRunner().invoke(
        app,
        ["project", "remove", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert removed.exit_code == 0, removed.exception
    assert destination.read_text() == "team instructions edited\n"


def test_replace_untracked_restores_exact_bytes_and_mode(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    destination = target / "AGENTS.md"
    destination.write_bytes(b"private original\x00\n")
    destination.chmod(0o640)

    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.exception
    assert destination.read_bytes() == b"managed instructions\n"

    removed = CliRunner().invoke(
        app,
        ["project", "remove", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert removed.exit_code == 0, removed.exception
    assert destination.read_bytes() == b"private original\x00\n"
    assert stat_mode(destination) == 0o640


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def _rewrite_only_claim(
    transform: Callable[[OwnershipClaim], OwnershipClaim],
) -> tuple[Path, bytes]:
    store = OwnershipStore()
    claims = store.list_claims()
    assert len(claims) == 1
    claim = claims[0]
    claim_path = store.claim_path(claim.resource_id)
    original = claim_path.read_bytes()
    updated = transform(claim)
    claim_path.write_text(json.dumps(ownership_claim_to_json(updated)) + "\n")
    return claim_path, original


def test_identical_injection_is_idempotent_and_source_change_refuses(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")

    first = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    second = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert first.exit_code == second.exit_code == 0
    assert "already current" in second.output

    (config.parent / "project" / "demo" / "AGENTS.md").write_text("updated\n")
    changed = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert changed.exit_code == 1
    assert changed.exception is not None
    assert "changed since injection" in str(changed.exception)
    assert (target / "AGENTS.md").read_text() == "managed instructions\n"


@pytest.mark.parametrize("claim_state", ["missing", "released", "mismatched"])
def test_idempotent_reinject_requires_exact_active_claim(
    tmp_path: Path, monkeypatch, claim_state: str
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    command = [
        "project",
        "inject",
        "demo",
        str(target),
        "--config",
        str(config),
        "--yes",
    ]
    assert CliRunner().invoke(app, command).exit_code == 0
    state = manifest_path(target, "demo")
    before_manifest = state.read_bytes()
    store = OwnershipStore()
    claims = store.list_claims()
    assert len(claims) == 1
    claim = claims[0]
    claim_path = store.claim_path(claim.resource_id)
    if claim_state == "missing":
        claim_path.unlink()
        expected_claim = None
    else:
        if claim_state == "released":
            generation = claim.generation + 1
            claim = replace(
                claim,
                authority=Authority.NONE,
                lifecycle=ClaimLifecycle.RELEASED,
                generation=generation,
                history=(
                    *claim.history,
                    ClaimEvent("release", claim.owner_id, generation),
                ),
            )
        else:
            claim = replace(claim, fingerprint="mismatched")
        claim_path.write_text(json.dumps(ownership_claim_to_json(claim)) + "\n")
        expected_claim = claim_path.read_bytes()

    reinjected = CliRunner().invoke(app, command)
    assert reinjected.exit_code == 1
    assert reinjected.exception is not None
    assert "ownership state is missing or mismatched" in str(reinjected.exception)
    assert state.read_bytes() == before_manifest
    assert (target / "AGENTS.md").read_text() == "managed instructions\n"
    if expected_claim is not None:
        assert claim_path.read_bytes() == expected_claim
    else:
        assert not claim_path.exists()


@pytest.mark.parametrize("request_kind", ["dry-run", "unconfirmed"])
def test_existing_injection_missing_owner_identity_never_recreates_it(
    tmp_path: Path, monkeypatch, request_kind: str
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    base_command = [
        "project",
        "inject",
        "demo",
        str(target),
        "--config",
        str(config),
    ]
    assert CliRunner().invoke(app, [*base_command, "--yes"]).exit_code == 0
    owner_path = resolve_owner_common_dir(config.parent) / "setforge" / "owner-id"
    owner_path.unlink()
    state = manifest_path(target, "demo")
    before_manifest = state.read_bytes()
    claim_path, before_claim = _rewrite_only_claim(lambda claim: claim)
    command = (
        [*base_command, "--dry-run"] if request_kind == "dry-run" else base_command
    )

    result = CliRunner().invoke(app, command)
    assert result.exit_code == 1
    assert not owner_path.exists()
    assert state.read_bytes() == before_manifest
    assert (target / "AGENTS.md").read_text() == "managed instructions\n"
    assert claim_path.read_bytes() == before_claim


def test_remove_missing_owner_identity_never_recreates_it(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    assert (
        CliRunner()
        .invoke(
            app,
            [
                "project",
                "inject",
                "demo",
                str(target),
                "--config",
                str(config),
                "--yes",
            ],
        )
        .exit_code
        == 0
    )
    owner_path = resolve_owner_common_dir(config.parent) / "setforge" / "owner-id"
    owner_path.unlink()
    state = manifest_path(target, "demo")
    before_manifest = state.read_bytes()
    before_file = (target / "AGENTS.md").read_bytes()
    claim_path, before_claim = _rewrite_only_claim(lambda claim: claim)

    removed = CliRunner().invoke(
        app,
        ["project", "remove", "demo", str(target), "--config", str(config), "--yes"],
    )

    assert removed.exit_code == 1
    assert not owner_path.exists()
    assert state.read_bytes() == before_manifest
    assert (target / "AGENTS.md").read_bytes() == before_file
    assert claim_path.read_bytes() == before_claim


def test_remove_refuses_drift_and_preserves_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    result = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert result.exit_code == 0, result.exception
    state = manifest_path(target, "demo")
    (target / "AGENTS.md").write_text("local drift\n")

    reinjected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert reinjected.exit_code == 1
    assert reinjected.exception is not None
    assert "drifted" in str(reinjected.exception)

    removed = CliRunner().invoke(
        app,
        ["project", "remove", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert removed.exit_code == 1
    assert removed.exception is not None
    assert "drifted" in str(removed.exception)
    assert state.exists()
    assert (target / "AGENTS.md").read_text() == "local drift\n"


def test_dry_run_and_noninteractive_confirmation_do_not_mutate(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    args = ["project", "inject", "demo", str(target), "--config", str(config)]

    dry = CliRunner().invoke(app, [*args, "--dry-run"])
    assert dry.exit_code == 0
    assert "dry run" in dry.output
    assert not (target / "AGENTS.md").exists()
    assert not manifest_path(target, "demo").exists()

    refused = CliRunner().invoke(app, args)
    assert refused.exit_code == 1
    assert refused.exception is not None
    assert "requires --yes" in str(refused.exception)
    assert not (target / "AGENTS.md").exists()


def test_visibility_flags_are_exclusive_and_tracked_intent_is_only_recorded(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    base = ["project", "inject", "demo", str(target), "--config", str(config)]

    invalid = CliRunner().invoke(app, [*base, "--git-hidden", "--git-tracked", "--yes"])
    assert invalid.exit_code == 1
    assert invalid.exception is not None
    assert "mutually exclusive" in str(invalid.exception)

    tracked = CliRunner().invoke(app, [*base, "--git-tracked", "--yes"])
    assert tracked.exit_code == 0, tracked.exception
    assert "Git visibility: tracked" in tracked.output
    status = subprocess.run(
        ["git", "-C", str(target), "status", "--short"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    assert status == "?? AGENTS.md\n"
    assert (
        not (target / ".git" / "info" / "exclude").read_text().endswith("AGENTS.md\n")
    )


def test_corrupt_manifest_fails_closed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    state = manifest_path(target, "demo")
    state.parent.mkdir(parents=True)
    state.write_text("{}")

    corrupt = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert corrupt.exit_code == 1
    assert corrupt.exception is not None
    assert "unsupported schema" in str(corrupt.exception)
    assert not (target / "AGENTS.md").exists()


def test_full_preflight_and_mid_apply_failure_leave_target_unchanged(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    second_source = config.parent / "project" / "demo" / "SECOND.md"
    second_source.write_text("second\n")
    config.write_text(
        config.read_text()
        + "      second:\n"
        + "        src: SECOND.md\n"
        + "        dst: SECOND.md\n"
    )
    target = _git_repo(tmp_path / "target")
    second = target / "SECOND.md"
    second.write_text("tracked team file\n")
    subprocess.run(["git", "-C", str(target), "add", "SECOND.md"], check=True)

    preflight = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert preflight.exit_code == 1
    assert not (target / "AGENTS.md").exists()
    assert second.read_text() == "tracked team file\n"

    subprocess.run(
        ["git", "-C", str(target), "rm", "--cached", "SECOND.md"], check=True
    )
    project_injection = __import__(
        "setforge.project_injection", fromlist=["_write_project_file"]
    )
    original_write = project_injection._write_project_file
    calls = 0

    def fail_second_write(
        guard: TargetLockGuard, path: Path, payload: bytes, mode: int
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("forced second write failure")
        original_write(guard, path, payload, mode)

    monkeypatch.setattr(
        "setforge.project_injection._write_project_file", fail_second_write
    )
    failed = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert failed.exit_code == 1
    assert isinstance(failed.exception, OSError)
    assert not (target / "AGENTS.md").exists()
    assert second.read_text() == "tracked team file\n"
    assert not manifest_path(target, "demo").exists()


def test_failure_after_visibility_write_compensates_every_effect(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    exclude = target / ".git" / "info" / "exclude"
    original_exclude = exclude.read_bytes()

    def fail_manifest(_plan: ProjectInjectionPlan, _owner_id: uuid.UUID) -> bytes:
        raise OSError("forced manifest failure")

    monkeypatch.setattr("setforge.project_injection._manifest_payload", fail_manifest)
    failed = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )

    assert failed.exit_code == 1
    assert isinstance(failed.exception, OSError)
    assert not (target / "AGENTS.md").exists()
    assert not manifest_path(target, "demo").exists()
    assert OwnershipStore().list_claims() == ()
    assert exclude.read_bytes() == original_exclude


def test_linked_worktrees_have_independent_injections(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    subprocess.run(
        [
            "git",
            "-C",
            str(target),
            "-c",
            "user.name=SetForge Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "base",
        ],
        check=True,
    )
    linked = tmp_path / "linked"
    subprocess.run(
        [
            "git",
            "-C",
            str(target),
            "worktree",
            "add",
            "-q",
            "-b",
            "linked",
            str(linked),
        ],
        check=True,
    )

    for worktree in (target, linked):
        result = CliRunner().invoke(
            app,
            [
                "project",
                "inject",
                "demo",
                str(worktree),
                "--config",
                str(config),
                "--yes",
            ],
        )
        assert result.exit_code == 0, result.exception
    assert manifest_path(target, "demo") != manifest_path(linked, "demo")
    assert manifest_path(target, "demo").exists()
    assert manifest_path(linked, "demo").exists()


def test_linked_hidden_claims_release_independently_and_conflict_with_tracked(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    subprocess.run(
        [
            "git",
            "-C",
            str(target),
            "-c",
            "user.name=SetForge Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "base",
        ],
        check=True,
    )
    linked = tmp_path / "linked"
    subprocess.run(
        [
            "git",
            "-C",
            str(target),
            "worktree",
            "add",
            "-q",
            "-b",
            "linked",
            str(linked),
        ],
        check=True,
    )
    base = ["project", "inject", "demo"]
    for worktree in (target, linked):
        result = CliRunner().invoke(
            app,
            [*base, str(worktree), "--config", str(config), "--yes"],
        )
        assert result.exit_code == 0, result.exception
    assert len(read_claims(target)[3]) == 2

    removed = CliRunner().invoke(
        app,
        ["project", "remove", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert removed.exit_code == 0, removed.exception
    assert len(read_claims(linked)[3]) == 1
    assert (
        subprocess.run(
            ["git", "-C", str(linked), "status", "--short"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        == ""
    )

    tracked_conflict = CliRunner().invoke(
        app,
        [
            "project",
            "inject",
            "demo",
            str(target),
            "--config",
            str(config),
            "--git-tracked",
            "--yes",
        ],
    )
    assert tracked_conflict.exit_code == 1
    assert tracked_conflict.exception is not None
    assert "linked worktrees" in str(tracked_conflict.exception)
    assert not (target / "AGENTS.md").exists()

    remove_linked = CliRunner().invoke(
        app,
        ["project", "remove", "demo", str(linked), "--config", str(config), "--yes"],
    )
    assert remove_linked.exit_code == 0, remove_linked.exception
    tracked = CliRunner().invoke(
        app,
        [
            "project",
            "inject",
            "demo",
            str(target),
            "--config",
            str(config),
            "--git-tracked",
            "--yes",
        ],
    )
    assert tracked.exit_code == 0, tracked.exception
    hidden_conflict = CliRunner().invoke(
        app,
        [
            "project",
            "inject",
            "demo",
            str(linked),
            "--config",
            str(config),
            "--yes",
        ],
    )
    assert hidden_conflict.exit_code == 1
    assert hidden_conflict.exception is not None
    assert "recorded tracked, requested hidden" in str(hidden_conflict.exception)
    assert not (linked / "AGENTS.md").exists()


def test_existing_g2_hidden_record_activates_only_on_live_reinject(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    command = [
        "project",
        "inject",
        "demo",
        str(target),
        "--config",
        str(config),
    ]
    first = CliRunner().invoke(app, [*command, "--yes"])
    assert first.exit_code == 0, first.exception
    claim = read_claims(target)[3][0]
    apply_claims(plan_claims(target, remove=(claim,)))
    assert (
        subprocess.run(
            ["git", "-C", str(target), "status", "--short"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        == "?? AGENTS.md\n"
    )

    dry = CliRunner().invoke(app, [*command, "--dry-run"])
    assert dry.exit_code == 0, dry.exception
    assert read_claims(target)[3] == ()
    live = CliRunner().invoke(app, [*command, "--yes"])
    assert live.exit_code == 0, live.exception
    assert "visibility activated" in live.output
    assert read_claims(target)[3] == (claim,)


def test_corrupt_sibling_manifest_blocks_visibility_without_mutation(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    subprocess.run(
        [
            "git",
            "-C",
            str(target),
            "-c",
            "user.name=SetForge Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "base",
        ],
        check=True,
    )
    linked = tmp_path / "linked"
    subprocess.run(
        [
            "git",
            "-C",
            str(target),
            "worktree",
            "add",
            "-q",
            "-b",
            "linked-corrupt",
            str(linked),
        ],
        check=True,
    )
    tracked = CliRunner().invoke(
        app,
        [
            "project",
            "inject",
            "demo",
            str(target),
            "--config",
            str(config),
            "--git-tracked",
            "--yes",
        ],
    )
    assert tracked.exit_code == 0, tracked.exception
    sibling_manifest = manifest_path(target, "demo")
    sibling_manifest.write_bytes(b"{corrupt")
    exclude = info_exclude_path(target)
    before_exclude = exclude.read_bytes()

    hidden = CliRunner().invoke(
        app,
        [
            "project",
            "inject",
            "demo",
            str(linked),
            "--config",
            str(config),
            "--yes",
        ],
    )

    assert hidden.exit_code == 1
    assert hidden.exception is not None
    assert "cannot validate sibling project visibility record" in str(hidden.exception)
    assert not (linked / "AGENTS.md").exists()
    assert exclude.read_bytes() == before_exclude
    assert sibling_manifest.read_bytes() == b"{corrupt"


def test_remove_then_reinject_reclaims_released_ownership(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    inject = [
        "project",
        "inject",
        "demo",
        str(target),
        "--config",
        str(config),
        "--yes",
    ]
    remove = [
        "project",
        "remove",
        "demo",
        str(target),
        "--config",
        str(config),
        "--yes",
    ]
    assert CliRunner().invoke(app, inject).exit_code == 0
    assert CliRunner().invoke(app, remove).exit_code == 0
    reinjected = CliRunner().invoke(app, inject)
    assert reinjected.exit_code == 0, reinjected.exception
    assert (target / "AGENTS.md").read_text() == "managed instructions\n"


def test_manifest_parent_escape_is_rejected_without_external_cleanup(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.exception
    outside = tmp_path / "outside"
    outside.mkdir()
    state = manifest_path(target, "demo")
    payload = json.loads(state.read_text())
    payload["files"][0]["created_parents"] = ["../outside"]
    state.write_text(json.dumps(payload))

    removed = CliRunner().invoke(
        app,
        ["project", "remove", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert removed.exit_code == 1
    assert outside.exists()
    assert (target / "AGENTS.md").exists()


def test_duplicate_manifest_destination_is_rejected_without_mutation(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    assert (
        CliRunner()
        .invoke(
            app,
            [
                "project",
                "inject",
                "demo",
                str(target),
                "--config",
                str(config),
                "--yes",
            ],
        )
        .exit_code
        == 0
    )
    state = manifest_path(target, "demo")
    payload = json.loads(state.read_text())
    payload["files"].append(dict(payload["files"][0]))
    state.write_text(json.dumps(payload))
    before_manifest = state.read_bytes()
    before_file = (target / "AGENTS.md").read_bytes()
    claim_path, before_claim = _rewrite_only_claim(lambda claim: claim)

    removed = CliRunner().invoke(
        app,
        ["project", "remove", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert removed.exit_code == 1
    assert removed.exception is not None
    assert "invalid file record" in str(removed.exception)
    assert state.read_bytes() == before_manifest
    assert (target / "AGENTS.md").read_bytes() == before_file
    assert claim_path.read_bytes() == before_claim


@pytest.mark.parametrize("tamper", ["action", "source-digest"])
def test_inconsistent_manifest_record_is_rejected_without_mutation(
    tmp_path: Path, monkeypatch, tamper: str
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    assert (
        CliRunner()
        .invoke(
            app,
            [
                "project",
                "inject",
                "demo",
                str(target),
                "--config",
                str(config),
                "--yes",
            ],
        )
        .exit_code
        == 0
    )
    state = manifest_path(target, "demo")
    payload = json.loads(state.read_text())
    if tamper == "action":
        payload["files"][0]["action"] = "retain-identical"
    else:
        payload["files"][0]["source_digest"] = "0" * 64
    state.write_text(json.dumps(payload))
    before_manifest = state.read_bytes()
    before_file = (target / "AGENTS.md").read_bytes()
    claim_path, before_claim = _rewrite_only_claim(lambda claim: claim)

    removed = CliRunner().invoke(
        app,
        ["project", "remove", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert removed.exit_code == 1
    assert removed.exception is not None
    assert "inconsistent file record" in str(removed.exception)
    assert state.read_bytes() == before_manifest
    assert (target / "AGENTS.md").read_bytes() == before_file
    assert claim_path.read_bytes() == before_claim


def test_remove_rejects_mismatched_claim_binding_without_mutation(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    assert (
        CliRunner()
        .invoke(
            app,
            [
                "project",
                "inject",
                "demo",
                str(target),
                "--config",
                str(config),
                "--yes",
            ],
        )
        .exit_code
        == 0
    )
    state = manifest_path(target, "demo")
    before_manifest = state.read_bytes()
    claim_path, _ = _rewrite_only_claim(
        lambda claim: replace(claim, declaration_refs=("project-profile:other:file",))
    )
    mismatched_claim = claim_path.read_bytes()

    removed = CliRunner().invoke(
        app,
        ["project", "remove", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert removed.exit_code == 1
    assert state.read_bytes() == before_manifest
    assert (target / "AGENTS.md").read_text() == "managed instructions\n"
    assert claim_path.read_bytes() == mismatched_claim


def test_remove_rejects_released_claim_without_mutation(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    assert (
        CliRunner()
        .invoke(
            app,
            [
                "project",
                "inject",
                "demo",
                str(target),
                "--config",
                str(config),
                "--yes",
            ],
        )
        .exit_code
        == 0
    )
    state = manifest_path(target, "demo")
    before_manifest = state.read_bytes()

    def release(claim):
        generation = claim.generation + 1
        return replace(
            claim,
            authority=Authority.NONE,
            lifecycle=ClaimLifecycle.RELEASED,
            generation=generation,
            history=(
                *claim.history,
                ClaimEvent("release", claim.owner_id, generation),
            ),
        )

    claim_path, _ = _rewrite_only_claim(release)
    released_claim = claim_path.read_bytes()
    removed = CliRunner().invoke(
        app,
        ["project", "remove", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert removed.exit_code == 1
    assert state.read_bytes() == before_manifest
    assert (target / "AGENTS.md").read_text() == "managed instructions\n"
    assert claim_path.read_bytes() == released_claim


def test_reinject_rejects_mismatched_released_tombstone(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    command = [
        "project",
        "inject",
        "demo",
        str(target),
        "--config",
        str(config),
        "--yes",
    ]
    assert CliRunner().invoke(app, command).exit_code == 0
    assert (
        CliRunner()
        .invoke(
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
        .exit_code
        == 0
    )
    claim_path, _ = _rewrite_only_claim(
        lambda claim: replace(claim, fingerprint="mismatched")
    )
    mismatched_claim = claim_path.read_bytes()

    reinjected = CliRunner().invoke(app, command)
    assert reinjected.exit_code == 1
    assert not (target / "AGENTS.md").exists()
    assert not manifest_path(target, "demo").exists()
    assert claim_path.read_bytes() == mismatched_claim
