from __future__ import annotations

import json
import os
import shutil
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
from setforge.errors import SetforgeError
from setforge.file_ownership import (
    decide_file,
    observe_file,
    publish_file_claim_locked,
)
from setforge.git_visibility import (
    info_exclude_path,
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
from setforge.project_injection import (
    ProjectInjectionPlan,
    _is_tracked,
    _verified_project_target,
    manifest_path,
)
from tests.project_helpers import _git_repo


def _raise_missing_git(*_args: object, **_kwargs: object) -> None:
    raise FileNotFoundError(2, "No such file or directory", "git")


def _raise_git_timeout(*_args: object, **kwargs: object) -> None:
    raise subprocess.TimeoutExpired(cmd="git", timeout=float(kwargs.get("timeout", 30)))  # type: ignore[arg-type]


def test_verified_project_target_wraps_git_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing git binary and a hung git both surface as SetforgeError."""
    target = tmp_path / "repo"
    target.mkdir()
    monkeypatch.setattr("setforge.project_injection.subprocess.run", _raise_missing_git)
    with pytest.raises(SetforgeError, match="project target"):
        _verified_project_target(target)

    monkeypatch.setattr("setforge.project_injection.subprocess.run", _raise_git_timeout)
    with pytest.raises(SetforgeError, match="project target"):
        _verified_project_target(target)


def test_is_tracked_wraps_git_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "repo"
    target.mkdir()
    monkeypatch.setattr("setforge.project_injection.subprocess.run", _raise_missing_git)

    with pytest.raises(SetforgeError, match="cannot classify Git destination"):
        _is_tracked(target, Path("a/b.txt"))


@pytest.fixture(autouse=True)
def _candidate_filter_entrypoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Make Git filter children execute this exact source candidate."""
    binary_dir = tmp_path / "candidate-bin"
    binary_dir.mkdir()
    entrypoint = binary_dir / "setforge"
    entrypoint.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "if os.environ.get('MUTANT_UNDER_TEST') == 'stats':\n"
        "    os.environ['MUTANT_UNDER_TEST'] = ''\n"
        "_original_cwd = os.getcwd()\n"
        "try:\n"
        f"    os.chdir({str(Path(__file__).parents[1])!r})\n"
        "    from setforge.cli import main\n"
        "finally:\n"
        "    os.chdir(_original_cwd)\n"
        "main()\n"
    )
    entrypoint.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parents[1]))


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


def _already_injected(target: Path, config: Path) -> str:
    return (
        f"project profile 'demo' is already injected at {target}; use "
        f"`setforge project sync {target}`. Sync keeps the config and each file's "
        "Git visibility recorded at injection: change a file's visibility with "
        f"`setforge project visibility {target} <file> --hidden` or `--tracked`, "
        "and to inject from another config file of the same checkout first run "
        f"`setforge project remove demo {target} --config {config.resolve()}`"
    )


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


def test_second_injection_is_refused_and_leaves_record_and_visibility_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    exposed = CliRunner().invoke(
        app,
        ["project", "visibility", str(target), "AGENTS.md", "--tracked", "--yes"],
    )
    assert exposed.exit_code == 0, exposed.exception
    assert read_claims(target)[3] == ()

    before = manifest_path(target, "demo").read_bytes()

    reinjected = CliRunner().invoke(app, command)

    assert reinjected.exit_code == 1
    assert str(reinjected.exception) == _already_injected(target, config)
    assert read_claims(target)[3] == ()
    assert manifest_path(target, "demo").read_bytes() == before


def test_apply_freshness_reuses_selected_config_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    default_config = _config(tmp_path)
    config = default_config.with_name("custom.yaml")
    default_config.rename(config)
    target = _git_repo(tmp_path / "target")

    result = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )

    assert result.exit_code == 0, result.exception
    record = json.loads(manifest_path(target, "demo").read_text())
    assert record["config_path"] == str(config.resolve())


def test_remove_rejects_different_config_manifest_in_same_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    alternate = config.with_name("alternate.yaml")
    alternate.write_bytes(config.read_bytes())
    target = _git_repo(tmp_path / "target")
    injected = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.exception
    state = manifest_path(target, "demo")
    before = (target / "AGENTS.md").read_bytes(), state.read_bytes()
    before_claims = OwnershipStore().list_claims()

    removed = CliRunner().invoke(
        app,
        [
            "project",
            "remove",
            "demo",
            str(target),
            "--config",
            str(alternate),
            "--yes",
        ],
    )

    assert removed.exit_code != 0
    assert "different config manifest" in str(removed.exception)
    assert ((target / "AGENTS.md").read_bytes(), state.read_bytes()) == before
    assert OwnershipStore().list_claims() == before_claims


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


def test_project_inject_refuses_tracked_file_ownership_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge.locking import mutation_locks

    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = tmp_path / "plain-target"
    target.mkdir()
    destination = target / "AGENTS.md"
    destination.write_text("ordinary tracked owner\n")
    store = OwnershipStore()
    observation = observe_file(destination)
    owner_id = uuid.uuid4()
    with mutation_locks(resources=True):
        publish_file_claim_locked(
            store,
            decide_file(observation, None, owner_id=owner_id),
            owner_id=owner_id,
            declaration_ref="tracked_files.instructions",
            acquisition="adopted-external",
        )
    before_claims = store.list_claims()

    result = CliRunner().invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )

    assert result.exit_code != 0
    assert "tracked-file ownership claim" in str(result.exception)
    assert destination.read_text() == "ordinary tracked owner\n"
    assert store.list_claims() == before_claims
    assert not manifest_path(target, "demo").exists()


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
    assert repeated.exit_code == 1
    assert "is already injected at" in str(repeated.exception)
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


