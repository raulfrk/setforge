"""Validate-before-write contract for ``setforge config add / remove``.

Anti-smell #7 (SPEC 4): when the candidate doc fails schema validation,
the original file MUST be left byte-identical on disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def seed_tracked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Seed a minimal valid tracked setforge.yaml."""
    tracked = tmp_path / "tracked" / "setforge.yaml"
    tracked.parent.mkdir(parents=True, exist_ok=True)
    tracked.write_text(
        "version: 1\n"
        "schema_version: '1.0'\n"
        "tracked_files:\n"
        "  foo:\n"
        "    src: foo.md\n"
        "    dst: foo.md\n"
        "profiles:\n"
        "  base:\n"
        "    tracked_files:\n"
        "      - foo\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("setforge.cli.config._tracked_yaml_path", lambda: tracked)
    monkeypatch.setattr(
        "setforge.cli.config._run_tracked_git_check", lambda yaml_path: None
    )
    return tracked


def test_invalid_candidate_leaves_file_untouched(
    runner: CliRunner, seed_tracked: Path
) -> None:
    """A schema-invalid mutation MUST refuse without writing."""
    original = seed_tracked.read_text(encoding="utf-8")
    # Set version to a non-int — Pydantic refuses.
    result = runner.invoke(
        app, ["config", "add", "--tracked", "version", "not-an-int", "--yes"]
    )
    assert result.exit_code != 0
    # File unchanged byte-for-byte.
    assert seed_tracked.read_text(encoding="utf-8") == original


def test_valid_candidate_writes(runner: CliRunner, seed_tracked: Path) -> None:
    """A schema-valid mutation lands on disk."""
    result = runner.invoke(
        app, ["config", "add", "--tracked", "schema_version", "1.1", "--yes"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "1.1" in seed_tracked.read_text(encoding="utf-8")
