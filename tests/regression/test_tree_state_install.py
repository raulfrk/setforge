"""A tree root that contains SetForge's own state installs on the first run."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.regression.support import claim_ids_for
from tests.regression.test_ownership import _command_from, _tree_host


def test_first_install_of_a_root_holding_the_state_dir_needs_no_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _tree_host(tmp_path, monkeypatch, {"managed": "~/.managed"})
    state = host.real_home / ".managed" / ".sfstate"
    state.parent.mkdir()
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    host.state = state

    first = host.install()

    assert first.exit_code == 0, first.output


def test_ownership_revert_of_a_tree_holding_setforge_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _tree_host(tmp_path, monkeypatch, {"s": "~/.local/share"})
    snapshots = host.real_home / ".local" / "share" / "setforge" / "snapshots"
    snapshots.mkdir(parents=True)
    (snapshots / "x.txt").write_bytes(b"x")
    assert host.install().exit_code == 0
    listing = host.cli("ownership", "list", config=False, profile=False).output
    (claim,) = claim_ids_for(listing, "/.local/share")
    released = host.cli("ownership", "release", claim, "--yes", profile=False)
    assert released.exit_code == 0, released.output
    blocked = host.proc_install()
    assert blocked.returncode == 1

    back = host.cli(
        *_command_from(blocked.stderr, "revert"), config=False, profile=False
    )

    assert back.exit_code == 0, back.output
