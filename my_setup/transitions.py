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
import shutil
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from my_setup import __version__
from my_setup.errors import RevertFailed


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


def _diff_path(path: Path) -> str:
    """Format a Path for a diff header.

    GNU patch's safe-paths feature rejects absolute paths as "potentially
    dangerous." Workaround: emit paths root-relative (no leading ``/``),
    and apply with ``patch -d /`` so the relative path resolves
    absolute. ``/dev/null`` is the standard sentinel for missing files
    and must NOT be stripped.
    """
    s = str(path)
    return s.lstrip("/") if s.startswith("/") else s


def compute_patch(
    pre: Mapping[Path, str | None],
    post: Mapping[Path, str | None],
) -> str:
    """Return one combined unified diff covering every path that
    differs between ``pre`` and ``post``.

    Missing files appear as ``/dev/null`` so ``patch`` can apply
    creations on forward (``+++ a/b``) and deletions on reverse
    (``--- a/b`` paired with ``+++ /dev/null``). Real paths are emitted
    root-relative (leading ``/`` stripped) so :func:`apply_patch_reverse`
    can invoke ``patch -d /`` and bypass GNU patch's safe-paths check.
    """
    chunks: list[str] = []
    for path in sorted(set(pre) | set(post), key=str):
        before = pre.get(path)
        after = post.get(path)
        if before == after:
            continue
        before_lines = (before or "").splitlines(keepends=True)
        after_lines = (after or "").splitlines(keepends=True)
        from_path = "/dev/null" if before is None else _diff_path(path)
        to_path = "/dev/null" if after is None else _diff_path(path)
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


@dataclass(frozen=True, slots=True)
class ExtensionDelta:
    """Net successful changes to the installed extension set during a
    state-changing command. Failed installs/uninstalls are excluded so
    revert never tries to reverse a no-op."""

    added: list[str]      # successfully installed during the command
    removed: list[str]    # successfully uninstalled during the command

    def is_empty(self) -> bool:
        return not (self.added or self.removed)


def write_transition(
    meta: TransitionMeta,
    file_pre: Mapping[Path, str | None],
    file_post: Mapping[Path, str | None],
    ext_delta: ExtensionDelta | None,
) -> Path:
    """Write a complete transition directory under :func:`transitions_root`.

    Layout:
    - ``meta.json`` — always present.
    - ``changes.patch`` — present iff :func:`compute_patch` returned non-empty.
    - ``extensions.json`` — present iff ``ext_delta`` is non-None and
      non-empty.

    Returns the absolute path of the directory written.
    """
    target = transitions_root() / transition_dirname(
        meta.timestamp, meta.command.value, meta.profile
    )
    write_meta(target, meta)

    patch = compute_patch(file_pre, file_post)
    if patch:
        (target / "changes.patch").write_text(patch, encoding="utf-8")

    if ext_delta is not None and not ext_delta.is_empty():
        payload = json.dumps(
            {"added": ext_delta.added, "removed": ext_delta.removed}, indent=2
        ) + "\n"
        (target / "extensions.json").write_text(payload, encoding="utf-8")

    return target


def load_latest(profile: str) -> Path | None:
    """Return the most recent transition directory for ``profile``,
    or ``None`` if no history exists.

    Sorts lexicographically; transition_dirname's UTC-ISO prefix makes
    that equivalent to chronological order.
    """
    root = transitions_root()
    if not root.exists():
        return None
    candidates = [
        d for d in root.iterdir()
        if d.is_dir() and d.name.endswith(f"-{profile}")
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda d: d.name)


def apply_patch_reverse(transition_dir: Path) -> None:
    """Apply ``<transition_dir>/changes.patch`` in reverse via ``patch -R``.

    No-op if the patch file is absent (e.g. transition recorded only an
    extension delta).

    Atomicity: a ``--dry-run`` pass runs first so drift on any single
    file aborts before any file is written. ``--reject-file=-`` discards
    rejected hunks (would otherwise leave ``.rej`` siblings in the
    user's tree). On a clean dry-run, the real apply follows.

    Raises :class:`RevertFailed` if the ``patch`` binary is missing or
    if either pass fails. The patch's stderr is surfaced verbatim so
    the user sees the conflicting paths.
    """
    patch_file = transition_dir / "changes.patch"
    if not patch_file.exists():
        return
    patch_bin = shutil.which("patch")
    if patch_bin is None:
        raise RevertFailed(
            "`patch` binary not on PATH; revert cannot apply file diffs"
        )
    # Run with cwd=/ and -p0 so root-relative paths in the diff
    # (per :func:`_diff_path`) resolve to absolute targets.
    base_args = [
        patch_bin,
        "-p0",
        "-R",
        "-d", "/",
        "--reject-file=-",
        "--input", str(patch_file.resolve()),
    ]
    dry = subprocess.run(
        [*base_args, "--dry-run"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if dry.returncode != 0:
        raise RevertFailed(
            f"patch -R dry-run failed (exit {dry.returncode}); no files changed:\n"
            f"{dry.stderr.strip() or dry.stdout.strip()}"
        )
    result = subprocess.run(
        base_args,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        # Should not happen after a clean dry-run; surface for forensics.
        raise RevertFailed(
            f"patch -R failed unexpectedly after dry-run succeeded "
            f"(exit {result.returncode}):\n"
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
