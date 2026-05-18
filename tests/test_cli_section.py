"""Unit tests for :mod:`setforge.cli.section` — section add + emit."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from setforge.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# --- section emit ---


def test_section_emit_shared(runner: CliRunner) -> None:
    result = runner.invoke(app, ["section", "emit", "shared", "foo"])
    assert result.exit_code == 0
    assert result.stdout == (
        "<!-- setforge:user-section start shared foo -->\n"
        "\n"
        "<!-- setforge:user-section end shared foo -->\n"
    )


def test_section_emit_host_local(runner: CliRunner) -> None:
    result = runner.invoke(app, ["section", "emit", "host-local", "bar"])
    assert result.exit_code == 0
    assert "<!-- setforge:user-section start host-local bar -->" in result.stdout
    assert "<!-- setforge:user-section end host-local bar -->" in result.stdout


def test_section_emit_uses_setforge_namespace_not_legacy(runner: CliRunner) -> None:
    result = runner.invoke(app, ["section", "emit", "shared", "foo"])
    assert "setforge:user-section" in result.stdout
    assert "my-setup:user-section" not in result.stdout


@pytest.mark.parametrize("bad_name", ["Foo", "foo bar", "1foo", "foo!", "_foo"])
def test_section_emit_rejects_invalid_name(runner: CliRunner, bad_name: str) -> None:
    result = runner.invoke(app, ["section", "emit", "shared", bad_name])
    assert result.exit_code == 2


def test_section_emit_rejects_too_long_name(runner: CliRunner) -> None:
    too_long = "a" * 64
    result = runner.invoke(app, ["section", "emit", "shared", too_long])
    assert result.exit_code == 2


def test_section_emit_accepts_max_length_name(runner: CliRunner) -> None:
    max_len = "a" * 63
    result = runner.invoke(app, ["section", "emit", "shared", max_len])
    assert result.exit_code == 0


def test_section_emit_rejects_invalid_semantics(runner: CliRunner) -> None:
    result = runner.invoke(app, ["section", "emit", "weird", "foo"])
    assert result.exit_code == 2


def test_section_emit_rejects_empty_name(runner: CliRunner) -> None:
    result = runner.invoke(app, ["section", "emit", "shared", ""])
    assert result.exit_code == 2


def test_section_emit_no_extra_blanks(runner: CliRunner) -> None:
    result = runner.invoke(app, ["section", "emit", "shared", "foo"])
    assert result.stdout.count("\n") == 3
