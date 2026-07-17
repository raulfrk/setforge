"""Profile-scoped advisory lock for setforge state-mutating commands.

Serializes ``install``, ``sync``, and ``compare`` runs on the same profile
so their access to the stored-base and transition state under
``state_root()`` does not interleave: ``install`` / ``sync`` write that
state, while ``compare`` only reads it and takes the lock to get a
consistent snapshot rather than a half-written one.

The lockfile lives at ``state_root() / "locks" / "<profile>.lock"``.  On
POSIX, ``fcntl.flock(fd, LOCK_EX)`` is kernel-mediated: the OS releases the
lock automatically when the fd is closed, even on process crash — no stale
lockfiles.

Blocking vs. timeout:
    Default (``timeout=None``) calls ``flock(LOCK_EX)`` directly — the
    kernel blocks until the lock is available.  This is the right default for
    production (a second ``setforge install`` should wait, not silently
    corrupt state).

    When ``timeout`` is set, the implementation polls with ``LOCK_EX |
    LOCK_NB`` and short sleeps (``_POLL_INTERVAL`` seconds) until the timeout
    expires, then raises :class:`~setforge.errors.SetforgeError`.  This path
    exists to make the contention case testable in-process without an
    unbounded hang.
"""

import fcntl
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from setforge.errors import SetforgeError
from setforge.transitions import state_root

_POLL_INTERVAL: float = 0.05  # seconds between LOCK_NB retries


@contextmanager
def profile_lock(profile: str, timeout: float | None = None) -> Iterator[None]:
    """Acquire an exclusive advisory lock scoped to ``profile``.

    Creates ``state_root() / "locks" / "<profile>.lock"`` (including parent
    dirs) and calls ``fcntl.flock(LOCK_EX)`` on the open file descriptor.
    The lock is held for the duration of the ``with`` body and released
    (``LOCK_UN`` + fd close) on normal exit or exception.

    Args:
        profile: Profile name; determines the lockfile basename.
        timeout: If ``None`` (default), block indefinitely until the lock
            is available.  If set, poll every ``_POLL_INTERVAL`` seconds
            for up to ``timeout`` seconds and raise :class:`SetforgeError`
            on contention.

    Raises:
        SetforgeError: When ``timeout`` is set and the lock cannot be
            acquired within the deadline.
    """
    # Capture state_root() once at acquire time; do not re-read it inside the
    # body so a $SETFORGE_STATE_DIR change mid-lock cannot shift the path.
    locks_dir: Path = state_root() / "locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    lock_path: Path = locks_dir / f"{profile}.lock"

    fd = lock_path.open("a")
    try:
        if timeout is None:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise SetforgeError(
                            f"another setforge process holds the lock for profile "
                            f"{profile!r}; retry shortly"
                        ) from None
                    time.sleep(_POLL_INTERVAL)
        try:
            yield
        finally:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    finally:
        fd.close()


@contextmanager
def lockfile_lock(config_dir: Path, timeout: float | None = None) -> Iterator[None]:
    """Acquire an exclusive advisory lock scoped to a config dir's ``setforge.lock``.

    The committed ``setforge.lock`` is a single file shared across ALL profiles
    (``lock_path`` has no profile component), so ``setforge lock --profile=A``
    and ``--profile=B`` running concurrently both read the same baseline, merge
    only their own profile's pins, then write — the second write clobbers the
    first (silent lost update).  This lock serializes the whole
    load -> merge -> write critical section on the config dir, so a second
    writer observes the first writer's pins and ``merge_lock`` unions them.

    Unlike :func:`profile_lock`, this lock is keyed on the config DIR, not the
    profile name — a profile-scoped lock would not serialize A vs B against the
    profile-independent lockfile.

    Creates ``<config-dir>/setforge.lock.lock`` (a sidecar; never the lockfile
    itself, which is atomically replaced) and calls ``fcntl.flock(LOCK_EX)`` on
    the open fd, released on normal exit or exception.

    Args:
        config_dir: Directory holding ``setforge.lock``; determines the sidecar
            lockfile location.
        timeout: If ``None`` (default), block indefinitely.  If set, poll every
            ``_POLL_INTERVAL`` seconds up to ``timeout`` seconds and raise
            :class:`SetforgeError` on contention.

    Raises:
        SetforgeError: When ``timeout`` is set and the lock cannot be acquired
            within the deadline.
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    lock_path: Path = config_dir / "setforge.lock.lock"

    fd = lock_path.open("a")
    try:
        if timeout is None:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise SetforgeError(
                            f"another setforge process holds the setforge.lock "
                            f"lock for {config_dir}; retry shortly"
                        ) from None
                    time.sleep(_POLL_INTERVAL)
        try:
            yield
        finally:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    finally:
        fd.close()
