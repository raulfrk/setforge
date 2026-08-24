from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from click.testing import Result
from typer.testing import CliRunner

import setforge.cli.install as install_mod
import setforge.cli.revert as revert_mod
from setforge import transitions
from setforge.cli import app
from setforge.file_ownership import file_resource_id, observe_file, observe_tree
from setforge.ownership import Authority, OwnershipError, OwnershipStore, read_owner_id
from setforge.tree_management import read_inventory


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    repo = tmp_path / "repo"
    (repo / "tracked").mkdir(parents=True)
    (repo / "tracked" / "note.md").write_text("shared\n", encoding="utf-8")
    config = repo / "setforge.yaml"
    config.write_text(
        "schema_version: '6.0'\n"
        "tracked_files:\n"
        "  note:\n"
        "    src: note.md\n"
        "    dst: ~/.config/example/note.md\n"
        "profiles:\n"
        "  p:\n"
        "    tracked_files: [note]\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True)
    return config, home / ".config" / "example" / "note.md"


def _install(config: Path, *, yes: bool) -> Result:
    args = [
        "install",
        "--profile=p",
        f"--config={config}",
        "--no-fetch",
        "--no-git-check",
        "--no-secrets-scan",
    ]
    if yes:
        args.append("--yes")
    return CliRunner().invoke(app, args)


def test_install_adopts_existing_file_without_replacing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, live = _setup(tmp_path, monkeypatch)
    live.parent.mkdir(parents=True)
    live.write_text("host local\n", encoding="utf-8")

    result = _install(config, yes=True)

    assert result.exit_code == 0, result.output
    assert live.read_text(encoding="utf-8") == "host local\n"
    claim = OwnershipStore().read(file_resource_id(live))
    assert claim is not None
    assert claim.authority is Authority.MANAGE
    assert claim.owner_id == read_owner_id(config.parent)


def test_install_refuses_unconfirmed_existing_file_adoption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, live = _setup(tmp_path, monkeypatch)
    live.parent.mkdir(parents=True)
    live.write_text("host local\n", encoding="utf-8")

    result = _install(config, yes=False)

    assert result.exit_code != 0
    assert "file adoption requires confirmation" in str(result.exception)
    assert live.read_text(encoding="utf-8") == "host local\n"
    assert OwnershipStore().read(file_resource_id(live)) is None


def test_install_transfers_foreign_file_and_revert_redo_preserve_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_a, live = _setup(tmp_path, monkeypatch)
    live.parent.mkdir(parents=True)
    live.write_text("host local\n", encoding="utf-8")
    assert _install(config_a, yes=True).exit_code == 0
    store = OwnershipStore()
    before = store.read(file_resource_id(live))
    assert before is not None

    repo_b = tmp_path / "repo-b"
    (repo_b / "tracked").mkdir(parents=True)
    (repo_b / "tracked" / "note.md").write_text("shared\n")
    config_b = repo_b / "setforge.yaml"
    config_b.write_bytes(config_a.read_bytes())
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_b, check=True)

    unrecorded = CliRunner().invoke(
        app,
        [
            "install",
            "--profile=p",
            f"--config={config_b}",
            "--no-fetch",
            "--no-git-check",
            "--no-secrets-scan",
            "--no-transition",
            "--yes",
        ],
    )
    assert unrecorded.exit_code != 0
    assert "requires transition recording" in str(unrecorded.exception)
    assert store.read(file_resource_id(live)) == before

    transferred = _install(config_b, yes=True)

    assert transferred.exit_code == 0, transferred.output
    assert "transferred tracked file ownership" in transferred.output
    assert live.read_text(encoding="utf-8") == "host local\n"
    after = store.read(file_resource_id(live))
    assert after is not None
    assert after.owner_id == read_owner_id(repo_b)
    assert after.owner_id != before.owner_id
    assert after.generation == before.generation + 1

    latest = transitions.load_latest("p")
    assert latest is not None
    shown = CliRunner().invoke(app, ["transitions", "show", latest.name])
    assert shown.exit_code == 0, shown.output
    assert "ownership transfers" in shown.output
    assert str(before.owner_id) in shown.output
    assert str(after.owner_id) in shown.output

    config_b_before = config_b.read_bytes()
    config_b.write_text(
        config_b.read_text(encoding="utf-8")
        .replace("  note:\n", "  renamed:\n")
        .replace("tracked_files: [note]", "tracked_files: [renamed]"),
        encoding="utf-8",
    )
    declaration_drift = CliRunner().invoke(
        app, ["revert", "--profile=p", f"--config={config_b}", "--yes"]
    )
    assert declaration_drift.exit_code != 0
    assert "ownership declaration changed" in str(declaration_drift.exception)
    assert store.read(file_resource_id(live)) == after
    config_b.write_bytes(config_b_before)

    unauthorized = CliRunner().invoke(
        app, ["revert", "--profile=p", f"--config={config_a}", "--yes"]
    )
    assert unauthorized.exit_code != 0
    assert "only the current ownership recipient" in str(unauthorized.exception)
    assert store.read(file_resource_id(live)) == after

    live.write_text("changed after transfer\n", encoding="utf-8")
    refused = CliRunner().invoke(
        app, ["revert", "--profile=p", f"--config={config_b}", "--yes"]
    )
    assert refused.exit_code != 0
    assert "ownership resource changed since transition" in str(refused.exception)
    assert store.read(file_resource_id(live)) == after
    assert live.read_text(encoding="utf-8") == "changed after transfer\n"
    live.write_text("host local\n", encoding="utf-8")

    real_write_reverse = revert_mod._write_reverse_transition

    def fail_reverse_transition(*args: object, **kwargs: object) -> Path:
        raise OSError("injected reverse-transition failure")

    monkeypatch.setattr(
        revert_mod,
        "_write_reverse_transition",
        fail_reverse_transition,
    )
    interrupted = CliRunner().invoke(
        app, ["revert", "--profile=p", f"--config={config_b}", "--yes"]
    )
    assert interrupted.exit_code != 0
    assert store.read(file_resource_id(live)) == after
    assert live.read_text(encoding="utf-8") == "host local\n"
    monkeypatch.setattr(revert_mod, "_write_reverse_transition", real_write_reverse)

    reverted = CliRunner().invoke(
        app, ["revert", "--profile=p", f"--config={config_b}", "--yes"]
    )
    assert reverted.exit_code == 0, reverted.output
    restored = store.read(file_resource_id(live))
    assert restored is not None
    assert restored.owner_id == before.owner_id
    assert live.read_text(encoding="utf-8") == "host local\n"

    redone = CliRunner().invoke(
        app, ["revert", "--profile=p", f"--config={config_a}", "--yes"]
    )
    assert redone.exit_code == 0, redone.output
    final = store.read(file_resource_id(live))
    assert final is not None
    assert final.owner_id == after.owner_id
    assert live.read_text(encoding="utf-8") == "host local\n"


