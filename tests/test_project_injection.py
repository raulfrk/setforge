from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import app
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
from setforge.project_injection import manifest_path


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
    assert "visibility intent: hidden" in injected.output

    removed = CliRunner().invoke(
        app,
        ["project", "remove", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert removed.exit_code == 0, removed.output
    assert not (target / "AGENTS.md").exists()


def test_project_inject_refuses_tracked_collision_without_mutation(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    destination = target / "AGENTS.md"
    destination.write_text("team instructions\n")
    subprocess.run(["git", "-C", str(target), "add", "AGENTS.md"], check=True)

    result = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert result.exit_code == 1
    assert result.exception is not None
    assert "tracked" in str(result.exception)
    assert destination.read_text() == "team instructions\n"


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
    assert "visibility intent: tracked" in tracked.output
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


def test_corrupt_manifest_and_non_git_target_fail_closed(
    tmp_path: Path, monkeypatch
) -> None:
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

    plain = tmp_path / "plain"
    plain.mkdir()
    non_git = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(plain), "--config", str(config), "--yes"],
    )
    assert non_git.exit_code == 1
    assert non_git.exception is not None
    assert "Git worktree" in str(non_git.exception)


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
