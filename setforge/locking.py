"""Profile-scoped advisory lock for setforge state-mutating commands.

Prevents concurrent ``install``, ``sync``, or ``compare`` runs on the same
profile from interleaving their reads and writes to the stored-base and
transition state under ``state_root()``.

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