def test_install_refuses_unconfirmed_foreign_file_transfer_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_a, live = _setup(tmp_path, monkeypatch)
    live.parent.mkdir(parents=True)
    live.write_text("host local\n", encoding="utf-8")
    assert _install(config_a, yes=True).exit_code == 0
    store = OwnershipStore()
    before = store.read(file_resource_id(live))
    assert before is not None
    transitions_before = transitions.list_transitions(profile_filter=["p"])

    repo_b = tmp_path / "repo-b"
    (repo_b / "tracked").mkdir(parents=True)
    (repo_b / "tracked" / "note.md").write_text("shared\n", encoding="utf-8")
    config_b = repo_b / "setforge.yaml"
    config_b.write_bytes(config_a.read_bytes())
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_b, check=True)

    result = _install(config_b, yes=False)

    assert result.exit_code != 0
    assert "file ownership transfer requires confirmation" in str(result.exception)
    assert store.read(file_resource_id(live)) == before
    assert live.read_text(encoding="utf-8") == "host local\n"
    assert transitions.list_transitions(profile_filter=["p"]) == transitions_before
    with pytest.raises(OwnershipError):
        read_owner_id(repo_b)


def test_later_install_failure_recovers_foreign_file_transfer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_a, live = _setup(tmp_path, monkeypatch)
    live.parent.mkdir(parents=True)
    live.write_text("host local\n", encoding="utf-8")
    assert _install(config_a, yes=True).exit_code == 0
    store = OwnershipStore()
    before = store.read(file_resource_id(live))
    assert before is not None
    transitions_before = transitions.list_transitions(profile_filter=["p"])

    repo_b = tmp_path / "repo-b"
    (repo_b / "tracked").mkdir(parents=True)
    (repo_b / "tracked" / "note.md").write_text("shared\n", encoding="utf-8")
    config_b = repo_b / "setforge.yaml"
    config_b.write_bytes(config_a.read_bytes())
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_b, check=True)

    def fail_after_transfer(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected later install failure")

    monkeypatch.setattr(install_mod, "_apply_capability_targets", fail_after_transfer)
    result = _install(config_b, yes=True)

    assert result.exit_code != 0
    assert store.read(file_resource_id(live)) == before
    assert live.read_text(encoding="utf-8") == "host local\n"
    assert transitions.list_transitions(profile_filter=["p"]) == transitions_before


def test_install_refuses_receiver_identity_change_before_file_transfer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_a, live = _setup(tmp_path, monkeypatch)
    live.parent.mkdir(parents=True)
    live.write_text("host local\n", encoding="utf-8")
    assert _install(config_a, yes=True).exit_code == 0
    store = OwnershipStore()
    before = store.read(file_resource_id(live))
    assert before is not None

    repo_b = tmp_path / "repo-b"
    (repo_b / "tracked").mkdir(parents=True)
    (repo_b / "tracked" / "note.md").write_text("shared\n", encoding="utf-8")
    config_b = repo_b / "setforge.yaml"
    config_b.write_bytes(config_a.read_bytes())
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_b, check=True)
    monkeypatch.setattr(
        install_mod, "load_or_create_owner_id_locked", lambda *args: uuid4()
    )

    result = _install(config_b, yes=True)

    assert result.exit_code != 0
    assert "config owner identity changed after confirmation" in str(result.exception)
    assert store.read(file_resource_id(live)) == before
    assert live.read_text(encoding="utf-8") == "host local\n"


