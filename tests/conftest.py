"""Shared pytest fixtures for the setforge test suite."""

from pathlib import Path

import pytest

# Re-export the ``fake_claude`` fixture defined in tests/test_claude_plugins.py
# so test_cli_e2e.py (setforge-181) can request it via parameter without
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
    """Redirect ``LOCAL_CONFIG_PATH`` constants to a tmp path for every test.

    Two modules carry the constant — ``binaries`` for the ``binaries:``
    block and ``source`` for the ``source:`` block — and both must be
    redirected so neither leaks to ``~/.config/setforge/local.yaml`` on
    the dev host. Also resets ``source._cli_source`` to None so a test
    that sets it directly via ``set_cli_source`` (without going through
    a ``CliRunner`` callback) doesn't leak the value to later tests.
    """
    monkeypatch.setattr(
        "setforge.binaries.LOCAL_CONFIG_PATH",
        tmp_path / "local.yaml",
    )
    monkeypatch.setattr(
        "setforge.source.LOCAL_CONFIG_PATH",
        tmp_path / "local.yaml",
    )
    monkeypatch.setattr("setforge.source._cli_source", None)
