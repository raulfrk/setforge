"""Shared pytest fixtures for the my-setup test suite."""

from pathlib import Path

import pytest


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
        "my_setup.binaries.LOCAL_CONFIG_PATH",
        tmp_path / "local.yaml",
    )