def test_install_refuses_declaration_change_after_transfer_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_a, live = _setup(tmp_path, monkeypatch)
    live.parent.mkdir(parents=True)
    live.write_text("host local\n", encoding="utf-8")
    assert _install(config_a, yes=True).exit_code == 0
    store = OwnershipStore()
    before = store.read(file_resource_id(live))
    assert before is not None

    repo_b = tmp_path / "repo-b"
    (repo_b / "tracked").mkdir(parents=True)
    (repo_b / "tracked" / "note.md").write_text("shared\n", encoding="utf-8")
    config_b = repo_b / "setforge.yaml"
    config_b.write_bytes(config_a.read_bytes())
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_b, check=True)
    real_confirm = install_mod._confirm_file_adoptions

    def confirm_then_rename(*args: object, **kwargs: object) -> None:
        real_confirm(*args, **kwargs)  # type: ignore[arg-type]
        config_b.write_text(
            config_b.read_text(encoding="utf-8")
            .replace("  note:\n", "  renamed:\n")
            .replace("tracked_files: [note]", "tracked_files: [renamed]"),
            encoding="utf-8",
        )

    monkeypatch.setattr(install_mod, "_confirm_file_adoptions", confirm_then_rename)
    result = _install(config_b, yes=True)

    assert result.exit_code != 0
    assert "configuration changed after confirmation" in str(result.exception)
    assert store.read(file_resource_id(live)) == before
    assert live.read_text(encoding="utf-8") == "host local\n"
    with pytest.raises(OwnershipError):
        read_owner_id(repo_b)


