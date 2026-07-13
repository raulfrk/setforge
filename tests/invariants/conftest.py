"""Fixtures for the real invariant machines (task E2).

Provides ``isolated_model_root`` — a per-test isolation root whose
``SETFORGE_STATE_DIR`` is redirected under ``tmp_path`` via ``monkeypatch`` (so
it restores automatically, never leaking to the dev-host state dir). The
Hypothesis-driven machine sets its own per-example env in setup; these fixtures
serve the hand-driven property + positive-control tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def isolated_model_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh root with ``SETFORGE_STATE_DIR`` pinned under it (auto-restored)."""
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    return tmp_path
