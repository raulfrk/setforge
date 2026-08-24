"""Public ownership inspection, release, history, revert, and recovery CLI."""

from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import app
from setforge.cli import ownership as ownership_cli
from setforge.config import load_config
from setforge.errors import OwnershipError
from setforge.file_ownership import observe_file, observe_tree
from setforge.locking import MutationLockGuards, install_resources_lock
from setforge.ownership import OwnershipStore, ResourceId, load_or_create_owner_id
from setforge.ownership_history import OwnershipHistoryStore
from setforge.provision.ownership import observation_fingerprint
from setforge.provision.protocol import Identity, ObservationOrigin, PackageObservation
from setforge.tree_management import scan_tree


def _owned_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, str]:
    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    repo = tmp_path / "config"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    tracked = repo / "tracked" / "doc.txt"
    tracked.parent.mkdir()
    tracked.write_text("managed\n", encoding="utf-8")
    live = tmp_path / "live" / "doc.txt"
    live.parent.mkdir()
    live.write_text("managed\n", encoding="utf-8")
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  doc:\n"
        "    src: doc.txt\n"
        f"    dst: {live}\n"
        "profiles:\n"
        "  default:\n"
        "    tracked_files: [doc]\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(repo), "add", "setforge.yaml", "tracked"], check=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=SetForge Test",
            "-c",
            "user.email=setforge@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    owner_id = load_or_create_owner_id(repo)
    observation = observe_file(live)
    ledger = OwnershipStore()
    with install_resources_lock():
        ledger.claim_locked(
            resource_id=observation.resource_id,
            owner_id=owner_id,
            declaration_refs=("tracked_files.doc",),
            provenance=(),
            locator=observation.locator,
            fingerprint=observation.fingerprint,
            expected_generation=None,
        )
    return config, live, ledger.claim_id(observation.resource_id)


def _owned_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, str, PackageObservation]:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    repo = tmp_path / "config"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files: {}\n"
        "packages:\n"
        "  ripgrep:\n"
        "    type: cargo\n"
        "    crate: ripgrep\n"
        "profiles:\n"
        "  default:\n"
        "    packages: [ripgrep]\n",
        encoding="utf-8",
    )
    owner_id = load_or_create_owner_id(repo)
    observation = PackageObservation(
        Identity("ripgrep", "ripgrep"),
        ObservationOrigin.EXTERNAL,
        version="14.1.1",
        source="cargo",
        locator="~/.cargo/bin/rg",
        fingerprint="provider-state",
    )
    ledger = OwnershipStore()
    resource_id = ResourceId.package("cargo", "ripgrep")
    with install_resources_lock():
        ledger.claim_locked(
            resource_id=resource_id,
            owner_id=owner_id,
            declaration_refs=("packages.cargo.ripgrep",),
            provenance=(),
            locator=observation.locator or "ripgrep",
            fingerprint=observation_fingerprint(observation),
            expected_generation=None,
        )
    return config, ledger.claim_id(resource_id), observation


def _owned_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, str]:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    repo = tmp_path / "config"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    source = repo / "tracked" / "tools"
    source.mkdir(parents=True)
    (source / "tool.txt").write_text("managed\n", encoding="utf-8")
    live = tmp_path / "live" / "tools"
    live.mkdir(parents=True)
    (live / "tool.txt").write_text("managed\n", encoding="utf-8")
    config = repo / "setforge.yaml"
    config.write_text(
        "schema_version: '6.2'\n"
        "minimum_version: '6.2'\n"
        "tracked_files:\n"
        "  tools:\n"
        "    src: tools\n"
        f"    dst: {live}\n"
        "    tree: {}\n"
        "profiles:\n"
        "  default:\n"
        "    tracked_files: [tools]\n",
        encoding="utf-8",
    )
    owner_id = load_or_create_owner_id(repo)
    policy = load_config(config).tracked_files["tools"].tree
    assert policy is not None
    inventory = scan_tree(
        live, policy.model_copy(update={"symlinks": "preserve"})
    ).inventory
    observation = observe_tree(live, inventory.fingerprint)
    ledger = OwnershipStore()
    with install_resources_lock():
        ledger.claim_locked(
            resource_id=observation.resource_id,
            owner_id=owner_id,
            declaration_refs=("tracked_files.tools",),
            provenance=(),
            locator=observation.locator,
            fingerprint=observation.fingerprint,
            expected_generation=None,
        )
    return config, live, ledger.claim_id(observation.resource_id)


