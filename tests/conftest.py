"""Shared pytest fixtures for the my-setup test suite."""

from pathlib import Path

import pytest

# Re-export the ``fake_claude`` fixture defined in tests/test_claude_plugins.py
# so test_cli_e2e.py (tracked_files-181) can request it via parameter without
# tripping ruff F811 (which fires on direct imports of a fixture name that
# matches a test-parameter name). Placing the import here makes the fixture
# discoverable via pytest's normal conftest mechanism instead of a same-file
# rebinding. ``__all__`` silences ruff F401 without per-site noqa.
from tests.test_claude_plugins import fake_claude, fake_git

__all__ = ["fake_claude", "fake_git"]


@pytest.fixture(autouse=True)
def _isolated_local_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Redirect ``binaries.LOCAL_CONFIG_PATH`` to a tmp path for every test.

    ``binaries.ensure_local_config_stub()`` runs in the typer ``@app.callback()``
    on every ``CliRunner.invoke(app, ...)``. Without this fixture, every CLI
    test would write ``~/.config/my-setup/local.yaml`` on the dev host (or CI
    runner). Pure test hygiene; no production effect.
    """
    monkeypatch.setattr(
        "setforge.binaries.LOCAL_CONFIG_PATH",
        tmp_path / "local.yaml",
    )
