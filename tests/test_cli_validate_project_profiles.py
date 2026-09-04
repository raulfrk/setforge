from pathlib import Path

import pytest
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


def test_validate_rejects_bundle_component_with_unknown_package(
    tmp_path: Path,
) -> None:
    config = tmp_path / "setforge.yaml"
    config.write_text(
        "tracked_files: {}\n"
        "packages: {}\n"
        "bundles:\n"
        "  tools:\n"
        "    components:\n"
        "      - id: ripgrep\n"
        "        package: missing-package\n"
        "profiles:\n"
        "  base:\n"
        "    bundles: [tools]\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["validate", "--all", f"--config={config}"])

    assert result.exit_code == 1, result.output
    assert "bundle" in result.output
    assert "tools" in result.output
    assert "ripgrep" in result.output
    assert "missing-package" in result.output
    assert "Traceback" not in result.output


def test_validate_rejects_missing_tracked_file_reference_without_traceback(
    tmp_path: Path,
) -> None:
    config = tmp_path / "setforge.yaml"
    config.write_text(
        "tracked_files: {}\nprofiles:\n  base:\n    tracked_files: [missing-file]\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["validate", "--all", f"--config={config}"])

    assert result.exit_code == 1, result.output
    assert "profile" in result.output
    assert "base" in result.output
    assert "tracked_files" in result.output
    assert "missing-file" in result.output
    assert "Traceback" not in result.output


def test_validate_rejects_missing_selected_codex_instruction_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (tmp_path / "tracked").mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    config = tmp_path / "setforge.yaml"
    config.write_text(
        "schema_version: '6.5'\n"
        "minimum_version: '6.4'\n"
        "tracked_files: {}\n"
        "codex:\n"
        "  instructions:\n"
        "    base: {source: codex/missing-AGENTS.md}\n"
        "profiles:\n"
        "  default:\n"
        "    codex:\n"
        "      instructions: [base]\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app, ["validate", "--profile=default", f"--config={config}"]
    )

    assert result.exit_code == 1, result.output
    assert "profile 'default'" in result.output
    assert "Codex source does not exist" in result.output
    assert "missing-AGENTS.md" in result.output
    assert "Traceback" not in result.output


def test_validate_does_not_expose_synthetic_codex_ids_to_overlays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    source = tmp_path / "tracked" / "codex" / "AGENTS.md"
    source.parent.mkdir(parents=True)
    source.write_text("instructions\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    local = tmp_path / "local.yaml"
    local.write_text(
        "tracked_files:\n  codex.instruction.base:\n    mode: 0o600\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("setforge.cli.validate._LOCAL_CONFIG_PATH", local)
    config = tmp_path / "setforge.yaml"
    config.write_text(
        "schema_version: '6.5'\n"
        "minimum_version: '6.4'\n"
        "tracked_files: {}\n"
        "codex:\n"
        "  instructions:\n"
        "    base: {source: codex/AGENTS.md}\n"
        "profiles:\n"
        "  default:\n"
        "    codex:\n"
        "      instructions: [base]\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app, ["validate", "--profile=default", f"--config={config}"]
    )

    assert result.exit_code == 1, result.output
    assert "codex.instruction.base" in result.output
    assert "not declared in setforge.yaml" in result.output
