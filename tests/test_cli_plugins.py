"""CLI-level tests for the ``plugin`` / ``marketplace`` command groups
(setforge-ec2o.35 / .51).

Drives the real CLI via Typer's :class:`CliRunner`. Covers source-layer
resolution and clean error handling for failing ``claude`` subprocesses.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import app


@pytest.mark.parametrize(
    "argv",
    [
        ["plugin", "list", "--profile=x"],
        ["marketplace", "add", "mp", "--from=github:o/r"],
    ],
)
def test_plugin_marketplace_resolves_config(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    """plugin/marketplace commands must call _resolve_config_arg before
    load_config so a configured source layer works outside the config-repo
    root (setforge-ec2o.35)."""
    import setforge.cli.plugins as plugins_mod

    seen: list[Path] = []

    def fake_resolve(config: Path) -> Path:
        seen.append(config)
        raise SystemExit(99)  # short-circuit before load_config

    monkeypatch.setattr(plugins_mod, "_resolve_config_arg", fake_resolve)
    CliRunner().invoke(app, argv)
    assert seen == [Path("setforge.yaml")]
