"""Tests for the ``scripts/check-no-bd-refs.sh`` pre-commit hard gate.

The gate is the deterministic, high-precision half of bd-leak enforcement: it
blocks ``bd <subcommand>`` command lines, ``.beads/`` paths, and ``~/handoff``
from entering shipping artifacts, while never false-blocking on the repo's own
name or the exempt private layer.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check-no-bd-refs.sh"


def _run(*args: str) -> int:
    """Invoke the hook script with ``args``; return its exit code."""
    return subprocess.run(
        [str(_SCRIPT), *args], capture_output=True, text=True
    ).returncode


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("run bd show some-id for context\n", 1),  # bd subcommand
        ("paths .beads/ live here\n", 1),  # .beads path
        ("see ~/handoff for state\n", 1),  # handoff repo path
        ("clean code; setforge-config is just the repo name\n", 0),  # no false-block
        ("plain prose with no tracker tokens\n", 0),
    ],
)
def test_file_content_gate(tmp_path: Path, content: str, expected: int) -> None:
    """File-content mode flags structured tracker tokens, not repo names."""
    f = tmp_path / "sample.py"
    f.write_text(content)
    assert _run(str(f)) == expected


@pytest.mark.parametrize(
    "relpath",
    [
        "CLAUDE.md",
        "tracked/claude/agents/x.md",
        ".dockerignore",
        ".gitignore",
        "scripts/check-no-bd-refs.sh",
    ],
)
def test_exempt_paths_never_flagged(tmp_path: Path, relpath: str) -> None:
    """The private layer and the invisibility mechanism are exempt."""
    f = tmp_path / relpath
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("bd ready and .beads/ appear here legitimately\n")
    assert _run(str(f)) == 0


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("fix: a thing\n\nper bd create notes\n", 1),
        ("fix: a thing\n\nwith an ordinary body\n", 0),
        ("feat: add gate\n\n# a comment line: bd show\n", 0),  # comment stripped
    ],
)
def test_commit_msg_gate(tmp_path: Path, message: str, expected: int) -> None:
    """Commit-message mode flags tracker tokens, ignoring comment lines."""
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(message)
    assert _run("--commit-msg", str(msg)) == expected