def test_second_injection_names_project_sync_whatever_changed(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    inject = ["project", "inject", "demo", str(target), "--config", str(config)]
    assert CliRunner().invoke(app, [*inject, "--yes"]).exit_code == 0
    record = manifest_path(target, "demo").read_bytes()
    message = _already_injected(target, config)

    for extra in (["--yes"], ["--dry-run"], ["--git-tracked", "--yes"]):
        second = CliRunner().invoke(app, [*inject, *extra])
        assert second.exit_code == 1
        assert str(second.exception) == message
    (config.parent / "project" / "demo" / "AGENTS.md").write_text("updated\n")
    (target / "AGENTS.md").write_text("local\n")
    changed = CliRunner().invoke(app, [*inject, "--yes"])
    assert changed.exit_code == 1
    assert str(changed.exception) == message
    assert (target / "AGENTS.md").read_text() == "local\n"
    assert manifest_path(target, "demo").read_bytes() == record


def test_second_injection_with_another_config_or_visibility_names_what_applies(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    other = config.with_name("other.yaml")
    other.write_bytes(config.read_bytes())
    target = _git_repo(tmp_path / "target")
    run = CliRunner().invoke
    assert (
        run(
            app,
            ["project", "inject", "demo", str(target), "--config", str(config), "-y"],
        ).exit_code
        == 0
    )
    again = ["project", "inject", "demo", str(target), "--config", str(other), "-y"]

    refused = run(app, [*again, "--git-tracked"])

    assert refused.exit_code == 1
    assert str(refused.exception) == _already_injected(target, config)
    exposed = run(
        app, ["project", "visibility", str(target), "AGENTS.md", "--tracked", "-y"]
    )
    assert exposed.exit_code == 0, exposed.exception
    assert read_claims(target)[3] == ()
    removed = run(
        app,
        ["project", "remove", "demo", str(target), "--config", str(config), "-y"],
    )
    assert removed.exit_code == 0, removed.exception
    injected = run(app, again)
    assert injected.exit_code == 0, injected.exception
    record = json.loads(manifest_path(target, "demo").read_text())
    assert record["config_path"] == str(other.resolve())


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
    assert "is already injected at" in str(reinjected.exception)

    removed = CliRunner().invoke(
        app,
        ["project", "remove", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert removed.exit_code == 1
    assert removed.exception is not None
    assert "drifted" in str(removed.exception)
    assert state.exists()
    assert (target / "AGENTS.md").read_text() == "local drift\n"


def test_remove_refuses_local_edit_kept_by_sync_until_profile_content_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    source = config.parent / "project" / "demo" / "AGENTS.md"
    source.write_text("alpha\nbeta\ngamma\n")
    target = _git_repo(tmp_path / "target")
    runner = CliRunner()
    remove = ["project", "remove", "demo", str(target), "--config", str(config)]
    injected = runner.invoke(
        app,
        ["project", "inject", "demo", str(target), "--config", str(config), "--yes"],
    )
    assert injected.exit_code == 0, injected.output
    live = target / "AGENTS.md"
    live.write_text("alpha-local\nbeta\ngamma\n")
    source.write_text("alpha\nbeta\ngamma-profile\n")
    synced = runner.invoke(app, ["project", "sync", str(target), "--yes"])
    assert synced.exit_code == 0, synced.output
    merged = b"alpha-local\nbeta\ngamma-profile\n"
    assert live.read_bytes() == merged
    state = manifest_path(target, "demo")
    record = state.read_bytes()

    for arguments in ([*remove, "--dry-run"], [*remove, "--yes"]):
        refused = runner.invoke(app, arguments)
        assert refused.exit_code == 1
        assert str(refused.exception) == (
            f"injected project file has drifted: {live}; its content or mode "
            "(expected 0644) differs from what was injected and removal would "
            "discard the difference. Save your changes elsewhere, delete the "
            "file, then remove again"
        )
        assert live.read_bytes() == merged
        assert state.read_bytes() == record

    visibility = runner.invoke(
        app, ["project", "visibility", str(target), "AGENTS.md", "--tracked", "--yes"]
    )
    assert visibility.exit_code == 0, visibility.output
    assert live.read_bytes() == merged

    live.write_text("alpha\nbeta\ngamma-profile\n")
    removed = runner.invoke(app, [*remove, "--yes"])
    assert removed.exit_code == 0, removed.output
    assert not live.exists()
    assert not state.exists()


def test_inject_and_remove_refuse_read_only_directory_before_journaling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root writes into read-only directories")
    from setforge import operations

    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    config = _config(tmp_path)
    config.write_text(config.read_text().replace("dst: AGENTS.md", "dst: docs/A.md"))
    target = _git_repo(tmp_path / "target")
    runner = CliRunner()
    inject = ["project", "inject", "demo", str(target), "--config", str(config)]
    remove = ["project", "remove", "demo", str(target), "--config", str(config)]

    target.chmod(0o555)
    try:
        refused = runner.invoke(app, [*inject, "--yes"])
    finally:
        target.chmod(0o755)
    assert refused.exit_code == 1
    assert isinstance(refused.exception, SetforgeError)
    assert str(refused.exception) == f"project directory is not writable: {target}"
    assert not (target / "docs").exists()
    assert not manifest_path(target, "demo").exists()
    assert not list(operations.journals_root().glob("*.json"))

    injected = runner.invoke(app, [*inject, "--yes"])
    assert injected.exit_code == 0, injected.output
    record = manifest_path(target, "demo").read_bytes()
    for directory in (target / "docs", target):
        directory.chmod(0o555)
        try:
            refused = runner.invoke(app, [*remove, "--yes"])
        finally:
            directory.chmod(0o755)
        assert refused.exit_code == 1
        assert str(refused.exception) == (
            f"project directory is not writable: {directory}"
        )
        assert (target / "docs/A.md").read_text() == "managed instructions\n"
        assert manifest_path(target, "demo").read_bytes() == record
        assert not list(operations.journals_root().glob("*.json"))

    removed = runner.invoke(app, [*remove, "--yes"])
    assert removed.exit_code == 0, removed.output
    assert not (target / "docs").exists()


def _remount_with_new_device_number(monkeypatch: pytest.MonkeyPatch) -> None:
    """Report another st_dev for every path, as an NFS remount or second host does."""

    def shifted(call: Callable[..., os.stat_result]) -> Callable[..., os.stat_result]:
        def wrapper(*args: object, **kwargs: object) -> os.stat_result:
            info = call(*args, **kwargs)
            fields = list(info)[:10]
            fields[2] += 7
            return os.stat_result(
                (
                    *fields,
                    info.st_atime,
                    info.st_mtime,
                    info.st_ctime,
                    info.st_atime_ns,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                )
            )

        return wrapper

    for name in ("stat", "lstat", "fstat"):
        monkeypatch.setattr(os, name, shifted(getattr(os, name)))


@pytest.mark.parametrize("git_target", [False, True])
def test_recorded_injection_survives_a_changed_device_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_target: bool
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    source = config.parent / "project" / "demo" / "AGENTS.md"
    target = tmp_path / "target"
    if git_target:
        _git_repo(target)
    else:
        target.mkdir()
    runner = CliRunner()
    inject = ["project", "inject", "demo", str(target), "--config", str(config)]
    injected = runner.invoke(app, [*inject, "--yes"])
    assert injected.exit_code == 0, injected.output
    state = manifest_path(target, "demo")
    recorded_device = json.loads(state.read_bytes())["target_device"]
    assert recorded_device == target.stat().st_dev
    claim_ids = {claim.resource_id for claim in OwnershipStore().list_claims()}
    visibility = "hidden" if git_target else "not-applicable"

    with monkeypatch.context() as remounted:
        _remount_with_new_device_number(remounted)
        assert target.stat().st_dev == recorded_device + 7

        listed = runner.invoke(app, ["project", "list"])
        assert listed.exit_code == 0, listed.output
        assert listed.output == f"{target}  [demo]\n  {visibility}: AGENTS.md\n"
        repeated = runner.invoke(app, [*inject, "--yes"])
        assert repeated.exit_code == 1
        assert "is already injected at" in str(repeated.exception)
        if git_target:
            tracked = runner.invoke(
                app,
                ["project", "visibility", str(target), "AGENTS.md", "--tracked", "-y"],
            )
            assert tracked.exit_code == 0, tracked.output
        source.write_text("managed instructions v2\n")
        synced = runner.invoke(app, ["project", "sync", str(target), "--yes"])
        assert synced.exit_code == 0, synced.output
        assert (target / "AGENTS.md").read_text() == "managed instructions v2\n"
        assert json.loads(state.read_bytes())["target_device"] == recorded_device
        assert {
            claim.resource_id for claim in OwnershipStore().list_claims()
        } == claim_ids
        removed = runner.invoke(
            app,
            ["project", "remove", "demo", str(target), "--config", str(config), "-y"],
        )
        assert removed.exit_code == 0, removed.output

    assert not (target / "AGENTS.md").exists()
    assert not state.exists()
    assert [claim.lifecycle for claim in OwnershipStore().list_claims()] == [
        ClaimLifecycle.RELEASED
    ]


def test_tracked_overlay_injection_survives_a_changed_device_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    assert injected.exit_code == 0, injected.output

    with monkeypatch.context() as remounted:
        _remount_with_new_device_number(remounted)
        listed = runner.invoke(app, ["project", "list"])
        assert listed.exit_code == 0, listed.output
        assert "tracked-overlay: AGENTS.md" in listed.output
        removed = runner.invoke(
            app,
            ["project", "remove", "demo", str(target), "--config", str(config), "-y"],
        )
        assert removed.exit_code == 0, removed.output

    assert destination.read_text() == "team instructions\n"


@pytest.mark.parametrize("command", ["list", "sync", "inject", "visibility"])
def test_recorded_injection_still_refuses_a_different_directory_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    runner = CliRunner()
    inject = ["project", "inject", "demo", str(target), "--config", str(config), "-y"]
    assert runner.invoke(app, inject).exit_code == 0
    state = manifest_path(target, "demo")
    record = json.loads(state.read_bytes())
    record["target_inode"] += 1
    state.write_text(json.dumps(record))
    arguments = {
        "list": ["project", "list"],
        "sync": ["project", "sync", str(target), "--dry-run"],
        "inject": inject,
        "visibility": [
            "project",
            "visibility",
            str(target),
            "AGENTS.md",
            "--tracked",
            "--yes",
        ],
    }[command]

    refused = runner.invoke(app, arguments)

    assert refused.exit_code == 1
    if command == "sync":
        assert str(refused.exception) == (
            f"project injection state does not match target identity: {state}; "
            f"run `setforge project remove demo {target}` to drop the stale record"
        )
    assert (target / "AGENTS.md").read_text() == "managed instructions\n"
    assert json.loads(state.read_bytes()) == record


@pytest.mark.parametrize("field", ["target_device", "target_inode"])
@pytest.mark.parametrize("value", [True, "25", 1.5, None])
def test_manifest_with_non_integer_target_identity_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    runner = CliRunner()
    remove = ["project", "remove", "demo", str(target), "--config", str(config), "-y"]
    inject = ["project", "inject", "demo", str(target), "--config", str(config), "-y"]
    assert runner.invoke(app, inject).exit_code == 0
    state = manifest_path(target, "demo")
    record = json.loads(state.read_bytes())
    record[field] = value
    state.write_text(json.dumps(record))

    refused = runner.invoke(app, remove)

    assert refused.exit_code == 1
    assert str(refused.exception) == (
        f"project injection state has invalid fields: {state}"
    )
    assert (target / "AGENTS.md").exists()


def _claim_lifecycles() -> list[ClaimLifecycle]:
    return [claim.lifecycle for claim in OwnershipStore().list_claims()]


def _replace_directory_keeping_contents(target: Path) -> None:
    """Give TARGET a new inode, as a restore from backup or a re-clone does."""
    replaced_inode = target.stat().st_ino
    moved = target.with_name(target.name + ".old")
    target.rename(moved)
    shutil.copytree(moved, target, symlinks=True)
    shutil.rmtree(moved)
    assert target.stat().st_ino != replaced_inode


def test_moved_project_record_is_reported_and_dropped_without_blocking_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    moved = tmp_path / "moved"
    runner = CliRunner()
    injected = runner.invoke(
        app, ["project", "inject", "demo", str(target), "--config", str(config), "-y"]
    )
    assert injected.exit_code == 0, injected.output
    stale_record = manifest_path(target, "demo")
    target.rename(moved)

    listed = runner.invoke(app, ["project", "list"])
    assert listed.exit_code == 1
    assert listed.output == (
        f"{target}  [demo]\n"
        "  error: project directory no longer exists; run "
        f"`setforge project remove demo {target}` to drop the stale record "
        f"({stale_record.name})\n"
    )
    other = _git_repo(tmp_path / "other")
    unrelated = runner.invoke(
        app, ["project", "inject", "demo", str(other), "--config", str(config), "-y"]
    )
    assert unrelated.exit_code == 0, unrelated.output

    remove = ["project", "remove", "demo", str(target), "--config", str(config)]
    preview = runner.invoke(app, [*remove, "--dry-run"])
    assert preview.exit_code == 0, preview.output
    assert preview.output == (
        "project profile: demo\n"
        f"target: {target}\n"
        "stale injection: project directory no longer exists\n"
        "  release ownership: AGENTS.md\n"
        "warning: the record's saved pre-injection contents are discarded and "
        "the injection cannot be removed normally afterwards; project files are "
        "left unchanged\n"
        "dry run: no changes applied\n"
    )
    assert stale_record.exists()
    assert _claim_lifecycles() == [ClaimLifecycle.CLAIMED, ClaimLifecycle.CLAIMED]

    dropped = runner.invoke(app, [*remove, "--yes"])
    assert dropped.exit_code == 0, dropped.output
    assert dropped.output.endswith("stale injection dropped\n")
    assert not stale_record.exists()
    assert sorted(_claim_lifecycles()) == [
        ClaimLifecycle.CLAIMED,
        ClaimLifecycle.RELEASED,
    ]
    assert (moved / "AGENTS.md").read_text() == "managed instructions\n"
    listed = runner.invoke(app, ["project", "list"])
    assert listed.exit_code == 0, listed.output
    assert listed.output == f"{other}  [demo]\n  hidden: AGENTS.md\n"


def test_replaced_project_directory_drops_record_claims_and_exclude_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    exclude = target / ".git" / "info" / "exclude"
    pristine_exclude = exclude.read_bytes()
    runner = CliRunner()
    inject = ["project", "inject", "demo", str(target), "--config", str(config), "-y"]
    remove = ["project", "remove", "demo", str(target), "--config", str(config)]
    assert runner.invoke(app, inject).exit_code == 0
    state = manifest_path(target, "demo")
    _replace_directory_keeping_contents(target)

    refused = runner.invoke(app, inject)
    assert refused.exit_code == 1
    assert str(refused.exception) == (
        f"a stale injection record exists for {target}; run "
        f"`setforge project remove demo {target}` to drop it, then inject again"
    )
    listed = runner.invoke(app, ["project", "list"])
    assert listed.exit_code == 1
    assert (
        "  error: project target identity does not match the record; run "
        f"`setforge project remove demo {target}` to drop the stale record"
    ) in listed.output

    dropped = runner.invoke(app, [*remove, "--yes"])
    assert dropped.exit_code == 0, dropped.output
    assert dropped.output == (
        "project profile: demo\n"
        f"target: {target}\n"
        "stale injection: project directory was replaced since injection\n"
        "  release ownership: AGENTS.md\n"
        f"  release private exclude claims: {exclude}\n"
        "warning: the record's saved pre-injection contents are discarded and "
        "the injection cannot be removed normally afterwards; project files are "
        "left unchanged\n"
        "stale injection dropped\n"
    )
    assert not state.exists()
    assert _claim_lifecycles() == [ClaimLifecycle.RELEASED]
    assert exclude.read_bytes() == pristine_exclude
    assert (target / "AGENTS.md").read_text() == "managed instructions\n"

    reinjected = runner.invoke(app, inject)
    assert reinjected.exit_code == 0, reinjected.output
    assert "retain-identical: AGENTS.md" in reinjected.output


def test_remove_drops_claims_and_exclude_entries_left_by_a_lost_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    exclude = target / ".git" / "info" / "exclude"
    pristine_exclude = exclude.read_bytes()
    runner = CliRunner()
    inject = ["project", "inject", "demo", str(target), "--config", str(config), "-y"]
    remove = ["project", "remove", "demo", str(target), "--config", str(config)]
    assert runner.invoke(app, inject).exit_code == 0
    manifest_path(target, "demo").unlink()
    assert runner.invoke(app, inject).exit_code == 1

    dropped = runner.invoke(app, [*remove, "--yes"])
    assert dropped.exit_code == 0, dropped.output
    assert "stale injection: the injection record is missing\n" in dropped.output
    assert _claim_lifecycles() == [ClaimLifecycle.RELEASED]
    assert exclude.read_bytes() == pristine_exclude
    assert (target / "AGENTS.md").read_text() == "managed instructions\n"

    again = runner.invoke(app, [*remove, "--yes"])
    assert again.exit_code == 1
    assert "project injection is not recorded" in str(again.exception)
    assert runner.invoke(app, inject).exit_code == 0


def test_stale_removal_failure_restores_record_claims_and_exclude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state_root))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    exclude = target / ".git" / "info" / "exclude"
    runner = CliRunner()
    inject = ["project", "inject", "demo", str(target), "--config", str(config), "-y"]
    assert runner.invoke(app, inject).exit_code == 0
    _replace_directory_keeping_contents(target)
    before = (
        {path: path.read_bytes() for path in state_root.rglob("*.json")},
        exclude.read_bytes(),
    )
    from setforge import operations

    def fail(_journal: object) -> None:
        raise RuntimeError("forced failure after stale effects")

    monkeypatch.setattr(operations, "finish_checkpoint", fail)
    failed = runner.invoke(
        app,
        ["project", "remove", "demo", str(target), "--config", str(config), "-y"],
    )

    assert isinstance(failed.exception, RuntimeError)
    assert (
        {path: path.read_bytes() for path in state_root.rglob("*.json")},
        exclude.read_bytes(),
    ) == before
    assert not list(operations.journals_root().glob("*.json"))


def test_ownership_release_refuses_project_claim_and_names_project_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    runner = CliRunner()
    injected = runner.invoke(
        app, ["project", "inject", "demo", str(target), "--config", str(config), "-y"]
    )
    assert injected.exit_code == 0, injected.output
    store = OwnershipStore()
    (claim,) = store.list_claims()

    released = runner.invoke(
        app,
        [
            "ownership",
            "release",
            store.claim_id(claim.resource_id),
            "--config",
            str(config),
            "--yes",
        ],
    )

    assert released.exit_code == 1
    assert str(released.exception) == (
        "ownership claim belongs to a project injection; run "
        f"`setforge project remove demo {target}` to remove it"
    )
    assert store.list_claims() == (claim,)
    removed = runner.invoke(
        app, ["project", "remove", "demo", str(target), "--config", str(config), "-y"]
    )
    assert removed.exit_code == 0, removed.output
    assert not (target / "AGENTS.md").exists()


def _two_profiles_one_destination(tmp_path: Path) -> Path:
    config = _config(tmp_path)
    config.write_text(
        config.read_text() + "  other:\n    files:\n      rules:\n"
        "        src: AGENTS.md\n        dst: AGENTS.md\n"
    )
    source = config.parent / "project" / "other" / "AGENTS.md"
    source.parent.mkdir()
    source.write_text("other instructions\n")
    return config


def _private_state(root: Path) -> dict[Path, bytes]:
    return {
        path: path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and "locks" not in path.parts
    }


@pytest.mark.parametrize("remounted", [False, True])
def test_overlapping_profile_is_refused_in_dry_run_and_apply_with_owner_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remounted: bool
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    config = _two_profiles_one_destination(tmp_path)
    target = _git_repo(tmp_path / "target")
    runner = CliRunner()
    first = runner.invoke(
        app, ["project", "inject", "demo", str(target), "--config", str(config), "-y"]
    )
    assert first.exit_code == 0, first.output
    before = _private_state(state)
    if remounted:
        _remount_with_new_device_number(monkeypatch)

    for mode in ("--dry-run", "--yes"):
        refused = runner.invoke(
            app,
            ["project", "inject", "other", str(target), "--config", str(config), mode],
        )
        assert refused.exit_code == 1, refused.output
        assert str(refused.exception) == (
            "a project destination already has an active ownership claim: "
            f"AGENTS.md is injected by project profile 'demo' at {target}; "
            f"run `setforge project remove demo {target}` first"
        )
        assert (target / "AGENTS.md").read_text() == "managed instructions\n"
        assert _private_state(state) == before


def test_tracked_conflict_without_resolution_fails_dry_run_like_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    destination = target / "AGENTS.md"
    destination.write_text("team instructions\n")
    subprocess.run(["git", "-C", str(target), "add", "AGENTS.md"], check=True)
    runner = CliRunner()
    inject = ["project", "inject", "demo", str(target), "--config", str(config)]

    outcomes = [runner.invoke(app, [*inject, mode]) for mode in ("--dry-run", "--yes")]

    for outcome in outcomes:
        assert outcome.exit_code == 1
        assert str(outcome.exception) == (
            "project injection has 1 unresolved tracked-file conflict(s) in "
            "AGENTS.md; use a TTY or --auto"
        )
    assert destination.read_text() == "team instructions\n"
    assert _private_state(state) == {}
    resolved = runner.invoke(app, [*inject, "--auto=use-profile", "--dry-run"])
    assert resolved.exit_code == 0, resolved.output
    assert "overlay-tracked: AGENTS.md" in resolved.output
    assert destination.read_text() == "team instructions\n"
    assert _private_state(state) == {}


def test_tracked_overlay_removal_restores_git_private_files_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    destination = target / "AGENTS.md"
    destination.write_text("team instructions\n")
    subprocess.run(["git", "-C", str(target), "add", "AGENTS.md"], check=True)
    info = target / ".git" / "info"
    before = {path.name: path.read_bytes() for path in info.iterdir()}
    assert "attributes" not in before
    runner = CliRunner()
    arguments = [str(target), "--config", str(config), "--yes"]
    injected = runner.invoke(
        app, ["project", "inject", "demo", *arguments, "--auto=use-profile"]
    )
    assert injected.exit_code == 0, injected.output
    assert (info / "attributes").exists()

    removed = runner.invoke(app, ["project", "remove", "demo", *arguments])

    assert removed.exit_code == 0, removed.output
    assert {path.name: path.read_bytes() for path in info.iterdir()} == before
    assert destination.read_text() == "team instructions\n"


def test_hidden_injection_works_in_repository_without_info_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    shutil.rmtree(target / ".git" / "info")
    runner = CliRunner()
    arguments = [str(target), "--config", str(config)]

    preview = runner.invoke(app, ["project", "inject", "demo", *arguments, "--dry-run"])
    assert preview.exit_code == 0, preview.output
    assert not (target / ".git" / "info").exists()
    injected = runner.invoke(app, ["project", "inject", "demo", *arguments, "--yes"])
    assert injected.exit_code == 0, injected.output
    status = subprocess.run(
        ["git", "-C", str(target), "status", "--short"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    assert status == ""
    listed = runner.invoke(app, ["project", "list"])
    assert listed.output == f"{target}  [demo]\n  hidden: AGENTS.md\n"
    removed = runner.invoke(app, ["project", "remove", "demo", *arguments, "--yes"])
    assert removed.exit_code == 0, removed.output
    assert not (target / "AGENTS.md").exists()
    assert (target / ".git" / "info" / "exclude").read_bytes() == b""


def test_visibility_conflict_between_profiles_does_not_blame_linked_worktrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _two_profiles_one_destination(tmp_path)
    config.write_text(config.read_text().replace("dst: AGENTS.md\n", "dst: A.md\n", 1))
    config.write_text(
        config.read_text()
        + "      shared:\n        src: AGENTS.md\n        dst: A.md\n"
    )
    target = _git_repo(tmp_path / "target")
    runner = CliRunner()
    hidden = runner.invoke(
        app, ["project", "inject", "demo", str(target), "--config", str(config), "-y"]
    )
    assert hidden.exit_code == 0, hidden.output

    tracked = runner.invoke(
        app,
        [
            "project",
            "inject",
            "other",
            str(target),
            "--config",
            str(config),
            "--git-tracked",
            "--dry-run",
        ],
    )

    assert tracked.exit_code == 1
    assert str(tracked.exception) == (
        "project visibility conflicts with another injection in this repository "
        "for A.md: it is already hidden"
    )


@pytest.mark.parametrize(
    "module",
    [
        "project_injection",
        "project_sync",
        "project_visibility",
        "project_overlay",
        "git_overlay",
        "git_visibility",
        "git_info",
    ],
)
def test_project_messages_carry_no_internal_milestone_names(module: str) -> None:
    import re

    source = (Path(__file__).parents[1] / "setforge" / f"{module}.py").read_text()

    assert re.findall(r"\bG[0-9]\b", source) == []


@pytest.mark.parametrize("change", ["git-init", "git-removed"])
def test_remove_restores_saved_contents_after_the_git_directory_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = tmp_path / "target"
    if change == "git-removed":
        _git_repo(target)
    else:
        target.mkdir()
    destination = target / "AGENTS.md"
    destination.write_bytes(b"USER ORIGINAL\n")
    destination.chmod(0o640)
    runner = CliRunner()
    arguments = [str(target), "--config", str(config)]
    injected = runner.invoke(app, ["project", "inject", "demo", *arguments, "--yes"])
    assert injected.exit_code == 0, injected.output
    assert "replace-untracked: AGENTS.md" in injected.output
    state = manifest_path(target, "demo")
    record = state.read_bytes()
    if change == "git-removed":
        shutil.rmtree(target / ".git")
        recorded, live = str(target / ".git"), "none"
    else:
        subprocess.run(["git", "init", "-q", str(target)], check=True)
        recorded, live = "none", str(target / ".git")
    remedy = (
        f"the Git directory changed since injection (recorded {recorded}, now "
        f"{live}); run `setforge project remove demo {target}` to remove the "
        "injection, then inject again"
    )

    listed = runner.invoke(app, ["project", "list"])
    assert listed.exit_code == 1
    assert listed.output == (
        f"{target}  [demo]\n"
        f"  error: project target identity does not match the record; {remedy} "
        f"({state.name})\n"
    )
    reinjected = runner.invoke(app, ["project", "inject", "demo", *arguments, "--yes"])
    assert reinjected.exit_code == 1
    assert str(reinjected.exception) == (
        f"project profile 'demo' is already injected at {target}, but {remedy}"
    )
    synced = runner.invoke(app, ["project", "sync", str(target), "--dry-run"])
    assert synced.exit_code == 1
    assert str(synced.exception) == (
        f"project injection state does not match target identity: {state}; {remedy}"
    )
    assert state.read_bytes() == record
    assert _claim_lifecycles() == [ClaimLifecycle.CLAIMED]

    preview = runner.invoke(app, ["project", "remove", "demo", *arguments, "--dry-run"])
    assert preview.exit_code == 0, preview.output
    assert "stale injection" not in preview.output
    assert "  restore replace-untracked: AGENTS.md\n" in preview.output
    assert destination.read_bytes() == b"managed instructions\n"
    removed = runner.invoke(app, ["project", "remove", "demo", *arguments, "--yes"])

    assert removed.exit_code == 0, removed.output
    assert "\nremoval complete\n" in removed.output
    assert destination.read_bytes() == b"USER ORIGINAL\n"
    assert destination.stat().st_mode & 0o7777 == 0o640
    assert not state.exists()
    assert _claim_lifecycles() == [ClaimLifecycle.RELEASED]


def test_remove_restores_tracked_overlay_after_the_git_directory_was_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state_root))
    config = _config(tmp_path)
    (config.parent / "project" / "demo" / "AGENTS.md").write_text(
        "team instructions\nmanaged instructions\n"
    )
    target = _git_repo(tmp_path / "target")
    destination = target / "AGENTS.md"
    destination.write_text("team instructions\n")
    subprocess.run(["git", "-C", str(target), "add", "AGENTS.md"], check=True)
    runner = CliRunner()
    arguments = [str(target), "--config", str(config), "--yes"]
    injected = runner.invoke(
        app, ["project", "inject", "demo", *arguments, "--auto=use-profile"]
    )
    assert injected.exit_code == 0, injected.output
    shutil.rmtree(target / ".git")

    removed = runner.invoke(app, ["project", "remove", "demo", *arguments])

    assert removed.exit_code == 0, removed.output
    assert destination.read_text() == "team instructions\n"
    assert not manifest_path(target, "demo").exists()
    assert not list((state_root / "project-overlays").glob("*.json"))


def test_recreated_project_at_the_same_path_names_a_working_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    runner = CliRunner()
    arguments = [str(target), "--config", str(config), "--yes"]
    assert runner.invoke(app, ["project", "inject", "demo", *arguments]).exit_code == 0
    original_inode = target.stat().st_ino
    shutil.rmtree(target)
    _git_repo(target)
    destination = target / "AGENTS.md"

    if target.stat().st_ino != original_inode:
        dropped = runner.invoke(app, ["project", "remove", "demo", *arguments])
        assert dropped.exit_code == 0, dropped.output
        assert "stale injection: project directory was replaced" in dropped.output
        assert runner.invoke(app, ["project", "list"]).exit_code == 0
        return
    restore = f"`setforge project sync {target} --auto=use-profile`"
    remedy = (
        f"run {restore} to restore it, or `setforge project sync {target}` to "
        "keep it deleted"
    )
    listed = runner.invoke(app, ["project", "list"])
    assert listed.exit_code == 1
    assert (
        f"  error: AGENTS.md: injected project file is missing; {remedy} "
    ) in listed.output
    refused = runner.invoke(app, ["project", "inject", "demo", *arguments])
    assert refused.exit_code == 1
    assert str(refused.exception) == _already_injected(target, config)
    assert not destination.exists()

    restored = runner.invoke(
        app, ["project", "sync", str(target), "--auto=use-profile", "--yes"]
    )
    assert restored.exit_code == 0, restored.output
    assert destination.read_text() == "managed instructions\n"
    listed = runner.invoke(app, ["project", "list"])
    assert listed.output == f"{target}  [demo]\n  hidden: AGENTS.md\n"
    removed = runner.invoke(app, ["project", "remove", "demo", *arguments])
    assert removed.exit_code == 0, removed.output
    assert not destination.exists()


@pytest.mark.parametrize("damage", ["git-removed", "git-unreadable"])
def test_sibling_without_readable_git_identity_does_not_block_other_projects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    sibling = _git_repo(tmp_path / "sibling")
    runner = CliRunner()
    first = runner.invoke(
        app, ["project", "inject", "demo", str(sibling), "--config", str(config), "-y"]
    )
    assert first.exit_code == 0, first.output
    if damage == "git-removed":
        shutil.rmtree(sibling / ".git")
    else:
        (sibling / ".git" / "HEAD").write_text("not a ref\n")
    record = manifest_path(sibling, "demo").read_bytes()
    target = _git_repo(tmp_path / "target")

    injected = runner.invoke(
        app, ["project", "inject", "demo", str(target), "--config", str(config), "-y"]
    )

    assert injected.exit_code == 0, injected.output
    assert (target / "AGENTS.md").read_text() == "managed instructions\n"
    assert manifest_path(sibling, "demo").read_bytes() == record
    listed = runner.invoke(app, ["project", "list"])
    assert listed.exit_code == 1
    assert f"{target}  [demo]\n  hidden: AGENTS.md\n" in listed.output
    assert f"{sibling}  [demo]\n  error: " in listed.output


def test_remove_accepts_member_whose_local_file_was_kept_at_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    runner = CliRunner()
    arguments = [str(target), "--config", str(config), "--yes"]
    assert runner.invoke(app, ["project", "inject", "demo", *arguments]).exit_code == 0
    local = target / "EXTRA.md"
    local.write_bytes(b"local file\n")
    local.chmod(0o600)
    (config.parent / "project" / "demo" / "EXTRA.md").write_text("profile file\n")
    config.write_text(
        config.read_text()
        + "      extra:\n        src: EXTRA.md\n        dst: EXTRA.md\n"
    )
    kept = runner.invoke(
        app, ["project", "sync", str(target), "--auto=keep-live", "--yes"]
    )
    assert kept.exit_code == 0, kept.output
    assert local.read_bytes() == b"local file\n"
    inode = local.stat().st_ino

    removed = runner.invoke(app, ["project", "remove", "demo", *arguments])

    assert removed.exit_code == 0, removed.output
    assert local.read_bytes() == b"local file\n"
    assert local.stat().st_mode & 0o7777 == 0o600
    assert local.stat().st_ino == inode
    assert not (target / "AGENTS.md").exists()
    assert not manifest_path(target, "demo").exists()


@pytest.mark.parametrize("before", ["absent", "user-file", "tracked-file"])
def test_remove_completes_when_git_directory_changed_and_a_member_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, before: str
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state_root))
    config = _config(tmp_path)
    target = tmp_path / "target"
    destination = target / "AGENTS.md"
    runner = CliRunner()
    arguments = [str(target), "--config", str(config), "--yes"]
    if before == "tracked-file":
        (config.parent / "project" / "demo" / "AGENTS.md").write_text(
            "team instructions\nmanaged instructions\n"
        )
        _git_repo(target)
        destination.write_bytes(b"team instructions\n")
        destination.chmod(0o640)
        subprocess.run(["git", "-C", str(target), "add", "AGENTS.md"], check=True)
        arguments.append("--auto=use-profile")
    else:
        target.mkdir()
        if before == "user-file":
            destination.write_bytes(b"USER ORIGINAL\n")
            destination.chmod(0o640)
    injected = runner.invoke(app, ["project", "inject", "demo", *arguments])
    assert injected.exit_code == 0, injected.output
    destination.unlink()
    if before == "tracked-file":
        shutil.rmtree(target / ".git")
    else:
        subprocess.run(["git", "init", "-q", str(target)], check=True)
    for refused in (
        runner.invoke(app, ["project", "sync", str(target), "--yes"]),
        runner.invoke(app, ["project", "inject", "demo", *arguments]),
    ):
        assert refused.exit_code == 1
        assert f"run `setforge project remove demo {target}`" in str(refused.exception)

    removed = runner.invoke(app, ["project", "remove", "demo", *arguments[:3], "-y"])

    assert removed.exit_code == 0, removed.output
    assert "\nremoval complete\n" in removed.output
    assert {
        "absent": "  leave absent: AGENTS.md\n",
        "user-file": "  restore replace-untracked: AGENTS.md\n",
        "tracked-file": "  restore overlay-tracked: AGENTS.md\n",
    }[before] in removed.output
    if before == "absent":
        assert not destination.exists()
    else:
        assert destination.read_bytes() == (
            b"USER ORIGINAL\n" if before == "user-file" else b"team instructions\n"
        )
        assert destination.stat().st_mode & 0o7777 == 0o640
    assert not manifest_path(target, "demo").exists()
    assert not list((state_root / "project-overlays").glob("*.json"))
    assert _claim_lifecycles() == [ClaimLifecycle.RELEASED]
    listed = runner.invoke(app, ["project", "list"])
    assert listed.output == "no project injections recorded\n"


