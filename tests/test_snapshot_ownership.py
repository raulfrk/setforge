"""Snapshot recovery must respect current cross-checkout file authority."""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest
from ruamel.yaml import YAML
from typer.testing import CliRunner

from setforge.cli import app
from setforge.errors import OwnershipError
from setforge.file_ownership import file_resource_id, observe_file, observe_tree
from setforge.locking import install_resources_lock
from setforge.ownership import (
    OwnershipStore,
    ProvenanceFact,
    ProvenanceFactKind,
    load_or_create_owner_id,
)


@pytest.mark.parametrize("claimed_tree", [False, True], ids=["file", "tree"])
@pytest.mark.parametrize("foreign", [False, True], ids=["own", "foreign"])
@pytest.mark.parametrize("missing", [False, True], ids=["present", "absent"])
def test_snapshot_restore_respects_current_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    claimed_tree: bool,
    foreign: bool,
    missing: bool,
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    repo = tmp_path / "repo"
    (repo / "tracked").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    owner = load_or_create_owner_id(repo)
    live = Path.home() / "managed"
    live.mkdir()
    first, second = live / "a.txt", live / "z.txt"
    for path in (first, second):
        path.write_text("snapshot bytes\n")
        (repo / "tracked" / path.name).write_text("tracked bytes\n")
    config = repo / "setforge.yaml"
    YAML().dump(
        {
            "schema_version": "6.5",
            "minimum_version": "6.4",
            "tracked_files": {
                "first": {"src": "a.txt", "dst": str(first)},
                "second": {"src": "z.txt", "dst": str(second)},
            },
            "profiles": {"p": {"tracked_files": ["first", "second"]}},
        },
        config,
    )
    runner = CliRunner()
    args = ["--profile=p", f"--config={config}"]
    created = runner.invoke(app, ["snapshot", "create", "saved", *args])
    assert created.exit_code == 0, created.output
    for path in (first, second):
        path.write_text("current bytes\n")
    claimed = live if claimed_tree else second
    fingerprint = (
        observe_tree(claimed, "fixture-inventory").fingerprint
        if claimed_tree
        else observe_file(claimed).fingerprint
    )
    store = OwnershipStore()
    with install_resources_lock():
        claim = store.claim_locked(
            resource_id=file_resource_id(claimed),
            owner_id=uuid.uuid4() if foreign else owner,
            declaration_refs=("tracked_files.fixture",),
            provenance=(ProvenanceFact(ProvenanceFactKind.ORIGIN, "fixture"),),
            locator=str(claimed),
            fingerprint=fingerprint,
            expected_generation=None,
        )
    if missing:
        first.unlink()
        second.unlink()

    restored = runner.invoke(app, ["snapshot", "restore", "saved", *args, "--yes"])
    error = restored.exception

    assert store.read(claim.resource_id) == claim
    if foreign:
        assert isinstance(error, OwnershipError)
        assert "claim" in str(error).lower()
        if missing:
            assert not first.exists()
            assert not second.exists()
        else:
            assert first.read_text() == second.read_text() == "current bytes\n"
    else:
        assert error is None
        assert first.read_text() == second.read_text() == "snapshot bytes\n"
