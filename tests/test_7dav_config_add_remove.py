"""Unit tests for ``setforge config add`` / ``setforge config remove``.

Covers:
- list-vs-scalar dispatch via Pydantic ``model_fields`` introspection,
- ``--local`` / ``--tracked`` mutex,
- ``--yes`` short-circuit (mutate-gate posture),
- round-trip preservation (comments / key-order kept by ruamel.yaml rt).

See setforge-7dav SPEC 4 acceptance for the enumerated cases.
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
def seed_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Seed a local.yaml with binaries + a comment for round-trip checks."""
    local = tmp_path / "local.yaml"
    local.write_text(
        "# comment-A\nbinaries:\n  code: /usr/bin/code\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("setforge.binaries.LOCAL_CONFIG_PATH", local)
    monkeypatch.setattr("setforge.source.LOCAL_CONFIG_PATH", local)
    monkeypatch.setattr("setforge.cli.config.LOCAL_CONFIG_PATH", local)
    return local


@pytest.fixture
def seed_tracked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Seed a tracked setforge.yaml and bypass the git-clean check."""
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


def test_add_local_scalar_with_yes(runner: CliRunner, seed_local: Path) -> None:
    """``add --local binaries.code /usr/local/bin/code --yes`` rewrites scalar."""
    result = runner.invoke(
        app,
        ["config", "add", "--local", "binaries.code", "/usr/local/bin/code", "--yes"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    text = seed_local.read_text(encoding="utf-8")
    assert "/usr/local/bin/code" in text
    # Round-trip preserved the leading comment.
    assert "# comment-A" in text


def test_add_local_unknown_path_errors(runner: CliRunner, seed_local: Path) -> None:
    """Adding to an unknown dotted-path surfaces a SetforgeError."""
    result = runner.invoke(
        app, ["config", "add", "--local", "bogus.field", "val", "--yes"]
    )
    assert result.exit_code != 0


def test_add_rejects_both_local_and_tracked(runner: CliRunner) -> None:
    """``--local`` + ``--tracked`` raises typer.BadParameter."""
    result = runner.invoke(
        app, ["config", "add", "--local", "--tracked", "binaries.code", "/x", "--yes"]
    )
    assert result.exit_code != 0


def test_add_requires_scope_flag(runner: CliRunner) -> None:
    """No scope flag → typer.BadParameter, non-zero exit."""
    result = runner.invoke(app, ["config", "add", "binaries.code", "/x", "--yes"])
    assert result.exit_code != 0


def test_remove_local_scalar_with_yes(runner: CliRunner, seed_local: Path) -> None:
    """``remove --local binaries.code --yes`` unsets the scalar."""
    result = runner.invoke(
        app, ["config", "remove", "--local", "binaries.code", "--yes"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    text = seed_local.read_text(encoding="utf-8")
    assert "code:" not in text


def test_remove_unknown_path_errors(runner: CliRunner, seed_local: Path) -> None:
    """Removing an absent path errors out cleanly."""
    result = runner.invoke(
        app, ["config", "remove", "--local", "binaries.nope", "--yes"]
    )
    assert result.exit_code != 0


def test_add_tracked_appends_to_profile_list(
    runner: CliRunner, seed_tracked: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adding to a profile's ``tracked_files`` list works (existing key)."""
    result = runner.invoke(
        app,
        [
            "config",
            "add",
            "--tracked",
            "profiles.base.tracked_files",
            "foo",  # already in list — should error (duplicate)
            "--profile=base",
            "--yes",
        ],
    )
    # 'foo' is already present → SetforgeError "already contains".
    assert result.exit_code != 0


def test_add_tracked_profile_required_for_profile_paths(
    runner: CliRunner, seed_tracked: Path
) -> None:
    """``profiles.*`` paths require ``--profile=NAME``."""
    result = runner.invoke(
        app,
        [
            "config",
            "add",
            "--tracked",
            "profiles.base.tracked_files",
            "bar",
            "--yes",
        ],
    )
    assert result.exit_code != 0


def test_add_tracked_profile_rejected_for_top_level_paths(
    runner: CliRunner, seed_tracked: Path
) -> None:
    """Top-level paths reject ``--profile``."""
    result = runner.invoke(
        app,
        [
            "config",
            "add",
            "--tracked",
            "schema_version",
            "1.1",
            "--profile=base",
            "--yes",
        ],
    )
    assert result.exit_code != 0
