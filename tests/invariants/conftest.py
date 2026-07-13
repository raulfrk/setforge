"""Fixtures for the real invariant machines (task E2)."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def isolated_model_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    return tmp_path
