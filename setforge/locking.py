"""Ordered advisory locks for SetForge reads and mutations.

Commands declare the namespaces they touch and acquire them in the sole legal
order: global mutation gate, user-global resources, canonical config repository,
then profile state.
The rank guard rejects in-process inversions, while POSIX ``flock`` serializes
independent processes and releases automatically when a process exits.

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
import hashlib
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from enum import IntEnum
from pathlib import Path

from setforge.errors import SetforgeError
from setforge.transitions import state_root

_POLL_INTERVAL: float = 0.05  # seconds between LOCK_NB retries


class LockRank(IntEnum):
    """Canonical acquisition order for SetForge mutation locks."""

    MUTATION = 0
    RESOURCES = 10
    CONFIG = 20
    PROFILE = 30


_HELD_RANKS: ContextVar[tuple[tuple[LockRank, str], ...]] = ContextVar(
    "setforge_held_lock_ranks", default=()
)


@contextmanager
def _ranked(rank: LockRank, key: str) -> Iterator[None]:
    """Reject an in-process lock-order inversion before it can deadlock."""
    held = _HELD_RANKS.get()
    if held and (rank < held[-1][0] or (rank == held[-1][0] and key <= held[-1][1])):
        raise SetforgeError(
            f"duplicate or inverted lock order: requested {rank.name.lower()} after "
            f"{held[-1][0].name.lower()}; acquire mutation -> resources -> "
            "config -> profile "
            "and same-rank locks by sorted identity"
        )
    token = _HELD_RANKS.set((*held, (rank, key)))
    try:
        yield
    finally:
        _HELD_RANKS.reset(token)


def _user_global_locks_dir() -> Path:
    """Return the lock namespace shared by one user's external resources."""
    return Path("~/.cache/setforge/locks").expanduser()


def _profile_lock_path(profile: str) -> Path:
    """Return a traversal-safe lock path for an arbitrary profile name."""
    digest = hashlib.sha256(profile.encode("utf-8")).hexdigest()[:24]
    return state_root() / "locks" / f"profile-{digest}.lock"


@contextmanager
def _mutation_gate_lock(timeout: float | None = None) -> Iterator[None]:
    """Serialize the refusal check and publication boundary for all mutations.

    A journal cannot protect the interval before it is durably published.  This
    user-global gate makes that interval exclusive across profiles, config repos,
    and transition-state overrides while still allowing an operation to acquire
    its narrower resource/config/profile locks in canonical order.
    """
    with _ranked(LockRank.MUTATION, "mutation-gate"):
        locks_dir = _user_global_locks_dir()
        locks_dir.mkdir(parents=True, exist_ok=True)
        lock_path = locks_dir / "mutation-gate.lock"
        fd = lock_path.open("a")
        try:
            _acquire_fd(
                fd,
                timeout=timeout,
                timeout_message=(
                    "another setforge command holds the global mutation gate; "
                    "retry shortly"
                ),
            )
            try:
                yield
            finally:
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        finally:
            fd.close()


