"""Top-level typo did-you-mean routing for ``setforge validate``.

Mirrors the local.yaml did-you-mean close-match UX but exercises
the engine-config side: a typo'd top-level key in setforge.yaml routes
through ``format_schema_validation_error`` + ``suggest_close_match``
against ``Config.model_fields.keys()`` instead of bailing on first error
via ``typer.Exit(1)``.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from setforge.cli import app


def _write_tracked_src(tmp_path: Path) -> None:
    """Write a minimal tracked source so the file-exists check passes
    when validation reaches Check 4 (after the schema-error path)."""
    (tmp_path / "tracked").mkdir(exist_ok=True)
    (tmp_path / "tracked" / "tracked_file.txt").write_text("x\n", encoding="utf-8")


def test_validate_setforge_yaml_top_level_typo_suggests_known_key(
    tmp_path: Path,
) -> None:
    """A typo'd top-level key (``proffiles:``) routes through the did-you-mean
    formatter and surfaces a "Did you mean 'profiles'" suggestion.

    Acceptance: exit code 1, structured schema-error output (header +
    snippet marker + Fix), and the close-match suggestion against the
    introspected ``Config.model_fields.keys()`` list.
    """
    _write_tracked_src(tmp_path)
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(
        # ``proffiles`` is Levenshtein distance 1 from the known
        # ``profiles`` top-level key on Config.
        "version: 1\n"
        "tracked_files:\n"
        "  d:\n"
        "    src: tracked_file.txt\n"
        "    dst: ~/.some-tracked_file\n"
        "proffiles:\n"
        "  p:\n"
        "    tracked_files: [d]\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["validate", "--all", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    assert "✗ SCHEMA VALIDATION ERROR" in result.output
    assert "←─── line" in result.output
    assert "Did you mean 'profiles'" in result.output
    assert "Fix:" in result.output
    assert "validation FAILED" in result.output


def test_validate_setforge_yaml_top_level_typo_no_close_match_omits_suggestion(
    tmp_path: Path,
) -> None:
    """A top-level typo with no candidate inside the Levenshtein <= 2 gate
    surfaces a schema error WITHOUT a "Did you mean" line (anti-smell:
    no false-positive suggestions)."""
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
        "thoroughly_unrelated_xyz: oops\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["validate", "--all", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    assert "✗ SCHEMA VALIDATION ERROR" in result.output
    assert "Did you mean" not in result.output
