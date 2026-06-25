"""Narrow-terminal demo driver for the button-bar e2e (``test_e2e_docker_button_bar``).

Runs :func:`setforge.ui.widgets.button_bar` exactly once with the
conflict-wizard-shaped buttons and prints the chosen outcome so the pyte
harness can assert it from the emulated screen / final stdout:

    CHOSE:theirs        — a button value
    CHOSE:CANCEL        — Esc / Ctrl-C

Kept out of :mod:`setforge.ui.widgets` so the library module carries no
module-level ``print`` / TUI launch. Invoked in-container as
``uv run python tests/docker/_button_bar_demo.py`` under a real TTY.
"""

from __future__ import annotations

import sys

from setforge.ui.widgets import CANCEL, Button, button_bar

_BUTTONS = [
    Button("Ours", "ours"),
    Button("Theirs", "theirs"),
    Button("Edit", "edit"),
    Button("Skip", "skip"),
]


def main() -> int:
    result = button_bar(
        _BUTTONS,
        title="Conflict 1/1 · CLAUDE.md",
        body="ours (live) vs theirs (tracked)",
    )
    chosen = "CANCEL" if result is CANCEL else str(result)
    print(f"CHOSE:{chosen}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
