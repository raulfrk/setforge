"""Fresh-process proof that a revert interrupted mid-write is recovered whole."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.test_infra

_CRASH_AFTER_FIRST_REVERSAL = """
import os
from pathlib import Path
import setforge
from setforge import operations
from setforge.cli import main

expected = Path(os.environ["SETFORGE_EXPECTED_ROOT"])
assert Path(setforge.__file__).resolve().is_relative_to(expected)
real_apply = operations.apply_filesystem_deltas_reverse_anchored
calls = []

def crash(*args, **kwargs):
    real_apply(*args, **kwargs)
    calls.append(1)
    if len(calls) == int(os.environ["SETFORGE_CRASH_AFTER"]):
        os._exit(79)

operations.apply_filesystem_deltas_reverse_anchored = crash
main()
"""


def _run(
    argv: list[str], *, env: dict[str, str], cwd: Path, code: str | None = None
) -> subprocess.CompletedProcess[str]:
    prefix = ["-c", code] if code is not None else ["-m", "setforge.cli"]
    return subprocess.run(
        [sys.executable, *prefix, *argv],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )


@pytest.mark.parametrize("chain", [False, True])
def test_revert_interrupted_after_writing_files_recovers_through_the_journal(
    tmp_path: Path, chain: bool
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    (repo / "tracked").mkdir(parents=True)
    note = home / ".restart-revert" / "sub" / "note.txt"
    keep = home / ".restart-revert" / "k" / "keep.txt"
    source = repo / "tracked" / "note.txt"
    source.write_bytes(b"one\r\ntwo")
    (repo / "tracked" / "keep.txt").write_bytes(b"keep\n")
    config = repo / "setforge.yaml"
    config.write_text(
        "schema_version: '6.0'\n"
        "version: 1\n"
        "tracked_files:\n"
        f"  note:\n    src: note.txt\n    dst: {note}\n"
        f"  keep:\n    src: keep.txt\n    dst: {keep}\n"
        "profiles:\n"
        "  p:\n"
        "    tracked_files: [note, keep]\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "HOME": str(home),
        "SETFORGE_STATE_DIR": str(tmp_path / "state"),
        "SETFORGE_EXPECTED_ROOT": str(repo_root),
        # A chain crashes after its oldest step removed the created directories.
        "SETFORGE_CRASH_AFTER": "2" if chain else "1",
    }
    install = [
        "install",
        "--profile=p",
        f"--config={config}",
        "--no-fetch",
        "--no-git-check",
        "--no-secrets-scan",
        "--yes",
    ]
    assert _run(install, env=env, cwd=repo_root).returncode == 0
    first = sorted((tmp_path / "state" / "transitions").iterdir())[-1].name
    source.write_bytes(b"one\r\nTWO\x00\n")
    (repo / "tracked" / "keep.txt").chmod(0o600)
    installed = _run(install, env=env, cwd=repo_root)
    assert installed.returncode == 0, installed.stderr
    after = (note.read_bytes(), keep.read_bytes(), keep.stat().st_mode & 0o7777)
    records = sorted((tmp_path / "state" / "transitions").iterdir())
    revert = ["revert", "--profile=p", f"--config={config}", "--yes"]
    if chain:
        revert.append(f"--to-before={first}")

    crashed = _run(revert, env=env, cwd=repo_root, code=_CRASH_AFTER_FIRST_REVERSAL)

    assert crashed.returncode == 79, (crashed.stdout, crashed.stderr)
    if chain:
        # The update left note.txt.bak beside note.txt, so only the directory
        # that held keep.txt alone was removed.
        assert not keep.parent.exists()
        assert not note.exists()
    else:
        assert (note.read_bytes(), keep.stat().st_mode & 0o7777) != (
            after[0],
            after[2],
        )
    assert tuple((home / ".cache/setforge/operations").glob("*.json"))

    recovery = _run(
        ["recover", "--profile=p", "--apply", "--yes"], env=env, cwd=repo_root
    )

    assert recovery.returncode == 0, (recovery.stdout, recovery.stderr)
    assert (note.read_bytes(), keep.read_bytes(), keep.stat().st_mode & 0o7777) == (
        after
    )
    assert not tuple((home / ".cache/setforge/operations").glob("*.json"))
    assert sorted((tmp_path / "state" / "transitions").iterdir()) == records

    reverted = _run(revert, env=env, cwd=repo_root)
    assert reverted.returncode == 0, (reverted.stdout, reverted.stderr)
    if chain:
        assert not keep.parent.exists()
        assert not note.exists()
    else:
        assert note.read_bytes() == b"one\r\ntwo"
        assert keep.stat().st_mode & 0o7777 == 0o644