@contextmanager
def profile_lock(profile: str, timeout: float | None = None) -> Iterator[None]:
    """Acquire an exclusive advisory lock scoped to ``profile``.

    Creates a digest-named sidecar under ``state_root() / "locks"`` and calls
    ``fcntl.flock(LOCK_EX)`` on the open file descriptor. Digest naming keeps
    nested or traversal-like profile text from changing the lock namespace.
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
    with _ranked(LockRank.PROFILE, profile):
        # Capture state_root() once at acquire time; do not re-read it inside the
        # body so a $SETFORGE_STATE_DIR change mid-lock cannot shift the path.
        lock_path = _profile_lock_path(profile)
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        fd = lock_path.open("a")
        try:
            _acquire_fd(
                fd,
                timeout=timeout,
                timeout_message=(
                    f"another setforge process holds the lock for profile "
                    f"{profile!r}; retry shortly"
                ),
            )
            try:
                yield
            finally:
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        finally:
            fd.close()


@contextmanager
def install_resources_lock(timeout: float | None = None) -> Iterator[None]:
    """Serialize global package and adapter planning/apply across commands.

    When a command also needs a profile lock, acquire this global lock first.
    Its path follows the user's global Claude/VSCode/cache namespace rather
    than ``SETFORGE_STATE_DIR``; alternate transition roots must not split the
    lock protecting the same external inventories.
    """
    with _ranked(LockRank.RESOURCES, "install-resources"):
        locks_dir = _user_global_locks_dir()
        locks_dir.mkdir(parents=True, exist_ok=True)
        lock_path = locks_dir / "install-resources.lock"
        fd = lock_path.open("a")
        try:
            _acquire_fd(
                fd,
                timeout=timeout,
                timeout_message=(
                    "another setforge command holds the global resource lock; "
                    "retry shortly"
                ),
            )
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

    Creates a path-keyed sidecar in the user-global lock namespace (never
    inside the config repository, never under operator-variable transition
    state, and never the lockfile itself, which is atomically replaced) and
    calls ``fcntl.flock(LOCK_EX)`` on the open fd.

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
    config_key = str(config_dir.resolve())
    with _ranked(LockRank.CONFIG, config_key):
        locks_dir = _user_global_locks_dir()
        locks_dir.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(config_key.encode()).hexdigest()[:24]
        lock_path = locks_dir / f"config-{key}.lock"

        fd = lock_path.open("a")
        try:
            _acquire_fd(
                fd,
                timeout=timeout,
                timeout_message=(
                    f"another setforge process holds the setforge.lock/config "
                    f"lock for {config_dir}; retry shortly"
                ),
            )
            try:
                yield
            finally:
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        finally:
            fd.close()


def _acquire_fd(fd: object, *, timeout: float | None, timeout_message: str) -> None:
    """Acquire one flock, optionally with the shared bounded-poll contract."""
    fileno = fd.fileno()  # type: ignore[attr-defined]
    if timeout is None:
        fcntl.flock(fileno, fcntl.LOCK_EX)
        return
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fileno, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise SetforgeError(timeout_message) from None
            time.sleep(_POLL_INTERVAL)


@contextmanager
def mutation_locks(
    *,
    resources: bool = False,
    config_dir: Path | None = None,
    config_dirs: tuple[Path, ...] = (),
    profile: str | None = None,
    profiles: tuple[str, ...] = (),
    timeout: float | None = None,
    allow_operation_id: str | None = None,
) -> Iterator[None]:
    """Acquire requested mutation locks in canonical rank order.

    The order is global mutation gate, user-global resources, canonical config
    repository, then profile state. Callers declare scopes instead of spelling
    nested context managers, making the ordering contract structural and
    reviewable. The gate also closes the pre-journal-publication race for
    migrations that later acquire several concrete profile locks.
    """
    with ExitStack() as stack:
        stack.enter_context(_mutation_gate_lock(timeout=timeout))
        if resources:
            stack.enter_context(install_resources_lock(timeout=timeout))
        requested_config_dirs = tuple(
            sorted(
                {
                    *(path.resolve() for path in config_dirs),
                    *((config_dir.resolve(),) if config_dir is not None else ()),
                },
                key=str,
            )
        )
        for requested_config_dir in requested_config_dirs:
            stack.enter_context(lockfile_lock(requested_config_dir, timeout=timeout))
        requested_profiles = tuple(
            sorted({*profiles, *((profile,) if profile is not None else ())})
        )
        for requested_profile in requested_profiles:
            stack.enter_context(profile_lock(requested_profile, timeout=timeout))
        from setforge import operations

        operations.refuse_conflicting_mutation(
            resources=resources,
            config_dir=None,
            profile=profile,
            profiles=requested_profiles,
            allow_operation_id=allow_operation_id,
        )
        for requested_config_dir in requested_config_dirs:
            operations.refuse_conflicting_mutation(
                resources=False,
                config_dir=requested_config_dir,
                profile=None,
                allow_operation_id=allow_operation_id,
            )
        yield
