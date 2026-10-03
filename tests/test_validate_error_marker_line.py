"""``setforge validate`` marks the offending line and names missing fields."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from setforge.cli import app

_BASE = (
    "version: 1\n"
    "tracked_files:\n"
    "  note:\n"
    "    src: note.txt\n"
    "    dst: ~/.sb/note.txt\n"
    "profiles:\n"
    "  p:\n"
    "    tracked_files: [note]\n"
)


def _validate(tmp_path: Path, text: str) -> str:
    (tmp_path / "tracked").mkdir(exist_ok=True)
    (tmp_path / "note.txt").write_text("x\n", encoding="utf-8")
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(text, encoding="utf-8")
    result = CliRunner().invoke(app, ["validate", "--all", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    return result.output


def _marker_lines(output: str) -> list[str]:
    return [ln for ln in output.splitlines() if "←─── line" in ln]


def test_unknown_key_marker_is_on_the_offending_line(tmp_path: Path) -> None:
    out = _validate(
        tmp_path,
        _BASE.replace(
            "    dst: ~/.sb/note.txt\n", "    dst: ~/.sb/note.txt\n    bogus: 1\n"
        ),
    )
    (marker,) = _marker_lines(out)
    assert "bogus: 1" in marker


def test_bad_value_marker_is_on_the_offending_line(tmp_path: Path) -> None:
    out = _validate(
        tmp_path,
        _BASE.replace(
            "    dst: ~/.sb/note.txt\n", "    dst: ~/.sb/note.txt\n    mode: 0755x\n"
        ),
    )
    (marker,) = _marker_lines(out)
    assert "mode:" in marker


def test_missing_required_field_names_field_and_points_at_parent(
    tmp_path: Path,
) -> None:
    out = _validate(tmp_path, _BASE.replace("    src: note.txt\n", ""))
    assert "tracked_files.note.src" in out
    (marker,) = _marker_lines(out)
    assert "note:" in marker
    assert "setforge.yaml:3" in out


def test_unquoted_schema_version_hints_at_quoting(tmp_path: Path) -> None:
    out = _validate(tmp_path, "schema_version: 6.5\n" + _BASE)
    (marker,) = _marker_lines(out)
    assert "schema_version" in marker
    assert "quote" in out
