"""Round-trip preservation tests for ``setforge config`` mutations.

Anti-smells #1, #2, #15: ruamel.yaml round-trip mode preserves
comments, key insertion order, and quoting. The whole config CLI is
useless if a single mutation reformats the file.
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
def seed_local_with_comments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Seed a local.yaml whose comments + key order MUST survive a mutation."""
    local = tmp_path / "local.yaml"
    local.write_text(
        "# top-level comment\n"
        "source:\n"
        "  # source kind\n"
        "  kind: path\n"
        "  path: /opt/cfg  # inline\n"
        "binaries:\n"
        "  code: /usr/bin/code\n"
        "  # patch tracker (TBD)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("setforge.binaries.LOCAL_CONFIG_PATH", local)
    monkeypatch.setattr("setforge.source.LOCAL_CONFIG_PATH", local)
    monkeypatch.setattr("setforge.cli.config.LOCAL_CONFIG_PATH", local)
    return local


def test_round_trip_preserves_comments(
    runner: CliRunner, seed_local_with_comments: Path
) -> None:
    """A scalar mutation preserves every comment in the file."""
    result = runner.invoke(
        app,
        ["config", "add", "--local", "binaries.code", "/usr/local/bin/code", "--yes"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    text = seed_local_with_comments.read_text(encoding="utf-8")
    assert "# top-level comment" in text
    assert "# source kind" in text
    assert "# inline" in text
    assert "# patch tracker (TBD)" in text


def test_round_trip_preserves_key_order(
    runner: CliRunner, seed_local_with_comments: Path
) -> None:
    """The pre-mutation key insertion order is preserved post-mutation."""
    result = runner.invoke(
        app, ["config", "add", "--local", "binaries.code", "/opt/code", "--yes"]
    )
    assert result.exit_code == 0
    text = seed_local_with_comments.read_text(encoding="utf-8")
    # `source:` still appears BEFORE `binaries:` (insertion order).
    src_idx = text.index("source:")
    bin_idx = text.index("binaries:")
    assert src_idx < bin_idx


def test_round_trip_atomic_write_no_partial_file(
    runner: CliRunner, seed_local_with_comments: Path, tmp_path: Path
) -> None:
    """``atomic_write_yaml`` leaves no ``.tmp`` sibling on success."""
    result = runner.invoke(
        app, ["config", "add", "--local", "binaries.code", "/opt/code", "--yes"]
    )
    assert result.exit_code == 0
    # No leftover .tmp file from atomic_write_yaml's mkstemp.
    siblings = [p.name for p in seed_local_with_comments.parent.iterdir()]
    assert not any(name.startswith(".local.yaml.") for name in siblings), siblings
