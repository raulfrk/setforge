"""Fresh-process recovery for interrupted unrecorded orphan cleanup."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.test_infra


def test_scan_cleanup_recovers_after_uncatchable_process_exit(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    config_repo = tmp_path / "config-repo"
    config_repo.mkdir()
    (config_repo / "kept.txt").write_text("tracked", encoding="utf-8")
    live = home / ".restart-orphans" / "tool"
    live.mkdir(parents=True)
    first = live / "first"
    second = live / "second"
    first.write_bytes(b"\x00first")
    second.write_bytes(b"\xffsecond")
    config = config_repo / "setforge.yaml"
    config.write_text(
        "schema_version: '6.0'\n"
        "tracked_files:\n"
        "  kept:\n"
        "    src: kept.txt\n"
        f"    dst: {live / 'kept.txt'}\n"
        "profiles:\n"
        "  restart-orphans:\n"
        "    tracked_files: [kept]\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "HOME": str(home),
        "SETFORGE_STATE_DIR": str(state),
        "SETFORGE_EXPECTED_ROOT": str(repo_root),
        "SETFORGE_TEST_CONFIG": str(config),
    }
    crash_after_first = """
import os
from pathlib import Path
import setforge
from rich.console import Console
from setforge.cli import orphans

expected = Path(os.environ["SETFORGE_EXPECTED_ROOT"])
assert Path(setforge.__file__).resolve().is_relative_to(expected)
orphans._confirm_scan_entries = lambda entries, _console: entries
real_unlink = orphans.orphan_scan.unlink_approved_entry
calls = 0

def crash(entry):
    global calls
    real_unlink(entry)
    calls += 1
    if calls == 1:
        os._exit(79)

orphans.orphan_scan.unlink_approved_entry = crash
orphans._execute_scan_cleanup(
    "restart-orphans", Path(os.environ["SETFORGE_TEST_CONFIG"]), console=Console()
)
"""
    crashed = subprocess.run(
        [sys.executable, "-c", crash_after_first],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert crashed.returncode == 79, (crashed.stdout, crashed.stderr)
    assert not first.exists()
    assert second.read_bytes() == b"\xffsecond"
    assert tuple((home / ".cache/setforge/operations").glob("*.json"))

    recovery = subprocess.run(
        [
            sys.executable,
            "-m",
            "setforge.cli",
            "recover",
            "--profile=restart-orphans",
            "--apply",
            "--yes",
        ],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert recovery.returncode == 0, (recovery.stdout, recovery.stderr)
    assert first.read_bytes() == b"\x00first"
    assert second.read_bytes() == b"\xffsecond"
    assert not tuple((home / ".cache/setforge/operations").glob("*.json"))
    cleanup_records = (state / "transitions").glob("*cleanup-orphans*")
    assert not tuple(cleanup_records)
