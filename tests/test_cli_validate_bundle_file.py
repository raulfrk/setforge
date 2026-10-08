"""``setforge validate`` sees bundle ``file`` components and enforces their gates."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge.cli import app
from tests.shared_helpers import write_setforge_yaml

_PROFILE = "vbf"


@pytest.fixture(autouse=True)
def _sandbox_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


def _repo_with_launcher(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "tracked").mkdir(parents=True)
    (repo / "tracked" / "launch.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    return repo


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
    cfg = write_setforge_yaml(
        repo,
        "version: 1\ntracked_files: {}\n" + _good_bundle_block() + _profile_block(),
    )
    result = _validate(cfg)
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("override", ["dst: ~/host-launch.sh", "mode: 0o644"])
def test_validate_all_keeps_inherited_bundle_overlays_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, override: str
) -> None:
    repo = _repo_with_launcher(tmp_path)
    cfg = write_setforge_yaml(
        repo,
        "schema_version: '6.5'\ntracked_files: {}\n"
        + _good_bundle_block()
        + "profiles:\n  base: {bundles: [revdiff]}\n  child: {extends: base}\n",
    )
    local = tmp_path / "local.yaml"
    local.write_text(f"tracked_files:\n  revdiff.launcher:\n    {override}\n")
    original = (cfg.read_bytes(), local.read_bytes())
    runner = CliRunner()
    for profile in ("base", "child"):
        result = runner.invoke(
            app, ["validate", f"--profile={profile}", f"--config={cfg}"]
        )
        assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["validate", "--all", f"--config={cfg}"])

    assert result.exit_code == 0, result.output
    assert (cfg.read_bytes(), local.read_bytes()) == original


def test_validate_rejects_name_collision(tmp_path: Path) -> None:
    repo = _repo_with_launcher(tmp_path)
    cfg = write_setforge_yaml(
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
    cfg = write_setforge_yaml(
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
    cfg = write_setforge_yaml(
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
    cfg = write_setforge_yaml(
        repo,
        "version: 1\ntracked_files: {}\n" + _good_bundle_block() + _profile_block(),
    )
    result = _validate(cfg)
    assert result.exit_code != 0, result.output
    assert "revdiff.launcher" in result.output
