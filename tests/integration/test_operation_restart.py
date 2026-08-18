"""Fresh-process proof that a real writer journal survives abrupt death."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.test_infra


def test_install_journal_recovers_after_uncatchable_process_exit(
    tmp_path: Path,
) -> None:
    """A new CLI process restores the first real install effect after ``_exit``."""
    repo_root = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    created = home / ".restart-recovery" / "bootstrap"
    config = tmp_path / "setforge.yaml"
    config.write_text(
        "schema_version: '6.0'\n"
        "version: 1\n"
        "tracked_files: {}\n"
        "profiles:\n"
        "  restart-recovery:\n"
        "    bootstrap:\n"
        f"      - {created}\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "HOME": str(home),
        "SETFORGE_STATE_DIR": str(state),
        "SETFORGE_EXPECTED_ROOT": str(repo_root),
    }
    crash_after_bootstrap = """
import os
from pathlib import Path
import setforge
from setforge import deploy
from setforge.cli import main

expected = Path(os.environ["SETFORGE_EXPECTED_ROOT"])
assert Path(setforge.__file__).resolve().is_relative_to(expected)
real_bootstrap = deploy.bootstrap_local

def crash(paths):
    real_bootstrap(paths)
    os._exit(79)

deploy.bootstrap_local = crash
main()
"""
    install = subprocess.run(
        [
            sys.executable,
            "-c",
            crash_after_bootstrap,
            "install",
            "--profile=restart-recovery",
            f"--config={config}",
            "--no-fetch",
            "--no-git-check",
            "--no-secrets-scan",
            "--no-transition",
            "--yes",
        ],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert install.returncode == 79, (install.stdout, install.stderr)
    assert created.is_file()
    assert tuple((home / ".cache/setforge/operations").glob("*.json"))

    recovery = subprocess.run(
        [
            sys.executable,
            "-m",
            "setforge.cli",
            "recover",
            "--profile=restart-recovery",
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
    assert not (home / ".restart-recovery").exists()
    assert not tuple((home / ".cache/setforge/operations").glob("*.json"))