def test_install_refuses_non_git_receiver_of_foreign_file_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_a, live = _setup(tmp_path, monkeypatch)
    live.parent.mkdir(parents=True)
    live.write_text("host local\n", encoding="utf-8")
    assert _install(config_a, yes=True).exit_code == 0
    store = OwnershipStore()
    before = store.read(file_resource_id(live))
    assert before is not None

    repo_b = tmp_path / "not-git"
    (repo_b / "tracked").mkdir(parents=True)
    (repo_b / "tracked" / "note.md").write_text("shared\n", encoding="utf-8")
    config_b = repo_b / "setforge.yaml"
    config_b.write_bytes(config_a.read_bytes())

    result = _install(config_b, yes=True)

    assert result.exit_code != 0
    assert "ownership transfer requires a Git-backed config" in str(result.exception)
    assert store.read(file_resource_id(live)) == before
    assert live.read_text(encoding="utf-8") == "host local\n"


def test_install_claims_file_created_by_setforge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, live = _setup(tmp_path, monkeypatch)

    result = _install(config, yes=True)

    assert result.exit_code == 0, result.output
    assert live.read_text(encoding="utf-8") == "shared\n"
    claim = OwnershipStore().read(file_resource_id(live))
    assert claim is not None
    assert claim.authority is Authority.MANAGE
    assert claim.owner_id == read_owner_id(config.parent)


def test_install_refreshes_claim_after_managed_file_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, live = _setup(tmp_path, monkeypatch)
    assert _install(config, yes=True).exit_code == 0
    store = OwnershipStore()
    before = store.read(file_resource_id(live))
    assert before is not None
    (config.parent / "tracked" / "note.md").write_text("updated\n", encoding="utf-8")

    result = _install(config, yes=True)

    assert result.exit_code == 0, result.output
    after = store.read(file_resource_id(live))
    assert after is not None
    assert after.generation > before.generation
    assert after.fingerprint == observe_file(live).fingerprint
    assert live.read_text(encoding="utf-8") == "updated\n"


def test_install_adopts_symlink_target_and_topology_then_refreshes_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, live = _setup(tmp_path, monkeypatch)
    target = live.parent / "owned-target.md"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "    dst: ~/.config/example/note.md\n",
            f"    dst: ~/.config/example/note.md\n    symlink: {target}\n",
        ),
        encoding="utf-8",
    )
    target.parent.mkdir(parents=True)
    target.write_text("external\n", encoding="utf-8")
    old_target = live.parent / "old-target.md"
    live.symlink_to(old_target)

    result = _install(config, yes=True)

    assert result.exit_code == 0, result.output
    assert live.is_symlink()
    assert live.readlink() == target
    assert target.read_text(encoding="utf-8") == "shared\n"
    store = OwnershipStore()
    for path in (live, target):
        claim = store.read(file_resource_id(path))
        assert claim is not None
        assert (
            claim.fingerprint
            == observe_file(path, allow_topology=path == live).fingerprint
        )


def test_revert_install_restores_unowned_file_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, live = _setup(tmp_path, monkeypatch)
    assert _install(config, yes=True).exit_code == 0
    resource_id = file_resource_id(live)
    assert OwnershipStore().read(resource_id) is not None

    reverted = CliRunner().invoke(
        app, ["revert", "--profile=p", f"--config={config}", "--yes"]
    )

    assert reverted.exit_code == 0, reverted.output
    assert OwnershipStore().read(resource_id) is None
    assert not live.exists()