@pytest.mark.parametrize("drift", ["content", "mode"])
def test_drift_remedy_works_even_after_the_git_directory_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    runner = CliRunner()
    arguments = [str(target), "--config", str(config), "--yes"]
    assert runner.invoke(app, ["project", "inject", "demo", *arguments]).exit_code == 0
    destination = target / "AGENTS.md"
    if drift == "content":
        destination.write_text("local edit\n")
    else:
        destination.chmod(0o600)
    subprocess.run(["git", "init", "-q", str(target)], check=True)

    refused = runner.invoke(app, ["project", "remove", "demo", *arguments])
    assert refused.exit_code == 1
    assert str(refused.exception) == (
        f"injected project file has drifted: {destination}; its content or mode "
        "(expected 0644) differs from what was injected and removal would "
        "discard the difference. Save your changes elsewhere, delete the "
        "file, then remove again"
    )
    destination.unlink()
    removed = runner.invoke(app, ["project", "remove", "demo", *arguments])

    assert removed.exit_code == 0, removed.output
    assert not destination.exists()
    assert not manifest_path(target, "demo").exists()


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
    assert "conflicts with another injection in this repository" in str(
        tracked_conflict.exception
    )
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


def test_remove_preview_says_a_created_file_will_be_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    arguments = ["demo", str(target), "--config", str(config), "--yes"]
    injected = CliRunner().invoke(app, ["project", "inject", *arguments])
    assert injected.exit_code == 0, injected.output

    preview = CliRunner().invoke(
        app, ["project", "remove", *arguments[:-1], "--dry-run"]
    )

    assert preview.exit_code == 0, preview.output
    assert "  delete: AGENTS.md\n" in preview.output
    assert "restore" not in preview.output
    assert (target / "AGENTS.md").exists()


