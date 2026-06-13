"""Integration tests for ``setforge validate`` orphan-overlay diagnostics.

``local.yaml`` may carry ``tracked_files.<id>`` overlay entries that the
silent apply site (``apply_host_local_tracked_file_overrides``) skips.
``validate`` surfaces them:

- **Unknown** id (absent from ``setforge.yaml``'s ``tracked_files``) → a
  hard SCHEMA VALIDATION ERROR failure (exit 1) with a did-you-mean
  suggestion drawn from the known tracked_file ids.
- **Off-profile** id (in the registry but not THIS profile's list) → a
  non-fatal note to stderr; exit stays 0.

Companion to :mod:`tests.test_collect_orphan_overlays` (the pure
classifier) and :mod:`tests.test_cli_validate_did_you_mean` (the
formatter UX).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import app

# Two tracked_files; profile ``p`` includes only ``minimal_text``.
_CLEAN_YAML = """\
version: 1
tracked_files:
  minimal_text:
    src: a.txt
    dst: ~/.a
  other_file:
    src: b.txt
    dst: ~/.b
profiles:
  p:
    tracked_files: [minimal_text]
  q:
    tracked_files: [other_file]
"""


def _write_minimal_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(_CLEAN_YAML, encoding="utf-8")
    tracked = tmp_path / "tracked"
    tracked.mkdir(exist_ok=True)
    (tracked / "a.txt").write_text("x\n", encoding="utf-8")
    (tracked / "b.txt").write_text("y\n", encoding="utf-8")
    return cfg


@pytest.fixture
def local_yaml_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point validate's _LOCAL_CONFIG_PATH at a per-test path."""
    local = tmp_path / "local.yaml"
    monkeypatch.setattr("setforge.cli.validate._LOCAL_CONFIG_PATH", local)
    return local


def test_unknown_overlay_id_fails_with_did_you_mean(
    tmp_path: Path, local_yaml_at: Path
) -> None:
    """An overlay id unknown to setforge.yaml → exit 1 + did-you-mean line.

    ``minimal_tex`` is Levenshtein 1 from ``minimal_text``.
    """
    cfg = _write_minimal_config(tmp_path)
    local_yaml_at.write_text(
        "tracked_files:\n  minimal_tex:\n    mode: 0o755\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    assert "Did you mean 'minimal_text'" in result.output
    assert "minimal_tex" in result.output
    assert "validation FAILED" in result.output


def test_off_profile_overlay_id_prints_note_exit_zero(
    tmp_path: Path, local_yaml_at: Path
) -> None:
    """An overlay id in the registry but not in profile ``p`` → exit 0 + note."""
    cfg = _write_minimal_config(tmp_path)
    local_yaml_at.write_text(
        "tracked_files:\n  other_file:\n    mode: 0o755\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    assert "other_file" in result.output
    # Non-fatal: must NOT be reported as a validation failure.
    assert "validation FAILED" not in result.output


def test_in_profile_overlay_id_no_note_no_failure(
    tmp_path: Path, local_yaml_at: Path
) -> None:
    """An overlay id IN profile ``p`` is neither a failure nor a note."""
    cfg = _write_minimal_config(tmp_path)
    local_yaml_at.write_text(
        "tracked_files:\n  minimal_text:\n    mode: 0o755\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    assert "ok" in result.output


def test_unknown_far_from_any_id_no_suggestion_still_fails(
    tmp_path: Path, local_yaml_at: Path
) -> None:
    """An unknown id with no close match still fails, just without a
    did-you-mean line (Levenshtein > 2 gate)."""
    cfg = _write_minimal_config(tmp_path)
    local_yaml_at.write_text(
        "tracked_files:\n  zzzzzzzz:\n    mode: 0o755\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    assert "zzzzzzzz" in result.output
    assert "Did you mean" not in result.output


def test_all_profiles_off_profile_only_when_in_no_profile(
    tmp_path: Path, local_yaml_at: Path
) -> None:
    """Under ``--all``, an id used by ANOTHER profile is not flagged
    off-profile: ``other_file`` belongs to profile ``q``, so a full-config
    validate stays clean (no note, exit 0)."""
    cfg = _write_minimal_config(tmp_path)
    local_yaml_at.write_text(
        "tracked_files:\n  other_file:\n    mode: 0o755\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["validate", "--all", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    assert "ok" in result.output
