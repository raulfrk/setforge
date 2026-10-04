"""Validate-before-write contract for ``setforge config add / remove``.

Anti-smell #7 (SPEC 4): when the candidate doc fails schema validation,
the original file MUST be left byte-identical on disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import app
from setforge.config import load_config


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


def test_cross_reference_invalid_candidate_leaves_file_untouched(
    runner: CliRunner, seed_tracked: Path
) -> None:
    """Removing a still-referenced tracked file refuses before the write."""
    original = seed_tracked.read_bytes()

    result = runner.invoke(
        app,
        ["config", "remove", "--tracked", "tracked_files.foo", "--yes"],
    )

    assert result.exit_code != 0
    message = str(result.exception)
    assert "profiles" in message
    assert "tracked_files" in message
    assert "foo" in message
    assert seed_tracked.read_bytes() == original


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        ("version", "2", "file-format version"),
        ("schema_version", "invalid", "malformed schema_version"),
        ("schema_version", "7.0", "requires a newer setforge"),
        ("minimum_version", "7.0", "requires a newer setforge"),
    ],
)
def test_incompatible_version_mutation_leaves_file_untouched(
    runner: CliRunner, seed_tracked: Path, path: str, value: str, message: str
) -> None:
    original = seed_tracked.read_bytes()
    load_config(seed_tracked)

    result = runner.invoke(app, ["config", "add", "--tracked", path, value, "--yes"])

    assert result.exit_code != 0
    assert message in str(result.exception)
    assert seed_tracked.read_bytes() == original


def test_supported_file_version_mutation_remains_loadable(
    runner: CliRunner, seed_tracked: Path
) -> None:
    result = runner.invoke(app, ["config", "add", "--tracked", "version", "1", "--yes"])

    assert result.exit_code == 0, result.output
    assert load_config(seed_tracked).version == 1


@pytest.mark.parametrize(
    "mutation",
    [
        ["remove", "--tracked", "minimum_version"],
        ["add", "--tracked", "minimum_version", "6.0"],
        ["add", "--tracked", "schema_version", "6.0"],
    ],
)
def test_generated_reader_floor_mutation_leaves_file_untouched(
    runner: CliRunner, seed_tracked: Path, mutation: list[str]
) -> None:
    seed_tracked.write_text(
        "schema_version: '6.1'\n"
        "minimum_version: '6.1'\n"
        "tracked_files:\n"
        "  foo:\n"
        "    src: foo.j2\n"
        "    dst: ~/foo\n"
        "    generated: {inputs: {home: home}}\n"
        "profiles: {base: {tracked_files: [foo]}}\n",
        encoding="utf-8",
    )
    original = seed_tracked.read_bytes()
    load_config(seed_tracked)

    result = runner.invoke(app, ["config", *mutation, "--yes"])

    assert result.exit_code != 0
    assert "generated tracked files require" in str(result.exception)
    assert seed_tracked.read_bytes() == original