def test_install_refuses_to_recreate_missing_file_with_local_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from setforge.reconcile import hunks, store
    from setforge.reconcile.types import HunkClass, file_id

    config, live = _setup(tmp_path, monkeypatch)
    assert _install(config, yes=True).exit_code == 0
    base = live.read_bytes()
    changed = base + b"host only\n"
    live.write_bytes(changed)
    units = [
        replace(unit, cls=HunkClass.LOCAL)
        for unit in hunks.extract_hunks(base, changed)
    ]
    store.record(
        "p",
        file_id("note"),
        base=base,
        local=changed,
        hunks=hunks.serialize(units),
        staged=True,
    )
    live.unlink()

    result = _install(config, yes=True)

    assert result.exit_code != 0
    assert "ownership blocks install" in str(result.exception)
    assert not live.exists()


def test_install_adopts_tree_then_manages_owned_entries_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _live_file = _setup(tmp_path, monkeypatch)
    source = config.parent / "tracked" / "tools"
    source.mkdir()
    (source / "managed.txt").write_text("tracked\n", encoding="utf-8")
    live = tmp_path / "home" / ".tools"
    live.mkdir()
    (live / "managed.txt").write_text("external\n", encoding="utf-8")
    (live / "unowned.txt").write_text("keep\n", encoding="utf-8")
    config.write_text(
        "schema_version: '6.2'\n"
        "minimum_version: '6.2'\n"
        "tracked_files:\n"
        "  tools:\n"
        "    src: tools\n"
        "    dst: ~/.tools\n"
        "    tree:\n"
        "      orphans: remove-owned\n"
        "profiles:\n"
        "  p:\n"
        "    tracked_files: [tools]\n",
        encoding="utf-8",
    )

    adopted = _install(config, yes=True)

    assert adopted.exit_code == 0, adopted.output
    assert (live / "managed.txt").read_text(encoding="utf-8") == "external\n"
    assert (live / "unowned.txt").read_text(encoding="utf-8") == "keep\n"
    prior = read_inventory("p", "tools")
    assert prior is not None
    claim = OwnershipStore().read(file_resource_id(live))
    assert claim is not None
    assert claim.fingerprint == observe_tree(live, prior.fingerprint).fingerprint

    (source / "managed.txt").write_text("updated\n", encoding="utf-8")
    managed = _install(config, yes=True)

    assert managed.exit_code == 0, managed.output
    assert (live / "managed.txt").read_text(encoding="utf-8") == "updated\n"
    assert not (live / "unowned.txt").exists()

    reverted = CliRunner().invoke(
        app, ["revert", "--profile=p", f"--config={config}", "--yes"]
    )
    assert reverted.exit_code == 0, reverted.output
    assert (live / "managed.txt").read_text(encoding="utf-8") == "external\n"
    assert (live / "unowned.txt").read_text(encoding="utf-8") == "keep\n"


def test_install_transfers_tree_root_without_rewriting_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_a, _live_file = _setup(tmp_path, monkeypatch)
    source_a = config_a.parent / "tracked" / "tools"
    source_a.mkdir()
    (source_a / "managed.txt").write_text("tracked\n", encoding="utf-8")
    live = tmp_path / "home" / ".tools"
    live.mkdir()
    (live / "managed.txt").write_text("external\n", encoding="utf-8")
    config_a.write_text(
        "schema_version: '6.2'\n"
        "minimum_version: '6.2'\n"
        "tracked_files:\n"
        "  tools:\n"
        "    src: tools\n"
        "    dst: ~/.tools\n"
        "    tree: {}\n"
        "profiles:\n"
        "  p:\n"
        "    tracked_files: [tools]\n",
        encoding="utf-8",
    )
    assert _install(config_a, yes=True).exit_code == 0
    store = OwnershipStore()
    before = store.read(file_resource_id(live))
    assert before is not None
    live_before = (live / "managed.txt").read_bytes()

    repo_b = tmp_path / "repo-b"
    (repo_b / "tracked" / "tools").mkdir(parents=True)
    (repo_b / "tracked" / "tools" / "managed.txt").write_text(
        "tracked\n", encoding="utf-8"
    )
    config_b = repo_b / "setforge.yaml"
    config_b.write_bytes(config_a.read_bytes())
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_b, check=True)

    transferred = _install(config_b, yes=True)

    assert transferred.exit_code == 0, transferred.output
    assert "transferred tracked file ownership" in transferred.output
    after = store.read(file_resource_id(live))
    assert after is not None
    assert after.owner_id == read_owner_id(repo_b)
    assert after.generation == before.generation + 1
    assert (live / "managed.txt").read_bytes() == live_before


