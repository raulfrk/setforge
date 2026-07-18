"""``setforge validate`` sees bundle ``file`` components and enforces their gates."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge.cli import app

_PROFILE = "vbf"


@pytest.fixture(autouse=True)
def _sandbox_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        "setforge.cli.validate._LOCAL_CONFIG_PATH", tmp_path / "local.yaml"
    )


def _repo_with_launcher(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "tracked").mkdir(parents=True)
    (repo / "tracked" / "launch.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    return repo


def _write(repo: Path, body: str) -> Path:
    cfg = repo / "setforge.yaml"
    cfg.write_text(body, encoding="utf-8")
    return cfg


def _validate(cfg: Path) -> Result:
    return CliRunner().invoke(
        app, ["validate", f"--profile={_PROFILE}", f"--config={cfg}"]
    )


def _good_bundle_block(dst: str = "~/.claude/plugins/data/rd/launch.sh") -> str:
    return (
        "bundles:\n"
        "  revdiff:\n"
        "    components:\n"
        "      - id: launcher\n"
        "        file:\n"
        "          src: launch.sh\n"
        f"          dst: {dst}\n"
        "          mode: 0o755\n"
    )


def _profile_block() -> str:
    return f"profiles:\n  {_PROFILE}:\n    bundles:\n      - revdiff\n"


def test_validate_passes_valid_file_component(tmp_path: Path) -> None:
    repo = _repo_with_launcher(tmp_path)
    cfg = _write(
        repo,
        "version: 1\ntracked_files: {}\n" + _good_bundle_block() + _profile_block(),
    )
    result = _validate(cfg)
    assert result.exit_code == 0, result.output


def test_validate_rejects_name_collision(tmp_path: Path) -> None:
    repo = _repo_with_launcher(tmp_path)
    cfg = _write(
        repo,
        "version: 1\n"
        "tracked_files:\n"
        "  revdiff.launcher:\n"
        "    src: launch.sh\n"
        "    dst: ~/other\n"
        + _good_bundle_block()
        + f"profiles:\n  {_PROFILE}:\n    tracked_files:\n      - revdiff.launcher\n"
        "    bundles:\n      - revdiff\n",
    )
    result = _validate(cfg)
    assert result.exit_code != 0, result.output
    assert "revdiff.launcher" in result.output


def test_validate_rejects_dst_collision(tmp_path: Path) -> None:
    repo = _repo_with_launcher(tmp_path)
    cfg = _write(
        repo,
        "version: 1\ntracked_files: {}\n"
        "bundles:\n"
        "  revdiff:\n"
        "    components:\n"
        "      - id: one\n"
        "        file:\n"
        "          src: launch.sh\n"
        "          dst: ~/.claude/dup\n"
        "      - id: two\n"
        "        file:\n"
        "          src: launch.sh\n"
        "          dst: ~/.claude/dup\n" + _profile_block(),
    )
    result = _validate(cfg)
    assert result.exit_code != 0, result.output


def test_validate_warns_out_of_home_dst(tmp_path: Path) -> None:
    # An out-of-$HOME bundle dst now WARNS and validates (parity with the plain
    # tracked_files warn-on-out-of-$HOME behavior), rather than refusing.
    repo = _repo_with_launcher(tmp_path)
    cfg = _write(
        repo,
        "version: 1\ntracked_files: {}\n"
        + _good_bundle_block(dst="~/../etc/evil")
        + _profile_block(),
    )
    result = _validate(cfg)
    assert result.exit_code == 0, result.output
    assert "outside $HOME" in result.output


def test_validate_sees_synthetic_entry_and_lints_missing_src(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "tracked").mkdir(parents=True)
    cfg = _write(
        repo,
        "version: 1\ntracked_files: {}\n" + _good_bundle_block() + _profile_block(),
    )
    result = _validate(cfg)
    assert result.exit_code != 0, result.output
    assert "revdiff.launcher" in result.output