@pytest.mark.parametrize("schema", [1, 2])
def test_remove_reads_a_record_written_with_an_older_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema: int
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    arguments = ["demo", str(target), "--config", str(config), "--yes"]
    injected = CliRunner().invoke(app, ["project", "inject", *arguments])
    assert injected.exit_code == 0, injected.exception
    state = manifest_path(target, "demo")
    payload = json.loads(state.read_text())
    payload["schema"] = schema
    for entry in payload["files"]:
        del entry["visibility"]
    if schema == 1:
        del payload["config_path"]
        for entry in payload["files"]:
            del entry["applied_payload"]
            del entry["upstream_mode"]
            del entry["upstream_payload"]
    state.write_text(json.dumps(payload))

    removed = CliRunner().invoke(app, ["project", "remove", *arguments])

    assert removed.exit_code == 0, (removed.output, removed.exception)
    assert "  delete: AGENTS.md\n" in removed.output
    assert "restore" not in removed.output
    assert not (target / "AGENTS.md").exists()
    assert not state.exists()
    assert _claim_lifecycles() == [ClaimLifecycle.RELEASED]


def test_remove_restores_a_file_injected_under_an_empty_file_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    config.write_text(config.read_text().replace("instructions:", '"":'))
    target = _git_repo(tmp_path / "target")
    (target / "AGENTS.md").write_text("my own notes\n")
    arguments = ["demo", str(target), "--config", str(config), "--yes"]
    injected = CliRunner().invoke(app, ["project", "inject", *arguments])
    assert injected.exit_code == 0, (injected.output, injected.exception)
    assert (target / "AGENTS.md").read_text() == "managed instructions\n"
    state = manifest_path(target, "demo")
    assert json.loads(state.read_text())["files"][0]["file_id"] == ""

    for mode in ("--dry-run", "--yes"):
        removed = CliRunner().invoke(app, ["project", "remove", *arguments[:-1], mode])
        assert removed.exit_code == 0, (removed.output, removed.exception)
        assert "  restore replace-untracked: AGENTS.md\n" in removed.output

    assert (target / "AGENTS.md").read_text() == "my own notes\n"
    assert not state.exists()
    assert _claim_lifecycles() == [ClaimLifecycle.RELEASED]


