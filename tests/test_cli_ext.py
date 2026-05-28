"""CLI-level tests for the ``ext`` command group (setforge-ec2o.35 / .52).

Drives the real CLI via Typer's :class:`CliRunner`. Covers source-layer
resolution (every command must consult the source layer before
``load_config``) and clean error handling for extension install failures.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import app


def test_ext_list_resolves_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """ext list must call _resolve_config_arg before load_config so a
    configured source layer works outside the config-repo root
    (setforge-ec2o.35)."""
    import setforge.cli.ext as ext_mod

    seen: list[Path] = []

    def fake_resolve(config: Path) -> Path:
        seen.append(config)
        raise SystemExit(99)  # short-circuit before load_config

    monkeypatch.setattr(ext_mod, "_resolve_config_arg", fake_resolve)
    CliRunner().invoke(app, ["ext", "list", "--profile=x"])
    assert seen == [Path("setforge.yaml")]
