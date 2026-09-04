from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import _install_helpers, app
from setforge.cli._helpers import ProfileContext
from setforge.compare import CompareReport, CompareStatus, FileCompare
from setforge.config import Config, Profile, ResolvedProfile, TrackedFile
from setforge.errors import SetforgeError


def test_dry_run_refuses_mismatched_regular_deploy_plan(tmp_path: Path) -> None:
    source = tmp_path / "tracked" / "note.md"
    source.parent.mkdir()
    source.write_text("tracked\n", encoding="utf-8")
    tracked = TrackedFile(src=Path("note.md"), dst=str(tmp_path / "live.md"))
    cfg = Config(
        tracked_files={"note": tracked},
        profiles={"p": Profile(tracked_files=["note"])},
    )
    ctx = ProfileContext(
        cfg=cfg,
        resolved=ResolvedProfile(tracked_files=["note"]),
        repo_root=tmp_path,
        profile="p",
    )
    report = CompareReport(
        entries=[FileCompare("note", CompareStatus.MISSING, "")],
        has_unexpected_drift=False,
    )

    with pytest.raises(SetforgeError, match="immutable deploy plan does not match"):
        _install_helpers._dry_run_pipeline(
            ctx=ctx,
            drift_report=report,
            deploys=(),
            host_local_sections_map={},
        )


def test_managed_tree_dry_run_renders_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    repo = tmp_path / "repo"
    source = repo / "tracked" / "tools"
    source.mkdir(parents=True)
    (source / "tool.txt").write_text("tracked\n", encoding="utf-8")
    config = repo / "setforge.yaml"
    config.write_text(
        "schema_version: '6.5'\n"
        "minimum_version: '6.5'\n"
        "tracked_files:\n"
        "  tools:\n"
        "    src: tools\n"
        "    dst: ~/.tools\n"
        "    tree: {}\n"
        "profiles:\n"
        "  p:\n"
        "    tracked_files: [tools]\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "install",
            "--profile=p",
            f"--config={config}",
            "--dry-run",
            "--no-fetch",
            "--no-git-check",
            "--no-secrets-scan",
            "--no-transition",
        ],
    )

    assert result.exit_code == 0, result.output
    assert f"WOULD install   {home / '.tools'}" in result.output
    assert result.output.rstrip().endswith(
        "=== rerun without --dry-run to apply for real ==="
    )