def _non_mapping_entry(entry: dict[str, object]) -> object:
    return list(entry)


def _edited_entry(**changes: object) -> Callable[[dict[str, object]], object]:
    return lambda entry: {**entry, **changes}


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(_non_mapping_entry, id="non-mapping-entry"),
        pytest.param(_edited_entry(unexpected=1), id="unknown-field"),
        pytest.param(_edited_entry(upstream_payload="***"), id="payload-not-base64"),
        pytest.param(_edited_entry(upstream_mode="rw"), id="mode-not-a-number"),
        pytest.param(_edited_entry(action="bogus"), id="unknown-action"),
        pytest.param(_edited_entry(visibility="bogus"), id="unknown-visibility"),
        pytest.param(_edited_entry(destination="/etc/passwd"), id="absolute-path"),
        pytest.param(_edited_entry(destination="../AGENTS.md"), id="escaping-path"),
        pytest.param(_edited_entry(created_parents=["/etc"]), id="absolute-parent"),
        pytest.param(_edited_entry(created_parents=["unrelated"]), id="foreign-parent"),
    ],
)
def test_remove_refuses_a_malformed_file_record_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: Callable[[dict[str, object]], object],
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    (target / "unrelated").mkdir()
    arguments = ["demo", str(target), "--config", str(config), "--yes"]
    injected = CliRunner().invoke(app, ["project", "inject", *arguments])
    assert injected.exit_code == 0, injected.exception
    state = manifest_path(target, "demo")
    payload = json.loads(state.read_text())
    payload["files"][0] = tamper(payload["files"][0])
    state.write_text(json.dumps(payload))
    before_manifest = state.read_bytes()
    before_file = (target / "AGENTS.md").read_bytes()
    claim_path, before_claim = _rewrite_only_claim(lambda claim: claim)

    removed = CliRunner().invoke(app, ["project", "remove", *arguments])

    assert removed.exit_code == 1
    assert isinstance(removed.exception, SetforgeError)
    assert str(removed.exception).startswith("project injection state has ")
    assert state.read_bytes() == before_manifest
    assert (target / "AGENTS.md").read_bytes() == before_file
    assert (target / "unrelated").is_dir()
    assert claim_path.read_bytes() == before_claim


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


