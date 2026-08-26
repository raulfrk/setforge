from pathlib import Path

from typer.testing import CliRunner

from setforge.cli import app


def _write_config(tmp_path: Path, project_profiles: str) -> Path:
    config = tmp_path / "setforge.yaml"
    config.write_text(
        "tracked_files: {}\nprofiles: {}\nproject_profiles:\n" + project_profiles
    )
    return config


def test_validate_checks_project_profile_sources(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path,
        "  docs:\n"
        "    files:\n"
        "      guide:\n"
        "        src: missing.md\n"
        "        dst: docs/guide.md\n",
    )

    result = CliRunner().invoke(app, ["validate", "--all", f"--config={config}"])

    assert result.exit_code == 1, result.output
    assert "project profile 'docs'" in result.output
    assert "missing.md" in result.output
    assert "does not exist" in result.output


def test_validate_accepts_project_profile_source(tmp_path: Path) -> None:
    source = tmp_path / "project" / "docs" / "guide.md"
    source.parent.mkdir(parents=True)
    source.write_text("guide")
    config = _write_config(
        tmp_path,
        "  docs:\n"
        "    files:\n"
        "      guide:\n"
        "        src: guide.md\n"
        "        dst: docs/guide.md\n",
    )

    result = CliRunner().invoke(app, ["validate", "--all", f"--config={config}"])

    assert result.exit_code == 0, result.output
    assert "ok" in result.output


def test_validate_reports_symlink_loop_without_traceback(tmp_path: Path) -> None:
    source = tmp_path / "project" / "docs" / "loop"
    source.parent.mkdir(parents=True)
    source.symlink_to("loop")
    config = _write_config(
        tmp_path,
        "  docs:\n    files:\n      loop:\n        src: loop\n        dst: docs/loop\n",
    )

    result = CliRunner().invoke(app, ["validate", "--all", f"--config={config}"])

    assert result.exit_code == 1, result.output
    assert "source cannot be resolved" in result.output
    assert "Traceback" not in result.output
