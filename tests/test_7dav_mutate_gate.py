"""Mutate-gate posture for ``setforge config`` (per feedback memory).

Non-TTY without ``--yes`` MUST raise :class:`ConfirmRequiresInteractive`,
not warn-and-skip. Mirrors ``confirm_auto_operation``
(_confirm.py:217-267).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import app
from setforge.errors import ConfirmRequiresInteractive


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def seed_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    local = tmp_path / "local.yaml"
    local.write_text("binaries:\n  code: /usr/bin/code\n", encoding="utf-8")
    monkeypatch.setattr("setforge.binaries.LOCAL_CONFIG_PATH", local)
    monkeypatch.setattr("setforge.source.LOCAL_CONFIG_PATH", local)
    monkeypatch.setattr("setforge.cli.config.LOCAL_CONFIG_PATH", local)
    return local


def test_non_tty_without_yes_raises(
    runner: CliRunner, seed_local: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-TTY stdin + no ``--yes`` MUST raise ConfirmRequiresInteractive."""
    # CliRunner's stdin is never a TTY — perfect for this contract.
    result = runner.invoke(app, ["config", "add", "--local", "binaries.code", "/x"])
    assert result.exit_code != 0
    # The wrapped error type must surface (top-level main() catches it).
    assert isinstance(result.exception, ConfirmRequiresInteractive) or (
        "requires --yes" in (result.stdout or "")
        or "requires --yes" in (result.stderr or "")
    )


def test_yes_flag_skips_confirm(runner: CliRunner, seed_local: Path) -> None:
    """``--yes`` short-circuits the confirm AND writes the change."""
    result = runner.invoke(
        app,
        ["config", "add", "--local", "binaries.code", "/usr/local/bin/code", "--yes"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "/usr/local/bin/code" in seed_local.read_text(encoding="utf-8")
