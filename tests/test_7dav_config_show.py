"""Unit tests for ``setforge config show`` (setforge-7dav).

Covers the read-only verbs: full-file rendering for ``--local`` and
``--tracked`` scope; dotted-path slicing; mutex enforcement on the
scope flags. Schema-introspection paths are exercised via dispatch.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import app


@pytest.fixture
def runner() -> CliRunner:
    """Mix-stderr-free runner so help-output substring asserts stay clean."""
    return CliRunner()


@pytest.fixture
def seed_local_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Drop a minimal local.yaml under the per-test isolated home."""
    home = Path.home()
    local_dir = home / ".config" / "setforge"
    local_dir.mkdir(parents=True, exist_ok=True)
    local_path = local_dir / "local.yaml"
    local_path.write_text(
        "# host-local overlays\nsource:\n  kind: path\n  path: /opt/cfg\n",
        encoding="utf-8",
    )
    # Re-redirect both LOCAL_CONFIG_PATH constants to this seeded file.
    monkeypatch.setattr("setforge.binaries.LOCAL_CONFIG_PATH", local_path)
    monkeypatch.setattr("setforge.source.LOCAL_CONFIG_PATH", local_path)
    monkeypatch.setattr("setforge.cli.config.LOCAL_CONFIG_PATH", local_path)
    return local_path


def test_show_requires_a_scope_flag(runner: CliRunner) -> None:
    """``config show`` without any scope flag raises typer.BadParameter."""
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code != 0
    combined = (result.stdout or "") + (result.stderr or "")
    assert "exactly one of --local" in combined or "Usage:" in combined


def test_show_rejects_both_local_and_tracked(runner: CliRunner) -> None:
    """``--local`` + ``--tracked`` is mutually exclusive — exit non-zero."""
    result = runner.invoke(app, ["config", "show", "--local", "--tracked"])
    assert result.exit_code != 0
    combined = (result.stdout or "") + (result.stderr or "")
    assert "mutually exclusive" in combined or "Usage:" in combined


def test_show_local_renders_full_file(runner: CliRunner, seed_local_yaml: Path) -> None:
    """``--local`` with no path prints the file's content (round-trip)."""
    result = runner.invoke(app, ["config", "show", "--local"])
    assert result.exit_code == 0, result.stdout
    assert "kind: path" in result.stdout
    assert "/opt/cfg" in result.stdout


def test_show_local_with_dotted_path_slices(
    runner: CliRunner, seed_local_yaml: Path
) -> None:
    """``config show --local source.kind`` returns just the scalar."""
    result = runner.invoke(app, ["config", "show", "--local", "source.kind"])
    assert result.exit_code == 0, result.stdout
    assert "path" in result.stdout


def test_show_local_with_unknown_path_errors(
    runner: CliRunner, seed_local_yaml: Path
) -> None:
    """An unknown dotted-path slice surfaces a setforge error, non-zero exit."""
    result = runner.invoke(app, ["config", "show", "--local", "no.such.field"])
    assert result.exit_code != 0


def test_show_local_empty_file_is_ok(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absent / empty local.yaml renders as empty without crashing."""
    # Default tmp_path / local.yaml is missing — conftest seeds the
    # path but the file is empty unless created.
    result = runner.invoke(app, ["config", "show", "--local"])
    assert result.exit_code == 0, result.stdout