@pytest.mark.parametrize("visibility", ["hidden", "tracked"])
def test_git_injection_coexists_with_recorded_plain_directory(
    tmp_path: Path, visibility: str
) -> None:
    config = _config(tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()
    target = _git_repo(tmp_path / "target")
    runner = CliRunner()
    for destination in (plain, target):
        result = runner.invoke(
            app,
            [
                "project",
                "inject",
                "demo",
                str(destination),
                "--config",
                str(config),
                f"--git-{visibility}",
                "--yes",
            ],
        )
        assert result.exit_code == 0, (result.output, result.exception)

    listed = runner.invoke(app, ["project", "list"])
    assert listed.exit_code == 0, listed.output
    assert "not-applicable" in listed.output
    assert visibility in listed.output
    plain_manifest = manifest_path(plain, "demo").read_bytes()
    changed = runner.invoke(
        app,
        ["project", "visibility", str(target), "AGENTS.md", "--tracked", "--yes"],
    )
    assert changed.exit_code == 0, (changed.output, changed.exception)
    assert not (plain / ".git").exists()
    assert (plain / "AGENTS.md").read_text() == "managed instructions\n"
    assert manifest_path(plain, "demo").read_bytes() == plain_manifest


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
    other_owner = uuid.uuid4()
    claim_path, _ = _rewrite_only_claim(
        lambda claim: replace(
            claim,
            owner_id=other_owner,
            history=tuple(
                replace(event, owner_id=other_owner) for event in claim.history
            ),
        )
    )
    mismatched_claim = claim_path.read_bytes()

    for mode in ("--dry-run", "--yes"):
        reinjected = CliRunner().invoke(app, [*command[:-1], mode])
        assert reinjected.exit_code == 1
        assert str(reinjected.exception) == (
            "a project destination has a released ownership claim from another "
            "config checkout: AGENTS.md was injected by project profile 'demo' at "
            f"{target} (owner {other_owner}); inject it from that checkout, or "
            "inspect the claim with `setforge ownership list`"
        )
        assert not (target / "AGENTS.md").exists()
        assert not manifest_path(target, "demo").exists()
        assert claim_path.read_bytes() == mismatched_claim


@pytest.mark.parametrize("change", ["source", "profile", "moved"])
def test_reinject_after_remove_reclaims_this_checkouts_released_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _two_profiles_one_destination(tmp_path)
    target = _git_repo(tmp_path / "target")
    runner = CliRunner()
    arguments = ["--config", str(config), "--yes"]
    for verb in ("inject", "remove"):
        done = runner.invoke(app, ["project", verb, "demo", str(target), *arguments])
        assert done.exit_code == 0, done.output
    (released,) = OwnershipStore().list_claims()
    assert released.lifecycle is ClaimLifecycle.RELEASED
    profile, expected = "demo", "managed instructions v2\n"
    if change == "source":
        (config.parent / "project" / "demo" / "AGENTS.md").write_text(expected)
    elif change == "profile":
        profile, expected = "other", "other instructions\n"
    else:
        expected = "managed instructions\n"
        target = target.rename(tmp_path / "moved")

    preview = runner.invoke(
        app,
        [
            "project",
            "inject",
            profile,
            str(target),
            "--config",
            str(config),
            "--dry-run",
        ],
    )
    assert preview.exit_code == 0, preview.output
    injected = runner.invoke(
        app, ["project", "inject", profile, str(target), *arguments]
    )

    assert injected.exit_code == 0, injected.output
    assert (target / "AGENTS.md").read_text() == expected
    (claim,) = OwnershipStore().list_claims()
    assert claim.lifecycle is ClaimLifecycle.CLAIMED
    assert claim.resource_id == released.resource_id
    assert claim.owner_id == released.owner_id
    assert claim.locator == str(target / "AGENTS.md")
    file_id = "rules" if profile == "other" else "instructions"
    assert claim.declaration_refs == (f"project-profile:{profile}:{file_id}",)
    removed = runner.invoke(
        app, ["project", "remove", profile, str(target), *arguments]
    )
    assert removed.exit_code == 0, removed.output
    assert not (target / "AGENTS.md").exists()


def _injected_tracked_overlay(
    tmp_path: Path, *, draft: str = ""
) -> tuple[Path, list[str]]:
    """Inject over a committed ``AGENTS.md``; return the target and CLI arguments.

    ``draft`` is appended to the file after the commit, so the content saved at
    injection holds a line Git does not have.
    """
    config = _config(tmp_path)
    (config.parent / "project" / "demo" / "AGENTS.md").write_text(
        "team instructions\nmanaged instructions\n"
    )
    target = _git_repo(tmp_path / "target")
    (target / "AGENTS.md").write_text("team instructions\n")
    subprocess.run(["git", "-C", str(target), "add", "AGENTS.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(target),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-q",
            "-m",
            "team file",
        ],
        check=True,
    )
    (target / "AGENTS.md").write_text("team instructions\n" + draft)
    arguments = [str(target), "--config", str(config), "--yes"]
    injected = CliRunner().invoke(
        app, ["project", "inject", "demo", *arguments, "--auto=use-profile"]
    )
    assert injected.exit_code == 0, injected.output
    assert (target / "AGENTS.md").read_text() == (
        "team instructions\nmanaged instructions\n"
    )
    return target, arguments


def test_remove_does_not_recreate_a_tracked_overlay_file_that_git_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state_root))
    target, arguments = _injected_tracked_overlay(tmp_path)
    subprocess.run(
        ["git", "-C", str(target), "rm", "-q", "-f", "AGENTS.md"], check=True
    )
    destination = target / "AGENTS.md"
    assert not destination.exists()

    removed = CliRunner().invoke(app, ["project", "remove", "demo", *arguments])

    assert removed.exit_code == 0, (removed.output, removed.exception)
    assert "  leave absent: AGENTS.md\n" in removed.output
    assert "restore" not in removed.output
    assert not destination.exists()
    assert not manifest_path(target, "demo").exists()
    assert not list((state_root / "project-overlays").glob("*.json"))
    assert not (target / ".git" / "info" / "attributes").exists()
    assert _claim_lifecycles() == [ClaimLifecycle.RELEASED]
    status = subprocess.run(
        ["git", "-C", str(target), "status", "--short"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    assert status == "D  AGENTS.md\n"


@pytest.mark.parametrize(
    "draft", ["", "local draft\n"], ids=["committed-content", "uncommitted-line"]
)
def test_remove_restores_a_tracked_overlay_file_that_the_user_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, draft: str
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state_root))
    target, arguments = _injected_tracked_overlay(tmp_path, draft=draft)
    destination = target / "AGENTS.md"
    destination.unlink()

    removed = CliRunner().invoke(app, ["project", "remove", "demo", *arguments])

    assert removed.exit_code == 0, (removed.output, removed.exception)
    assert destination.read_text() == "team instructions\n" + draft
    assert not manifest_path(target, "demo").exists()
    assert not list((state_root / "project-overlays").glob("*.json"))
    assert not (target / ".git" / "info" / "attributes").exists()
    assert _claim_lifecycles() == [ClaimLifecycle.RELEASED]
    status = subprocess.run(
        ["git", "-C", str(target), "status", "--short"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    assert status == (" M AGENTS.md\n" if draft else "")


def test_missing_tracked_overlay_file_names_project_remove_not_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    target, arguments = _injected_tracked_overlay(tmp_path)
    destination = target / "AGENTS.md"
    destination.unlink()
    runner = CliRunner()
    remedy = (
        f"run `setforge project remove demo {target}` to remove the injection, or "
        "restore the file with Git"
    )

    listed = runner.invoke(app, ["project", "list"])
    assert listed.exit_code == 1
    assert (
        f"  error: AGENTS.md: injected project file is missing; {remedy} "
    ) in listed.output
    reinjected = runner.invoke(app, ["project", "inject", "demo", *arguments])
    assert reinjected.exit_code == 1
    assert str(reinjected.exception) == _already_injected(
        target, tmp_path / "config" / "setforge.yaml"
    )
    subprocess.run(
        ["git", "-C", str(target), "checkout", "--", "AGENTS.md"], check=True
    )
    assert destination.read_text() == "team instructions\nmanaged instructions\n"
    assert runner.invoke(app, ["project", "list"]).output == (
        f"{target}  [demo]\n  tracked-overlay: AGENTS.md\n"
    )


def test_remove_accepts_tracked_overlay_file_that_holds_only_the_committed_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state_root))
    target, arguments = _injected_tracked_overlay(tmp_path)
    destination = target / "AGENTS.md"
    destination.write_text("team instructions\n")

    removed = CliRunner().invoke(app, ["project", "remove", "demo", *arguments])

    assert removed.exit_code == 0, (removed.output, removed.exception)
    assert destination.read_text() == "team instructions\n"
    assert not manifest_path(target, "demo").exists()
    assert not list((state_root / "project-overlays").glob("*.json"))
    assert not (target / ".git" / "info" / "attributes").exists()


