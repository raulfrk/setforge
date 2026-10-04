"""A tree root that contains SetForge's own state installs on the first run."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.regression.test_ownership import _tree_host


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
