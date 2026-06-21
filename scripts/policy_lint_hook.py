#!/usr/bin/env python3
"""PreToolUse(Bash) hook — block a ``git commit`` when policy lints fail.

Runs :mod:`scripts.check_policy_lints` over the repo when the Bash command is a
``git commit`` and maps the result onto the Claude Code hook contract:

* lints clean (exit 0) → **exit 0** (allow the commit)
* lints fail (exit 1, or any nonzero) → reason to **stderr**, **exit 2** (block)
* any internal error → **exit 2** (FAIL-CLOSED)

For PreToolUse, *only* exit 2 blocks — any other nonzero **fails open** (the
action proceeds) — so the top-level guard forces exit 2 on the unexpected. The
``git commit`` match also can't see every command shape, so CI is the hard
backstop; this hook is fast local feedback, not the sole guarantee.

``POLICY_LINT_CMD`` overrides the lint command (used by the tests).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "check_policy_lints.py"
_GIT_COMMIT_RE = re.compile(r"\bgit\b.*\bcommit\b", re.DOTALL)


def is_git_commit(command: str) -> bool:
    """True if the Bash command runs ``git commit`` (incl. inside a compound)."""
    return bool(_GIT_COMMIT_RE.search(command or ""))


def _lint_cmd() -> list[str]:
    override = os.environ.get("POLICY_LINT_CMD")
    return shlex.split(override) if override else [sys.executable, str(SCRIPT)]


def run_lints() -> tuple[int, str]:
    r = subprocess.run(_lint_cmd(), capture_output=True, text=True)
    return r.returncode, (r.stderr or r.stdout)


def main() -> None:
    raw = sys.stdin.read()
    try:
        event = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        event = {}
    tool_input = event.get("tool_input", {}) if isinstance(event, dict) else {}
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""

    if not is_git_commit(command):
        sys.exit(0)  # not a commit — nothing to gate

    rc, msg = run_lints()
    if rc == 0:
        sys.exit(0)
    print(
        "Commit blocked by policy lints (SAFE-1/2, UX-1/3 — see docs/RULES.md):\n"
        + msg,
        file=sys.stderr,
    )
    sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as exc:  # fail-CLOSED: never let an error fail open
        print(f"policy-lint hook error (blocking to be safe): {exc}", file=sys.stderr)
        sys.exit(2)
