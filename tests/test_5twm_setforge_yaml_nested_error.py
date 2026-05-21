"""Tests for nested-error tmln routing on setforge.yaml (setforge-5twm).

Covers the nested ``loc`` paths emitted by Pydantic for
``extra_forbidden`` inside ``profiles.<name>`` and
``tracked_files.<id>``. The candidate list for close-match suggestions
must come from the nested model's ``model_fields`` (Profile /
TrackedFile), NOT the top-level :attr:`Config.model_fields`.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from setforge.cli import app


def _write_tracked_src(tmp_path: Path) -> None:
    (tmp_path / "tracked").mkdir(exist_ok=True)
    (tmp_path / "tracked" / "tracked_file.txt").write_text("x\n", encoding="utf-8")


def test_validate_setforge_yaml_profile_nested_typo_routes_to_tmln(
    tmp_path: Path,
) -> None:
    """A typo'd profile-level key (``extends_to``) routes through tmln.

    The error surfaces as a schema-error carrier with file:line + snippet
    + Fix; close-match against :attr:`Profile.model_fields.keys()` is
    attempted (``extends`` is dist 3, beyond the gate, so no suggestion
    fires — but the structured carrier is the load-bearing check).
    """
    _write_tracked_src(tmp_path)
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  d:\n"
        "    src: tracked_file.txt\n"
        "    dst: ~/.some-tracked_file\n"
        "profiles:\n"
        "  p:\n"
        "    tracked_files: [d]\n"
        "    extendz: q\n",  # typo of 'extends', distance 1
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["validate", "--all", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    assert "✗ SCHEMA VALIDATION ERROR" in result.output
    assert "←─── line" in result.output
    assert "Fix:" in result.output
    # The close-match candidates come from Profile.model_fields, NOT Config.
    # 'extendz' is distance 1 from 'extends' → suggestion fires.
    assert "Did you mean 'extends'" in result.output


def test_validate_setforge_yaml_tracked_files_nested_typo_routes_to_tmln(
    tmp_path: Path,
) -> None:
    """A typo'd tracked_files-entry key (``srcc``) routes through tmln.

    Candidate list = :attr:`TrackedFile.model_fields.keys()`; ``srcc`` is
    distance 1 from ``src`` so the suggestion fires.
    """
    _write_tracked_src(tmp_path)
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  d:\n"
        "    srcc: tracked_file.txt\n"  # typo of 'src'
        "    dst: ~/.some-tracked_file\n"
        "profiles:\n"
        "  p:\n"
        "    tracked_files: [d]\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["validate", "--all", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    assert "✗ SCHEMA VALIDATION ERROR" in result.output
    # Two errors fire here: missing 'src' (other type) + extra 'srcc'.
    # The 'srcc' error must surface a close-match suggestion against
    # TrackedFile.model_fields, NOT Config.model_fields.
    assert "Did you mean 'src'" in result.output


def test_validate_setforge_yaml_multiple_errors_all_reported(tmp_path: Path) -> None:
    """All schema errors are collected and reported together (no abort
    on first); final summary names the count."""
    _write_tracked_src(tmp_path)
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  d:\n"
        "    src: tracked_file.txt\n"
        "    dst: ~/.some-tracked_file\n"
        "proffiles:\n"  # extra_forbidden
        "  p:\n"
        "    tracked_files: [d]\n"
        "another_unknown: 1\n",  # second extra_forbidden
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["validate", "--all", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    # Two distinct schema errors land in the output.
    assert result.output.count("✗ SCHEMA VALIDATION ERROR") >= 2
    assert "validation FAILED:" in result.output