def test_managed_tree_never_promotes_new_unowned_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _live_file = _setup(tmp_path, monkeypatch)
    source = config.parent / "tracked" / "tools"
    source.mkdir()
    live = tmp_path / "home" / ".tools"
    config.write_text(
        "schema_version: '6.2'\n"
        "minimum_version: '6.2'\n"
        "tracked_files:\n"
        "  tools:\n"
        "    src: tools\n"
        "    dst: ~/.tools\n"
        "    tree:\n"
        "      orphans: remove-owned\n"
        "profiles:\n"
        "  p:\n"
        "    tracked_files: [tools]\n",
        encoding="utf-8",
    )
    assert _install(config, yes=True).exit_code == 0
    (live / "host-added.txt").write_text("host\n", encoding="utf-8")

    assert _install(config, yes=True).exit_code == 0
    assert _install(config, yes=True).exit_code == 0

    assert (live / "host-added.txt").read_text(encoding="utf-8") == "host\n"
    prior = read_inventory("p", "tools")
    assert prior is not None
    assert prior.owned_paths == ()


def test_tree_revert_removes_every_created_root_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _live_file = _setup(tmp_path, monkeypatch)
    source = config.parent / "tracked" / "tools"
    source.mkdir()
    (source / "managed.txt").write_text("tracked\n", encoding="utf-8")
    created_root = tmp_path / "home" / ".new-tree-root"
    config.write_text(
        "schema_version: '6.2'\n"
        "minimum_version: '6.2'\n"
        "tracked_files:\n"
        "  tools:\n"
        "    src: tools\n"
        "    dst: ~/.new-tree-root/one/two/tools\n"
        "    tree: {}\n"
        "profiles:\n"
        "  p:\n"
        "    tracked_files: [tools]\n",
        encoding="utf-8",
    )

    assert _install(config, yes=True).exit_code == 0
    assert created_root.joinpath("one/two/tools/managed.txt").exists()

    reverted = CliRunner().invoke(
        app, ["revert", "--profile=p", f"--config={config}", "--yes"]
    )

    assert reverted.exit_code == 0, reverted.output
    assert not created_root.exists()


def test_install_missing_tree_source_refuses_without_removing_owned_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _live_file = _setup(tmp_path, monkeypatch)
    source = config.parent / "tracked" / "tools"
    source.mkdir()
    (source / "managed.txt").write_text("tracked\n", encoding="utf-8")
    live = tmp_path / "home" / ".tools"
    config.write_text(
        "schema_version: '6.2'\n"
        "minimum_version: '6.2'\n"
        "tracked_files:\n"
        "  tools:\n"
        "    src: tools\n"
        "    dst: ~/.tools\n"
        "    tree:\n"
        "      orphans: remove-owned\n"
        "profiles:\n"
        "  p:\n"
        "    tracked_files: [tools]\n",
        encoding="utf-8",
    )
    assert _install(config, yes=True).exit_code == 0
    before = live.joinpath("managed.txt").read_bytes()
    prior = read_inventory("p", "tools")
    source.rename(source.with_name("tools-away"))

    result = _install(config, yes=True)

    assert result.exit_code != 0
    assert "managed tree source is missing" in str(result.exception)
    assert live.joinpath("managed.txt").read_bytes() == before
    assert read_inventory("p", "tools") == prior
