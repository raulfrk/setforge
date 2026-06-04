"""Tests for setforge.locking.profile_lock."""

import fcntl
from pathlib import Path

import pytest

from setforge.errors import SetforgeError
from setforge.locking import profile_lock
from setforge.transitions import state_root


@pytest.fixture(autouse=True)
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    return tmp_path


def test_lock_creates_lockfile_and_runs_body() -> None:
    """profile_lock("p") creates state_root()/locks/p.lock and the body runs."""
    executed: list[bool] = []
    with profile_lock("p"):
        lock_path = state_root() / "locks" / "p.lock"
        assert lock_path.exists(), "lockfile must exist while the lock is held"
        executed.append(True)
    assert executed == [True]


def test_lock_released_after_exit() -> None:
    """After the ``with`` block the fd is released; a second acquire succeeds."""
    with profile_lock("p"):
        pass
    # If the fd leaked we'd hang here because flock(LOCK_EX) on the same
    # file from the same process would block (flock is per open-file-
    # description, not per-path, so a second open + LOCK_NB below would
    # still succeed even with a leak — but open + LOCK_EX + LOCK_NB from
    # a second fd is the correct re-entrant test).
    lock_path = state_root() / "locks" / "p.lock"
    fd = lock_path.open("a")
    try:
        # LOCK_NB: if the lock were still held this would raise BlockingIOError
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        # If we get here the fd is free — release it.
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    finally:
        fd.close()


def test_timeout_raises_on_contention(state_dir: Path) -> None:
    """Lock held by another fd: profile_lock(..., timeout=0.2) raises SetforgeError."""
    lock_path = state_dir / "locks" / "p.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch()

    # Hold the lock ourselves via a second fd (flock is per open-file-
    # description, so opening a second fd and locking it is exactly the
    # in-process contention signal the poll path sees).
    holder = lock_path.open("a")
    try:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        with pytest.raises(  # noqa: SIM117 — outer raises context cannot be merged with inner lock
            SetforgeError, match="another setforge process holds the lock"
        ):
            with profile_lock("p", timeout=0.2):
                pass
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()


def test_different_profiles_do_not_block_each_other(state_dir: Path) -> None:
    """profile_lock("a") held, profile_lock("b", timeout=0.2) must succeed."""
    locks_dir = state_dir / "locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    lock_a = locks_dir / "a.lock"
    lock_a.touch()

    holder = lock_a.open("a")
    try:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        # "b" is a different lockfile — should not be affected.
        executed: list[bool] = []
        with profile_lock("b", timeout=0.2):
            executed.append(True)
        assert executed == [True]
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()
