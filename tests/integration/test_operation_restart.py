"""Fresh-process proof that a real writer journal survives abrupt death."""

from __future__ import annotations

import os
import shutil
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


def test_retired_store_prune_recovers_after_uncatchable_process_exit(
    tmp_path: Path,
) -> None:
    """A new process restores retired reconcile state after prune publishes."""
    repo_root = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    tracked = tmp_path / "tracked"
    tracked.mkdir()
    (tracked / "doc.txt").write_text("tracked\n", encoding="utf-8")
    live = home / "live" / "doc.txt"
    config = tmp_path / "setforge.yaml"
    config.write_text(
        "schema_version: '6.0'\n"
        "version: 1\n"
        "tracked_files:\n"
        "  doc:\n"
        "    src: doc.txt\n"
        f"    dst: {live}\n"
        "profiles:\n"
        "  restart-prune:\n"
        "    tracked_files: [doc]\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "HOME": str(home),
        "SETFORGE_STATE_DIR": str(state),
        "SETFORGE_EXPECTED_ROOT": str(repo_root),
    }
    install_args = [
        sys.executable,
        "-m",
        "setforge.cli",
        "install",
        "--profile=restart-prune",
        f"--config={config}",
        "--no-fetch",
        "--no-git-check",
        "--no-secrets-scan",
        "--no-transition",
        "--yes",
    ]
    seeded = subprocess.run(
        install_args,
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert seeded.returncode == 0, (seeded.stdout, seeded.stderr)
    store_paths = (
        state / "base" / "restart-prune" / "doc",
        state / "local" / "restart-prune" / "doc",
        state / "index" / "restart-prune.json",
    )
    before = tuple(path.read_bytes() for path in store_paths)

    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "    tracked_files: [doc]\n", "    tracked_files: []\n"
        ),
        encoding="utf-8",
    )
    crash_after_prune = """
import os
from pathlib import Path
import setforge
from setforge.cli import main
from setforge.reconcile import store

expected = Path(os.environ["SETFORGE_EXPECTED_ROOT"])
assert Path(setforge.__file__).resolve().is_relative_to(expected)
real_prune = store.prune

def crash(profile, live_fids):
    changed = real_prune(profile, live_fids)
    assert changed
    os._exit(79)

store.prune = crash
main()
"""
    crashed = subprocess.run(
        [sys.executable, "-c", crash_after_prune, *install_args[3:]],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert crashed.returncode == 79, (crashed.stdout, crashed.stderr)
    assert not store_paths[0].exists()
    assert not store_paths[1].exists()
    assert tuple((home / ".cache/setforge/operations").glob("*.json"))

    recovery = subprocess.run(
        [
            sys.executable,
            "-m",
            "setforge.cli",
            "recover",
            "--profile=restart-prune",
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
    assert tuple(path.read_bytes() for path in store_paths) == before
    assert not tuple((home / ".cache/setforge/operations").glob("*.json"))


def test_snapshot_restore_recovers_after_uncatchable_process_exit(
    tmp_path: Path,
) -> None:
    """A fresh CLI process restores live files after snapshot restore dies."""
    repo_root = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    tracked = tmp_path / "tracked"
    tracked.mkdir()
    (tracked / "first.txt").write_text("tracked first\n", encoding="utf-8")
    (tracked / "second.txt").write_text("tracked second\n", encoding="utf-8")
    (tracked / "third.txt").write_text("tracked third\n", encoding="utf-8")
    first = home / "live" / "first.txt"
    second = home / "live" / "second.txt"
    third = home / "deleted-live" / "third.txt"
    first.parent.mkdir()
    first.write_text("snapshot first\n", encoding="utf-8")
    second.write_text("snapshot second\n", encoding="utf-8")
    third.parent.mkdir()
    third.write_text("snapshot third\n", encoding="utf-8")
    local_config = home / ".config" / "setforge" / "local.yaml"
    local_config.parent.mkdir(parents=True)
    local_config.write_text("binaries:\n  code: /snapshot/code\n", encoding="utf-8")
    config = tmp_path / "setforge.yaml"
    config.write_text(
        "schema_version: '6.0'\n"
        "version: 1\n"
        "tracked_files:\n"
        "  first:\n"
        "    src: first.txt\n"
        f"    dst: {first}\n"
        "  second:\n"
        "    src: second.txt\n"
        f"    dst: {second}\n"
        "  third:\n"
        "    src: third.txt\n"
        f"    dst: {third}\n"
        "profiles:\n"
        "  restart-snapshot:\n"
        "    tracked_files: [first, second, third]\n"
        "  other-snapshot:\n"
        "    tracked_files: [first]\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "HOME": str(home),
        "SETFORGE_STATE_DIR": str(state),
        "SETFORGE_EXPECTED_ROOT": str(repo_root),
    }
    create = subprocess.run(
        [
            sys.executable,
            "-m",
            "setforge.cli",
            "snapshot",
            "create",
            "restart",
            "--profile=restart-snapshot",
            f"--config={config}",
        ],
        cwd=tracked,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert create.returncode == 0, (create.stdout, create.stderr)
    first.write_text("pre-restore first\n", encoding="utf-8")
    second.write_text("pre-restore second\n", encoding="utf-8")
    shutil.rmtree(third.parent)
    local_config.write_text("binaries:\n  code: /before/code\n", encoding="utf-8")
    first_mtime_ns = 1_700_000_000_111_111_111
    second_mtime_ns = 1_700_000_000_222_222_222
    local_mtime_ns = 1_700_000_000_333_333_333
    os.utime(first, ns=(first_mtime_ns, first_mtime_ns))
    os.utime(second, ns=(second_mtime_ns, second_mtime_ns))
    os.utime(local_config, ns=(local_mtime_ns, local_mtime_ns))
    crash_after_first_write = """
import os
from pathlib import Path
import setforge
from setforge import snapshots
from setforge.cli import main

expected = Path(os.environ["SETFORGE_EXPECTED_ROOT"])
assert Path(setforge.__file__).resolve().is_relative_to(expected)
crash_target = Path(os.environ["SETFORGE_CRASH_LIVE"])
real_write = snapshots._write_restored_file

def crash(source, guard_identities):
    real_write(source, guard_identities)
    if source.path == crash_target:
        os._exit(79)

snapshots._write_restored_file = crash
main()
"""
    crash_env = {**env, "SETFORGE_CRASH_LIVE": str(third)}
    restore = subprocess.run(
        [
            sys.executable,
            "-c",
            crash_after_first_write,
            "snapshot",
            "restore",
            "restart",
            "--profile=restart-snapshot",
            f"--config={config}",
            "--yes",
        ],
        cwd=repo_root,
        env=crash_env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert restore.returncode == 79, (restore.stdout, restore.stderr)
    assert first.read_text(encoding="utf-8") == "snapshot first\n"
    assert third.read_text(encoding="utf-8") == "snapshot third\n"
    assert tuple((home / ".cache/setforge/operations").glob("*.json"))

    blocked_local_edit = subprocess.run(
        [
            sys.executable,
            "-m",
            "setforge.cli",
            "config",
            "add",
            "--local",
            "binaries.code",
            "/blocked/code",
            "--yes",
        ],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert blocked_local_edit.returncode == 1, (
        blocked_local_edit.stdout,
        blocked_local_edit.stderr,
    )
    assert "unfinished snapshot restore operation" in (
        blocked_local_edit.stdout + blocked_local_edit.stderr
    )
    assert local_config.read_text(encoding="utf-8") == (
        "binaries:\n  code: /before/code\n"
    )

    blocked_other_profile = subprocess.run(
        [
            sys.executable,
            "-m",
            "setforge.cli",
            "install",
            "--profile=other-snapshot",
            f"--config={config}",
            "--yes",
            "--no-fetch",
            "--no-git-check",
            "--no-secrets-scan",
            "--no-transition",
        ],
        cwd=tracked,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert blocked_other_profile.returncode == 1, (
        blocked_other_profile.stdout,
        blocked_other_profile.stderr,
    )
    assert "unfinished snapshot restore operation" in (
        blocked_other_profile.stdout + blocked_other_profile.stderr
    )
    assert first.read_text(encoding="utf-8") == "snapshot first\n"

    recovery = subprocess.run(
        [
            sys.executable,
            "-m",
            "setforge.cli",
            "recover",
            "--profile=restart-snapshot",
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
    assert first.read_text(encoding="utf-8") == "pre-restore first\n"
    assert second.read_text(encoding="utf-8") == "pre-restore second\n"
    assert not third.parent.exists()
    assert local_config.read_text(encoding="utf-8") == (
        "binaries:\n  code: /before/code\n"
    )
    assert first.stat().st_mtime_ns == first_mtime_ns
    assert second.stat().st_mtime_ns == second_mtime_ns
    assert local_config.stat().st_mtime_ns == local_mtime_ns
    assert not tuple((home / ".cache/setforge/operations").glob("*.json"))
