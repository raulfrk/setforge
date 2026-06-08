"""Cross-major refuse-clean regression guard for the 2.0 contract.

A config whose major exceeds this engine's must refuse cleanly — a non-zero
exit with the "upgrade setforge" message and NO Python traceback. After the
breaking 1.x -> 2.0 bump the engine is major 2, so a major-3 config exercises
the same guard a 1.x engine would hit reading a 2.0 config. This pins that the
major bump did not regress the clean-refusal contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import app
from setforge.config import load_config
from setforge.errors import ConfigError

_FUTURE_MAJOR = (
    'version: 1\nschema_version: "3.0"\ntracked_files: {}\nprofiles:\n  p: {}\n'
)


def _write(tmp_path: Path) -> Path:
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(_FUTURE_MAJOR, encoding="utf-8")
    return cfg


def test_load_config_refuses_future_major_clean(tmp_path: Path) -> None:
    """load_config raises a clean ConfigError naming the upgrade path."""
    with pytest.raises(ConfigError, match="upgrade setforge") as exc:
        load_config(_write(tmp_path))
    assert "3.0" in str(exc.value)


def test_cli_validate_future_major_nonzero_no_traceback(tmp_path: Path) -> None:
    """`validate` on a future-major config exits non-zero with no traceback."""
    cfg = _write(tmp_path)
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code != 0, result.output
    # Clean refusal: the domain error is surfaced, not a raw Python traceback.
    assert "Traceback (most recent call last)" not in result.output
    assert "upgrade setforge" in result.output
