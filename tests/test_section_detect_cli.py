"""Unit tests for ``setforge section detect`` (S4/S5) — the CLI orchestration
layer in :mod:`setforge.cli._detect_helpers`.

The pure detect engine (``compute_detect_regions`` / ``propose_anchor``) is
covered by :mod:`tests.test_section_detect`; these tests cover the config/IO
plumbing, KIND gating, anchor reconstruction, atomic carve writes, the wizard
loop, and S5 overlay re-capture.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from setforge.cli import app

_CFG = "tests/fixtures/e2e/setforge.test.yaml"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_detect_no_drift_exits_zero(runner: CliRunner) -> None:
    """When no live file drifts (here: the live dst does not exist in the unit
    sandbox), detect reports no changes and exits 0."""
    result = runner.invoke(
        app,
        [
            "section",
            "detect",
            f"--config={_CFG}",
            "--profile=test-text-sections",
            "--tracked-file=sections_md",
        ],
    )
    assert result.exit_code == 0, result.output
    out = result.output.lower()
    assert "no changes detected" in out