def test_tracked_overlay_injects_into_repository_without_info_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    (config.parent / "project" / "demo" / "AGENTS.md").write_text(
        "team instructions\nmanaged instructions\n"
    )
    (config.parent / "project" / "demo" / "NOTES.md").write_text("private notes\n")
    config.write_text(
        config.read_text()
        + "      notes:\n        src: NOTES.md\n        dst: NOTES.md\n"
    )
    target = _git_repo(tmp_path / "target")
    destination = target / "AGENTS.md"
    destination.write_text("team instructions\n")
    subprocess.run(["git", "-C", str(target), "add", "AGENTS.md"], check=True)
    info = target / ".git" / "info"
    shutil.rmtree(info)
    runner = CliRunner()
    arguments = [str(target), "--config", str(config), "--auto=use-profile"]

    preview = runner.invoke(app, ["project", "inject", "demo", *arguments, "--dry-run"])
    assert preview.exit_code == 0, (preview.output, preview.exception)
    assert not info.exists()
    injected = runner.invoke(app, ["project", "inject", "demo", *arguments, "--yes"])

    assert injected.exit_code == 0, (injected.output, injected.exception)
    assert destination.read_text() == "team instructions\nmanaged instructions\n"
    assert b"/AGENTS.md filter=setforge-project\n" in (info / "attributes").read_bytes()
    assert b"/NOTES.md\n" in (info / "exclude").read_bytes()
    diff = subprocess.run(
        ["git", "-C", str(target), "diff", "--", "AGENTS.md"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    assert diff == ""
    listed = runner.invoke(app, ["project", "list"])
    assert listed.output == (
        f"{target}  [demo]\n  tracked-overlay: AGENTS.md\n  hidden: NOTES.md\n"
    )
    removed = runner.invoke(app, ["project", "remove", "demo", *arguments[:3], "--yes"])
    assert removed.exit_code == 0, (removed.output, removed.exception)
    assert destination.read_text() == "team instructions\n"
    assert not (target / "NOTES.md").exists()
    assert not (info / "attributes").exists()
    assert (info / "exclude").read_bytes() == b""


def test_moved_project_whose_old_path_is_now_a_symlink_drops_its_stale_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    moved = tmp_path / "moved"
    runner = CliRunner()
    injected = runner.invoke(
        app, ["project", "inject", "demo", str(target), "--config", str(config), "-y"]
    )
    assert injected.exit_code == 0, injected.output
    stale_record = manifest_path(target, "demo")
    target.rename(moved)
    target.symlink_to(moved, target_is_directory=True)

    listed = runner.invoke(app, ["project", "list"])
    assert listed.exit_code == 1
    assert listed.output == (
        f"{target}  [demo]\n"
        "  error: project directory no longer exists; run "
        f"`setforge project remove demo {target}` to drop the stale record "
        f"({stale_record.name})\n"
    )

    dropped = runner.invoke(
        app, ["project", "remove", "demo", str(target), "--config", str(config), "-y"]
    )

    assert dropped.exit_code == 0, (dropped.output, dropped.exception)
    assert f"target: {target}\n" in dropped.output
    assert "stale injection: project directory no longer exists\n" in dropped.output
    assert dropped.output.endswith("stale injection dropped\n")
    assert not stale_record.exists()
    assert _claim_lifecycles() == [ClaimLifecycle.RELEASED]
    assert (moved / "AGENTS.md").read_text() == "managed instructions\n"
    assert target.is_symlink()
    assert runner.invoke(app, ["project", "list"]).output == (
        "no project injections recorded\n"
    )