def test_ownership_commands_are_discoverable_by_shell_completion() -> None:
    runner = CliRunner()
    root = runner.invoke(
        app,
        [],
        prog_name="setforge",
        env={
            "_SETFORGE_COMPLETE": "complete_bash",
            "COMP_WORDS": "setforge own",
            "COMP_CWORD": "1",
        },
    )
    leaves = runner.invoke(
        app,
        [],
        prog_name="setforge",
        env={
            "_SETFORGE_COMPLETE": "complete_bash",
            "COMP_WORDS": "setforge ownership r",
            "COMP_CWORD": "2",
        },
    )

    assert root.exit_code == 0
    assert root.stdout == "ownership\n"
    assert leaves.exit_code == 0
    assert set(leaves.stdout.splitlines()) == {"recover", "release", "revert"}


def test_ownership_list_human_json_and_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config, _live, claim_id = _owned_file(tmp_path, monkeypatch)
    runner = CliRunner()

    human = runner.invoke(app, ["ownership", "list"])
    assert human.exit_code == 0, human.output
    assert claim_id in human.stdout
    assert "tracked_files.doc" in human.stdout
    assert "claimed" in human.stdout

    structured = runner.invoke(app, ["--format=json", "ownership", "list"])
    assert structured.exit_code == 0, structured.output
    envelope = json.loads(structured.stdout)
    assert envelope["schema_version"] == 1
    assert envelope["command"] == "ownership list"
    item = envelope["data"]["claims"][0]
    assert item["claim_id"] == claim_id
    assert set(item) == {
        "authority",
        "claim_id",
        "declaration_refs",
        "fingerprint",
        "generation",
        "history_summary",
        "lifecycle",
        "locator",
        "owner_id",
        "provenance",
        "resource_id",
        "scope",
    }

    quiet = runner.invoke(app, ["--quiet", "ownership", "list"])
    assert quiet.exit_code == 0
    assert quiet.stdout == ""


def test_release_history_show_and_revert_preserve_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, live, claim_id = _owned_file(tmp_path, monkeypatch)
    runner = CliRunner()

    released = runner.invoke(
        app,
        ["ownership", "release", claim_id, "--config", str(config), "--yes"],
    )
    assert released.exit_code == 0, released.output
    assert live.read_text(encoding="utf-8") == "managed\n"
    transition_id = released.stdout.strip().split()[-1]

    history = runner.invoke(
        app,
        ["--format=json", "ownership", "history", "--config", str(config)],
    )
    assert history.exit_code == 0, history.output
    records = json.loads(history.stdout)["data"]["transitions"]
    assert [record["transition_id"] for record in records] == [transition_id]
    assert records[0]["action"] == "release"

    shown = runner.invoke(
        app,
        [
            "--format=json",
            "ownership",
            "history",
            transition_id,
            "--config",
            str(config),
        ],
    )
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.stdout)["data"]["transition_id"] == transition_id

    reverted = runner.invoke(
        app,
        ["ownership", "revert", transition_id, "--config", str(config), "--yes"],
    )
    assert reverted.exit_code == 0, reverted.output
    assert "reverted ownership transition" in reverted.stdout
    assert live.read_text(encoding="utf-8") == "managed\n"


def test_revert_refuses_drifted_file_and_writes_no_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, live, claim_id = _owned_file(tmp_path, monkeypatch)
    runner = CliRunner()
    released = runner.invoke(
        app,
        ["ownership", "release", claim_id, "--config", str(config), "--yes"],
    )
    assert released.exit_code == 0, released.output
    transition_id = released.stdout.strip().split()[-1]
    live.write_text("drifted\n", encoding="utf-8")

    reverted = runner.invoke(
        app,
        ["ownership", "revert", transition_id, "--config", str(config), "--yes"],
    )

    assert reverted.exit_code == 1
    assert "fingerprint changed" in str(reverted.exception)
    history = runner.invoke(app, ["ownership", "history", "--config", str(config)])
    assert history.exit_code == 0
    assert history.stdout.count(transition_id) == 1


