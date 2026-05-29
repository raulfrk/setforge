"""Centralized $EDITOR invocation with shutil.which pre-validation."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

from setforge.errors import SetforgeError

_EDITOR_TIMEOUT_S = 3600


def run_editor(target: Path) -> None:
    """Open ``$EDITOR`` on ``target`` and block until it exits.

    Honors ``EDITOR='code --wait'``-style multi-token invocations via
    :func:`shlex.split`. Pre-validates the editor binary with
    :func:`shutil.which` and raises :class:`SetforgeError` with a
    clean message instead of a raw :exc:`FileNotFoundError` when the
    editor is missing. Propagates
    :exc:`subprocess.CalledProcessError` on non-zero exit (caller
    decides whether that's an error — e.g. user :q!ing vim).

    The edit session is bounded by ``timeout=_EDITOR_TIMEOUT_S`` (1h);
    on expiry the editor is SIGKILLed and :exc:`subprocess.TimeoutExpired`
    is surfaced as :class:`SetforgeError`.
    """
    editor = os.environ.get("EDITOR", "vi")
    try:
        argv = shlex.split(editor)
    except ValueError as exc:
        raise SetforgeError(f"$EDITOR={editor!r} has malformed quoting: {exc}") from exc
    if not argv:
        raise SetforgeError("$EDITOR is empty; set EDITOR=<editor-binary> and retry.")
    if shutil.which(argv[0]) is None:
        raise SetforgeError(
            f"editor {argv[0]!r} not found on PATH; "
            f"set $EDITOR or install it, then retry."
        )
    try:
        # On timeout the interactive editor is SIGKILLed; a killed TTY editor
        # (e.g. vim) may leave the terminal in a raw/cooked state with no cleanup.
        subprocess.run([*argv, str(target)], check=True, timeout=_EDITOR_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise SetforgeError("editor timed out after 1h; aborting edit.") from exc
