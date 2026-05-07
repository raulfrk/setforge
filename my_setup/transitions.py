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

import difflib
import json
import os
import platform
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from my_setup import __version__


class TransitionCommand(StrEnum):
    """Closed set of state-changing commands that record transitions."""

    INSTALL = "install"
    SYNC = "sync"
    REVERT = "revert"

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


@dataclass(frozen=True, slots=True)
class TransitionMeta:
    """Metadata for one transition. Serialized to ``meta.json``."""

    command: TransitionCommand
    profile: str
    timestamp: datetime    # UTC; serialized as ISO 8601
    host: str              # platform.node()
    version: str           # my_setup.__version__

    def to_dict(self) -> dict[str, str]:
        return {
            "command": self.command.value,
            "profile": self.profile,
            "timestamp": self.timestamp.astimezone(timezone.utc).isoformat(),
            "host": self.host,
            "version": self.version,
        }


def make_meta(command: TransitionCommand, profile: str) -> TransitionMeta:
    """Build a TransitionMeta with current host + version + UTC timestamp."""
    return TransitionMeta(
        command=command,
        profile=profile,
        timestamp=now_utc(),
        host=platform.node(),
        version=__version__,
    )


def write_meta(transition_dir: Path, meta: TransitionMeta) -> None:
    """Serialize ``meta`` to ``<transition_dir>/meta.json``.

    Creates ``transition_dir`` (with parents) if needed.
    """
    transition_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(meta.to_dict(), indent=2) + "\n"
    (transition_dir / "meta.json").write_text(payload, encoding="utf-8")


def snapshot_paths(paths: Iterable[Path]) -> dict[Path, str | None]:
    """Read every path in ``paths``. Missing files map to ``None``.

    Returns a dict so callers can pass it directly to :func:`compute_patch`.
    Reads as text/UTF-8; binary file deploys are out of scope for v1
    (the deploy primitive itself only handles text dotfiles today).
    """
    out: dict[Path, str | None] = {}
    for p in paths:
        try:
            out[p] = p.read_text(encoding="utf-8")
        except FileNotFoundError:
            out[p] = None
    return out


def compute_patch(
    pre: Mapping[Path, str | None],
    post: Mapping[Path, str | None],
) -> str:
    """Return one combined unified diff covering every path that
    differs between ``pre`` and ``post``.

    Missing files appear as ``/dev/null`` so ``patch`` can apply
    creations on forward (``+++ /a/b``) and deletions on reverse
    (``--- /a/b`` paired with ``+++ /dev/null``).
    """
    chunks: list[str] = []
    for path in sorted(set(pre) | set(post), key=str):
        before = pre.get(path)
        after = post.get(path)
        if before == after:
            continue
        before_lines = (before or "").splitlines(keepends=True)
        after_lines = (after or "").splitlines(keepends=True)
        from_path = "/dev/null" if before is None else str(path)
        to_path = "/dev/null" if after is None else str(path)
        chunks.append(
            "".join(
                difflib.unified_diff(
                    before_lines,
                    after_lines,
                    fromfile=from_path,
                    tofile=to_path,
                )
            )
        )
    return "".join(chunks)