def test_revert_revalidates_live_file_immediately_before_authority_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, live, claim_id = _owned_file(tmp_path, monkeypatch)
    owner_id = load_or_create_owner_id(config.parent)
    runner = CliRunner()
    released = runner.invoke(
        app,
        ["ownership", "release", claim_id, "--config", str(config), "--yes"],
    )
    assert released.exit_code == 0, released.output
    transition_id = released.stdout.strip().split()[-1]
    original = ownership_cli._validate_authority
    calls = 0

    def _race_after_validation(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        original(*args, **kwargs)  # type: ignore[arg-type]
        if calls == 1:
            live.write_text("raced after first validation\n", encoding="utf-8")

    monkeypatch.setattr(ownership_cli, "_validate_authority", _race_after_validation)
    reverted = runner.invoke(
        app,
        ["ownership", "revert", transition_id, "--config", str(config), "--yes"],
    )

    assert reverted.exit_code == 1
    assert "fingerprint changed" in str(reverted.exception)
    assert calls == 2
    current = OwnershipStore().read_claim_id(claim_id)
    assert current is not None
    assert current.lifecycle.value == "released"
    pending = OwnershipHistoryStore().pending(owner_id)
    assert len(pending) == 1
    assert pending[0].after.lifecycle.value == "claimed"

    live.write_text("managed\n", encoding="utf-8")
    monkeypatch.setattr(ownership_cli, "_validate_authority", original)
    recovered = runner.invoke(
        app,
        ["ownership", "recover", "--config", str(config), "--apply", "--yes"],
    )
    assert recovered.exit_code == 0, recovered.output
    assert OwnershipHistoryStore().pending(owner_id) == ()
    restored = OwnershipStore().read_claim_id(claim_id)
    assert restored is not None
    assert restored.lifecycle.value == "claimed"


def test_revert_revalidates_config_after_pending_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _live, claim_id = _owned_file(tmp_path, monkeypatch)
    runner = CliRunner()
    released = runner.invoke(
        app,
        ["ownership", "release", claim_id, "--config", str(config), "--yes"],
    )
    transition_id = released.stdout.strip().split()[-1]
    original = ownership_cli._validate_authority
    calls = 0

    def _race_config(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        original(*args, **kwargs)  # type: ignore[arg-type]
        if calls == 1:
            config.write_text(
                config.read_text(encoding="utf-8") + "# raced\n",
                encoding="utf-8",
            )

    monkeypatch.setattr(ownership_cli, "_validate_authority", _race_config)
    reverted = runner.invoke(
        app,
        ["ownership", "revert", transition_id, "--config", str(config), "--yes"],
    )

    assert reverted.exit_code == 1
    assert "configuration changed" in str(reverted.exception)
    assert calls == 2
    current = OwnershipStore().read_claim_id(claim_id)
    assert current is not None
    assert current.lifecycle.value == "released"


def test_revert_rechecks_owner_inside_grant_lock_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _live, claim_id = _owned_file(tmp_path, monkeypatch)
    runner = CliRunner()
    released = runner.invoke(
        app,
        ["ownership", "release", claim_id, "--config", str(config), "--yes"],
    )
    transition_id = released.stdout.strip().split()[-1]
    owner_file = config.parent / ".git" / "setforge" / "owner-id"
    original_locks = ownership_cli.mutation_locks

    @contextmanager
    def _race_owner(**kwargs: object) -> Iterator[MutationLockGuards]:
        with original_locks(**kwargs) as guards:  # type: ignore[arg-type]
            owner_file.write_text(f"{uuid.uuid4()}\n", encoding="ascii")
            yield guards

    monkeypatch.setattr(ownership_cli, "mutation_locks", _race_owner)
    reverted = runner.invoke(
        app,
        ["ownership", "revert", transition_id, "--config", str(config), "--yes"],
    )

    assert reverted.exit_code == 1
    assert "owner identity changed" in str(reverted.exception)
    current = OwnershipStore().read_claim_id(claim_id)
    assert current is not None
    assert current.lifecycle.value == "released"


def test_granting_recovery_rechecks_owner_inside_lock_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _live, claim_id = _owned_file(tmp_path, monkeypatch)
    owner_id = load_or_create_owner_id(config.parent)
    runner = CliRunner()
    released = runner.invoke(
        app,
        ["ownership", "release", claim_id, "--config", str(config), "--yes"],
    )
    transition_id = released.stdout.strip().split()[-1]
    history = OwnershipHistoryStore()
    calls = 0

    def _interrupt_grant(_claim: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OwnershipError("injected grant interruption")

    with (
        install_resources_lock(),
        pytest.raises(OwnershipError, match="grant interruption"),
    ):
        history.revert_locked(
            OwnershipStore(),
            owner_id,
            transition_id,
            validate_authority=_interrupt_grant,
        )
    assert history.pending(owner_id)
    owner_file = config.parent / ".git" / "setforge" / "owner-id"
    original_locks = ownership_cli.mutation_locks

    @contextmanager
    def _race_owner(**kwargs: object) -> Iterator[MutationLockGuards]:
        with original_locks(**kwargs) as guards:  # type: ignore[arg-type]
            owner_file.write_text(f"{uuid.uuid4()}\n", encoding="ascii")
            yield guards

    monkeypatch.setattr(ownership_cli, "mutation_locks", _race_owner)
    recovered = runner.invoke(
        app,
        ["ownership", "recover", "--config", str(config), "--apply", "--yes"],
    )

    assert recovered.exit_code == 1
    assert "owner identity changed" in str(recovered.exception)
    current = OwnershipStore().read_claim_id(claim_id)
    assert current is not None
    assert current.lifecycle.value == "released"
    assert history.pending(owner_id)


def test_revert_target_guard_refuses_tree_root_swap_after_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, live, claim_id = _owned_tree(tmp_path, monkeypatch)
    runner = CliRunner()
    released = runner.invoke(
        app,
        ["ownership", "release", claim_id, "--config", str(config), "--yes"],
    )
    transition_id = released.stdout.strip().split()[-1]
    original = ownership_cli._validate_authority
    calls = 0

    def _swap_tree(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        original(*args, **kwargs)  # type: ignore[arg-type]
        if calls == 1:
            moved = live.with_name("tools-before-swap")
            live.rename(moved)
            live.mkdir()
            (live / "tool.txt").write_text("managed\n", encoding="utf-8")

    monkeypatch.setattr(ownership_cli, "_validate_authority", _swap_tree)
    reverted = runner.invoke(
        app,
        ["ownership", "revert", transition_id, "--config", str(config), "--yes"],
    )

    assert reverted.exit_code == 1
    assert "target changed" in str(reverted.exception)
    assert calls == 2
    current = OwnershipStore().read_claim_id(claim_id)
    assert current is not None
    assert current.lifecycle.value == "released"


def test_release_requires_yes_when_noninteractive_and_does_not_mint_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _live, claim_id = _owned_file(tmp_path, monkeypatch)
    owner_file = config.parent / ".git" / "setforge" / "owner-id"
    before = owner_file.read_bytes()

    result = CliRunner().invoke(
        app, ["ownership", "release", claim_id, "--config", str(config)]
    )

    assert result.exit_code == 1
    assert "requires --yes" in str(result.exception)
    assert owner_file.read_bytes() == before
    assert OwnershipHistoryStore().list(load_or_create_owner_id(config.parent)) == ()


def test_ownership_recover_inspects_then_applies_pending_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _live, claim_id = _owned_file(tmp_path, monkeypatch)
    owner_id = load_or_create_owner_id(config.parent)
    history = OwnershipHistoryStore()
    original = OwnershipHistoryStore._commit_transition
    monkeypatch.setattr(
        OwnershipHistoryStore,
        "_commit_transition",
        lambda _self, _transition: (_ for _ in ()).throw(RuntimeError("crash")),
    )
    with install_resources_lock(), pytest.raises(RuntimeError, match="crash"):
        history.release_locked(OwnershipStore(), owner_id, claim_id)
    monkeypatch.setattr(OwnershipHistoryStore, "_commit_transition", original)
    transition_id = str(history.pending(owner_id)[0].transition_id)

    inspected = CliRunner().invoke(
        app, ["ownership", "recover", "--config", str(config)]
    )
    assert inspected.exit_code == 0, inspected.output
    assert transition_id in inspected.stdout
    assert "--apply" in inspected.stdout
    assert history.pending(owner_id)

    applied = CliRunner().invoke(
        app,
        ["ownership", "recover", "--config", str(config), "--apply", "--yes"],
    )
    assert applied.exit_code == 0, applied.output
    assert "recovered 1 ownership transition" in applied.stdout
    assert history.pending(owner_id) == ()


def test_history_isolated_between_clone_owners_and_shared_by_worktrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _live, claim_id = _owned_file(tmp_path, monkeypatch)
    base = config.parent
    clone = tmp_path / "clone"
    worktree = tmp_path / "worktree"
    subprocess.run(["git", "clone", "-q", str(base), str(clone)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(base),
            "worktree",
            "add",
            "-qb",
            "fixture-worktree",
            str(worktree),
        ],
        check=True,
    )
    clone_owner = load_or_create_owner_id(clone)
    base_owner = load_or_create_owner_id(base)
    assert clone_owner != base_owner
    assert load_or_create_owner_id(worktree) == base_owner

    released = CliRunner().invoke(
        app,
        ["ownership", "release", claim_id, "--config", str(config), "--yes"],
    )
    assert released.exit_code == 0, released.output
    transition_id = released.stdout.strip().split()[-1]

    linked = CliRunner().invoke(
        app,
        ["ownership", "history", "--config", str(worktree / "setforge.yaml")],
    )
    assert linked.exit_code == 0, linked.output
    assert transition_id in linked.stdout

    isolated = CliRunner().invoke(
        app,
        ["ownership", "history", "--config", str(clone / "setforge.yaml")],
    )
    assert isolated.exit_code == 0, isolated.output
    assert isolated.stdout == "(no ownership transitions)\n"


def test_package_revert_revalidates_provider_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, claim_id, observation = _owned_package(tmp_path, monkeypatch)

    class _Provider:
        def probe(self) -> set[Identity]:
            return {observation.identity}

        def observations(
            self, _installed: set[Identity]
        ) -> tuple[PackageObservation, ...]:
            return (observation,)

    monkeypatch.setattr("setforge.cli.ownership.build", lambda _item: _Provider())
    runner = CliRunner()
    released = runner.invoke(
        app,
        ["ownership", "release", claim_id, "--config", str(config), "--yes"],
    )
    assert released.exit_code == 0, released.output
    transition_id = released.stdout.strip().split()[-1]

    reverted = runner.invoke(
        app,
        ["ownership", "revert", transition_id, "--config", str(config), "--yes"],
    )
    assert reverted.exit_code == 0, reverted.output

    with install_resources_lock():
        latest = OwnershipStore().read_claim_id(claim_id)
    assert latest is not None
    assert latest.owner_id == load_or_create_owner_id(config.parent)
    assert latest.lifecycle.value == "claimed"


def test_package_revert_revalidates_inventory_after_pending_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, claim_id, observation = _owned_package(tmp_path, monkeypatch)
    calls = 0

    class _Provider:
        def probe(self) -> set[Identity]:
            return {observation.identity}

        def observations(
            self, _installed: set[Identity]
        ) -> tuple[PackageObservation, ...]:
            nonlocal calls
            calls += 1
            if calls == 1:
                return (observation,)
            return (replace(observation, fingerprint="raced-provider-state"),)

    monkeypatch.setattr("setforge.cli.ownership.build", lambda _item: _Provider())
    runner = CliRunner()
    released = runner.invoke(
        app,
        ["ownership", "release", claim_id, "--config", str(config), "--yes"],
    )
    transition_id = released.stdout.strip().split()[-1]
    reverted = runner.invoke(
        app,
        ["ownership", "revert", transition_id, "--config", str(config), "--yes"],
    )

    assert reverted.exit_code == 1
    assert "package fingerprint changed" in str(reverted.exception)
    assert calls == 2
    current = OwnershipStore().read_claim_id(claim_id)
    assert current is not None
    assert current.lifecycle.value == "released"


def test_release_rejects_malformed_claim_id_before_history_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _live, claim_id = _owned_file(tmp_path, monkeypatch)
    owner_id = load_or_create_owner_id(config.parent)

    result = CliRunner().invoke(
        app,
        ["ownership", "release", claim_id.upper(), "--config", str(config), "--yes"],
    )

    assert result.exit_code == 1
    assert "64 lowercase" in str(result.exception)
    assert OwnershipHistoryStore().list(owner_id) == ()


@pytest.mark.parametrize("git_backed", [False, True])
def test_history_fails_closed_without_existing_checkout_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_backed: bool
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    repo = tmp_path / "config"
    repo.mkdir()
    if git_backed:
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
    config = repo / "setforge.yaml"
    config.write_text("version: 1\nprofiles: {}\n", encoding="utf-8")

    result = CliRunner().invoke(app, ["ownership", "history", "--config", str(config)])

    assert result.exit_code == 1
    if git_backed:
        assert "owner identity directory is missing" in str(result.exception)
        assert not (repo / ".git" / "setforge").exists()
    else:
        assert "Git-backed" in str(result.exception)
