"""Transition records: per-invocation undo support for install/sync.

Each state-changing command (install, sync, revert) writes a directory
under ``~/.local/state/my-setup/transitions/`` containing:

- ``meta.json`` — command, profile, UTC timestamp, host, my-setup version
- ``changes.patch`` — unified diff of file changes (omitted if no edits)
- ``extensions.json`` — added/removed extension IDs (omitted if no delta)

A subsequent ``my-setup revert`` consumes the most recent transition for
a profile, applies the patch in reverse via ``patch -R``, reverses the
extension delta, and records its own reverse transition.
"""

import os
from datetime import datetime, timezone
from pathlib import Path

_STATE_ENV = "MY_SETUP_STATE_DIR"
_DEFAULT_STATE_ROOT_SUFFIX = (".local", "state", "my-setup")


def state_root() -> Path:
    """Resolve the my-setup state dir.

    Honors the ``MY_SETUP_STATE_DIR`` env var (used by tests and by
    operators relocating state). Falls back to ``~/.local/state/my-setup``.
    """
    override = os.environ.get(_STATE_ENV)
    if override:
        return Path(override)
    return Path.home().joinpath(*_DEFAULT_STATE_ROOT_SUFFIX)


def transitions_root() -> Path:
    """Directory that holds every transition record for this host."""
    return state_root() / "transitions"


def now_utc() -> datetime:
    """Single source of truth for transition timestamps."""
    return datetime.now(timezone.utc)


def transition_dirname(timestamp: datetime, command: str, profile: str) -> str:
    """Return the directory name for one transition.

    Format: ``YYYYMMDDTHHMMSSZ-<command>-<profile>`` so that lexicographic
    sort matches chronological sort and ``load_latest`` is a single ``max()``.
    """
    iso = timestamp.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{iso}-{command}-{profile}"
