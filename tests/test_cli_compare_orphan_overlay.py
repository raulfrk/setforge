"""Integration tests for ``setforge compare`` orphan-overlay surfacing.

``compare`` lists the ``local.yaml`` overlay entries the silent apply
site skipped:

- human output: a ``Skipped overlay entries (N):`` block after the
  orphans block, one line per entry with its class.
- ``--json``: a top-level ``orphan_overlay_entries`` array of
  ``{"id", "class"}`` objects (additive; existing keys untouched).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import app

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
def local_yaml(tmp_path: Path) -> Path:
    """Path the autouse conftest fixture already redirects
    ``setforge.source.LOCAL_CONFIG_PATH`` to (``tmp_path/local.yaml``)."""
    return tmp_path / "local.yaml"


def test_compare_human_lists_skipped_overlay_entries(
    tmp_path: Path, local_yaml: Path
) -> None:
    """Human output carries a ``Skipped overlay entries`` block listing
    both an unknown and an off-profile orphan with their classes."""
    cfg = _write_minimal_config(tmp_path)
    local_yaml.write_text(
        "tracked_files:\n"
        "  other_file:\n"
        "    mode: 0o755\n"  # off-profile
        "  bogus_id:\n"
        "    mode: 0o755\n",  # unknown
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["compare", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    assert "Skipped overlay entries" in result.output
    assert "other_file" in result.output
    assert "off_profile" in result.output
    assert "bogus_id" in result.output
    assert "unknown" in result.output


def test_compare_human_no_block_when_no_orphans(
    tmp_path: Path, local_yaml: Path
) -> None:
    """No orphan overlay entries ⇒ no skipped-overlay block."""
    cfg = _write_minimal_config(tmp_path)
    local_yaml.write_text(
        "tracked_files:\n  minimal_text:\n    mode: 0o755\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["compare", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    assert "Skipped overlay entries" not in result.output


def test_compare_json_carries_orphan_overlay_entries(
    tmp_path: Path, local_yaml: Path
) -> None:
    """``--json`` carries the additive ``orphan_overlay_entries`` key."""
    cfg = _write_minimal_config(tmp_path)
    local_yaml.write_text(
        "tracked_files:\n"
        "  other_file:\n"
        "    mode: 0o755\n"
        "  bogus_id:\n"
        "    mode: 0o755\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        app, ["--format=json", "compare", "--profile=p", f"--config={cfg}"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)["data"]
    entries = payload["orphan_overlay_entries"]
    by_id = {e["id"]: e["class"] for e in entries}
    assert by_id == {"other_file": "off_profile", "bogus_id": "unknown"}
    # Additive: the existing keys are untouched.
    assert "entries" in payload
    assert "orphans" in payload
    assert "has_unexpected_drift" in payload


def test_compare_json_empty_when_no_orphans(tmp_path: Path, local_yaml: Path) -> None:
    """``orphan_overlay_entries`` is an empty list when there are no orphans."""
    cfg = _write_minimal_config(tmp_path)
    result = CliRunner().invoke(
        app, ["--format=json", "compare", "--profile=p", f"--config={cfg}"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)["data"]
    assert payload["orphan_overlay_entries"] == []
