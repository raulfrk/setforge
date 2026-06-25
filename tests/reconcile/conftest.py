"""Fixtures for the reconcile-store test suite."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the setforge state dir at an isolated tmp tree for the test."""
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    return tmp_path
