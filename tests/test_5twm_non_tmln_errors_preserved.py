"""Tests that non-tmln errors keep their existing routing.

The 5twm change ONLY routes Pydantic ``ValidationError`` (raw shape
errors) through the tmln formatter. Cross-field violations raised as
:class:`setforge.errors.SetforgeError` subclasses (cycle detection,
missing-profile reference, plugin reference to unknown marketplace,
etc.) must keep their existing bail-on-first ``schema:`` echo path —
their phrasing is the contract the e2e suite already keys on, and
they have no "Did you mean" suggestion path.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from setforge.cli import app


def _write_tracked_src(tmp_path: Path) -> None:
    (tmp_path / "tracked").mkdir(exist_ok=True)
    (tmp_path / "tracked" / "tracked_file.txt").write_text("x\n", encoding="utf-8")


def test_validate_setforge_yaml_cycle_error_not_routed_to_tmln(tmp_path: Path) -> None:
    """A profile-extends cycle surfaces via the existing ``schema:`` echo
    path, NOT the tmln carrier (cycle messages have their own contract).
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
        "  a:\n"
        "    extends: b\n"
        "    tracked_files: [d]\n"
        "  b:\n"
        "    extends: a\n"
        "    tracked_files: [d]\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["validate", "--profile=a", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    # Cycle text is preserved verbatim (not transformed into the SCHEMA
    # VALIDATION ERROR header).
    assert "profile cycle" in result.output
    assert "✗ SCHEMA VALIDATION ERROR" not in result.output


def test_validate_setforge_yaml_missing_profile_not_routed_to_tmln(
    tmp_path: Path,
) -> None:
    """A ``--profile=<name>`` that doesn't exist surfaces via the
    existing path (not tmln). The existing flow appends a string
    failure to the failures list; we just confirm tmln did NOT fire.
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
        "    tracked_files: [d]\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        app, ["validate", "--profile=does_not_exist", f"--config={cfg}"]
    )
    assert result.exit_code == 1, result.output
    # The tmln schema header MUST NOT be in the output — missing-profile
    # errors flow through the existing string-failures path.
    assert "✗ SCHEMA VALIDATION ERROR" not in result.output
    # The unknown-profile text surfaces verbatim.
    assert "does_not_exist" in result.output
