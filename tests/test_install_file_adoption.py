from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge.cli import app
from setforge.file_ownership import file_resource_id, observe_file, observe_tree
from setforge.ownership import Authority, OwnershipStore, read_owner_id
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
