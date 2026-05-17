"""Centralized $EDITOR invocation with shutil.which pre-validation."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

from my_setup.errors import MySetupError


def run_editor(target: Path) -> None:
    """Open ``$EDITOR`` on ``target`` and block until it exits.

    Honors ``EDITOR='code --wait'``-style multi-token invocations via
    :func:`shlex.split`. Pre-validates the editor binary with
    :func:`shutil.which` and raises :class:`MySetupError` with a
    clean message instead of a raw :exc:`FileNotFoundError` when the
    editor is missing. Propagates
    :exc:`subprocess.CalledProcessError` on non-zero exit (caller
    decides whether that's an error — e.g. user :q!ing vim).
    """
    editor = os.environ.get("EDITOR", "vi")
    argv = shlex.split(editor)
    if not argv:
        raise MySetupError("$EDITOR is empty; set EDITOR=<editor-binary> and retry.")
    if shutil.which(argv[0]) is None:
        raise MySetupError(
            f"editor {argv[0]!r} not found on PATH; "
            f"set $EDITOR or install it, then retry."
        )
    subprocess.run([*argv, str(target)], check=True)
