from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import app


def _write_config(path: Path, profile: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"schema_version: '6.5'\ntracked_files: {{}}\nprofiles:\n  {profile}: {{}}\n",
        encoding="utf-8",
    )


def test_config_help_preserves_visible_default() -> None:
    result = CliRunner().invoke(app, ["profile", "list", "--help"])

    assert result.exit_code == 0, result.output
    assert "Path to setforge.yaml. [default: setforge.yaml]" in result.output


@pytest.mark.parametrize(
    ("config_args", "cwd_has_config", "expected_profile"),
    [
        (["--config", "setforge.yaml"], True, "explicit_default"),
        (["--config", "chosen.yaml"], False, "explicit_non_default"),
        ([], False, "discovered"),
    ],
)
def test_config_argument_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_args: list[str],
    cwd_has_config: bool,
    expected_profile: str,
) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    discovered = tmp_path / "discovered"
    _write_config(discovered / "setforge.yaml", "discovered")
    monkeypatch.setenv("SETFORGE_SOURCE", str(discovered))
    monkeypatch.chdir(cwd)

    if cwd_has_config:
        _write_config(cwd / "setforge.yaml", "explicit_default")
    if "chosen.yaml" in config_args:
        _write_config(cwd / "chosen.yaml", "explicit_non_default")

    result = CliRunner().invoke(app, ["profile", "list", *config_args])

    assert result.exit_code == 0, result.output
    assert expected_profile in result.output
