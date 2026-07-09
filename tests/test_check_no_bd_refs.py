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
        # Stemmed depth-3 epic-child version shorthand — caught by the boundary-
        # anchored arms.
        ("post-4.15.4\n", 1),  # post- stem, depth-3
        ("setforge-deoq.4.15.4\n", 1),  # setforge-<stem> depth-3
        ("see post-4.15.4 in the notes\n", 1),  # embedded, left boundary is space
        ("the setforge-deoq.4.15.4 bead\n", 1),  # embedded, both boundaries clean
        # Bare stem-less shorthand + RFC bead pointers stay DEFERRED to the agent.
        ("bare deoq.4.15.4 with no stem\n", 0),  # no post-/setforge- stem
        ("RFC bead deoq.4.3 pointer\n", 0),  # depth-2 RFC id
        # Real version pins must never false-block (left/right guard, magnitude).
        ("gitleaks 8.21.2 in the image\n", 0),  # high-magnitude pin, no stem
        ("ARG GITLEAKS_VERSION=8.21.2\n", 0),
        ("typing_extensions==4.15.0 dep\n", 0),
        ("prompt_toolkit==3.0.50 dep\n", 0),
        ("mutmut==3.6.0 dep\n", 0),
        ('schema_version = "3.0"\n', 0),
        ('version = "0.3.0"\n', 0),
        ("bare 4.15.0 with no stem\n", 0),
        ("compost-4.15.4 left-guard defeats post-\n", 0),  # left guard
        ("setforge-deoq.4.15.44 longer final segment\n", 0),  # right guard / magnitude
        ("setforge-deoq.4.15.4-rc2 semver prerelease\n", 0),  # right guard rejects -
        ("post-4.15.4.9 four-segment pin\n", 0),  # right guard rejects trailing .
        ("post-1.0 depth-2 pin\n", 0),  # depth-2, below the arm's depth-3 floor
        ("setforge-p5qc-audit worktree slug\n", 0),  # no version tail
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
        # Epic-child shorthand is caught in a real (non-comment) body line...
        ("feat: land it\n\ncloses post-4.15.4\n", 1),
        # ...but a shorthand token on a `#` comment line is stripped and ignored
        # (commit-msg mode ONLY — file mode scans every line; see the file-mode
        # counterpart below).
        ("feat: land it\n\n# closes post-4.15.4\n", 0),
    ],
)
def test_commit_msg_gate(tmp_path: Path, message: str, expected: int) -> None:
    """Commit-message mode flags tracker tokens, ignoring comment lines."""
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(message)
    assert _run("--commit-msg", str(msg)) == expected


def test_file_mode_scans_comment_lines(tmp_path: Path) -> None:
    """File mode scans EVERY line: a `#`-prefixed leak is still flagged.

    This pins the intended split against the commit-msg mode, which strips
    `#` lines (a comment token there is git-scissors chrome, not a shipped
    artifact). In a source file a `#` line is a real code comment and must
    not be a blind spot.
    """
    f = tmp_path / "sample.py"
    f.write_text("# closes post-4.15.4\n")
    assert _run(str(f)) == 1
