from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge.cli import app
from setforge.file_ownership import file_resource_id, observe_file
from setforge.ownership import Authority, OwnershipStore, read_owner_id


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
