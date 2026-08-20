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
import os
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

from setforge.errors import SetforgeError
from setforge.transitions import state_root

_POLL_INTERVAL: float = 0.05  # seconds between LOCK_NB retries


class LockRank(IntEnum):
    """Canonical acquisition order for SetForge mutation locks."""

    MUTATION = 0
    RESOURCES = 10
    CONFIG_IDENTITY = 15
    CONFIG = 20
    TARGET = 25
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
            "config-identity -> config -> target -> profile "
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


def require_resources_lock() -> None:
    """Refuse a resource mutation outside the canonical resource lock."""
    if not any(rank is LockRank.RESOURCES for rank, _key in _HELD_RANKS.get()):
        raise SetforgeError("ownership mutation requires the global resource lock")


@dataclass(frozen=True, slots=True)
class TargetLockRequest:
    """One target root whose parent must already exist."""

    target: Path


@dataclass(frozen=True, slots=True)
class _TargetLockSnapshot:
    coordinate_key: str
    object_key: str | None
    parent: Path
    parent_identity: tuple[int, int]
    target: Path
    target_identity: tuple[int, int] | None


@dataclass(slots=True)
class TargetLockGuard:
    """Descriptor-bound target coordinate held for guarded publication."""

    target: Path
    parent_fd: int
    parent_identity: tuple[int, int]
    expected_target_identity: tuple[int, int] | None
    target_fd: int | None = None

    def mkdir(self, mode: int = 0o777) -> None:
        """Create the target leaf relative to the verified parent descriptor."""
        self.verify_expected()
        os.mkdir(self.target.name, mode=mode, dir_fd=self.parent_fd)
        os.fsync(self.parent_fd)
        self.target_fd = os.open(
            self.target.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=self.parent_fd,
        )
        info = os.fstat(self.target_fd)
        self.expected_target_identity = info.st_dev, info.st_ino

    def verify(self) -> tuple[int, int] | None:
        """Verify the held parent and return the current target identity."""
        parent_info = os.fstat(self.parent_fd)
        if (parent_info.st_dev, parent_info.st_ino) != self.parent_identity:
            raise SetforgeError("held target parent descriptor changed")
        try:
            lexical_parent = _filesystem_identity(
                self.target.parent.resolve(strict=True)
            )
        except FileNotFoundError:
            lexical_parent = None
        if lexical_parent != self.parent_identity:
            raise SetforgeError("target parent binding changed while locked")
        try:
            info = os.stat(
                self.target.name, dir_fd=self.parent_fd, follow_symlinks=True
            )
        except FileNotFoundError:
            return None
        return info.st_dev, info.st_ino

    def verify_expected(self) -> None:
        """Refuse if the target no longer has the permitted identity."""
        if self.target_fd is not None:
            held = os.fstat(self.target_fd)
            if (held.st_dev, held.st_ino) != self.expected_target_identity:
                raise SetforgeError("held target descriptor changed")
        if self.verify() != self.expected_target_identity:
            raise SetforgeError("target changed while target lock was held")

    def close(self) -> None:
        """Close the optional target descriptor retained across publication."""
        if self.target_fd is not None:
            os.close(self.target_fd)
            self.target_fd = None


def _filesystem_identity(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino


def _target_snapshot(request: TargetLockRequest) -> _TargetLockSnapshot:
    target = request.target.absolute()
    if not target.name:
        raise SetforgeError("target lock requires a non-root path")
    parent = target.parent.resolve(strict=True)
    parent_identity = _filesystem_identity(parent)
    coordinate = f"{parent_identity[0]}:{parent_identity[1]}:{target.name}"
    try:
        leaf_info = target.lstat()
    except FileNotFoundError:
        leaf_info = None
    try:
        resolved_target = target.resolve(strict=True)
    except FileNotFoundError:
        if leaf_info is not None:
            raise SetforgeError(
                f"target root is a dangling symlink: {target}"
            ) from None
        target_identity = None
        object_key = None
    else:
        target_identity = _filesystem_identity(resolved_target)
        object_key = f"{target_identity[0]}:{target_identity[1]}"
    return _TargetLockSnapshot(
        coordinate_key=f"coordinate:{coordinate}",
        object_key=f"object:{object_key}" if object_key is not None else None,
        parent=parent,
        parent_identity=parent_identity,
        target=target,
        target_identity=target_identity,
    )


@contextmanager
def _global_named_lock(
    *, rank: LockRank, key: str, prefix: str, timeout: float | None
) -> Iterator[None]:
    with _ranked(rank, key):
        locks_dir = _user_global_locks_dir()
        locks_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(key.encode()).hexdigest()[:24]
        fd = (locks_dir / f"{prefix}-{digest}.lock").open("a")
        try:
            _acquire_fd(
                fd,
                timeout=timeout,
                timeout_message=f"another setforge process holds the {prefix} lock",
            )
            try:
                yield
            finally:
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        finally:
            fd.close()


@contextmanager
def config_identity_lock(
    common_dir: Path, timeout: float | None = None
) -> Iterator[int]:
    """Serialize checkout UUID creation by verified Git common-directory identity."""
    resolved = common_dir.resolve(strict=True)
    device, inode = _filesystem_identity(resolved)
    key = f"{device}:{inode}"
    with _global_named_lock(
        rank=LockRank.CONFIG_IDENTITY,
        key=key,
        prefix="config-identity",
        timeout=timeout,
    ):
        descriptor = os.open(
            resolved, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        try:
            info = os.fstat(descriptor)
            if (info.st_dev, info.st_ino) != (device, inode):
                raise SetforgeError(
                    "Git common directory changed while acquiring its lock"
                )
            yield descriptor
            if _filesystem_identity(resolved) != (device, inode):
                raise SetforgeError("Git common directory changed while locked")
        finally:
            os.close(descriptor)


@contextmanager
def target_locks(
    requests: tuple[TargetLockRequest, ...], timeout: float | None = None
) -> Iterator[tuple[TargetLockGuard, ...]]:
    """Acquire stable coordinate and existing-object locks for target roots."""
    snapshots = tuple(_target_snapshot(request) for request in requests)
    lock_keys = {"0:all-targets"} if snapshots else set()
    for snapshot in snapshots:
        for key in (snapshot.coordinate_key, snapshot.object_key):
            if key is not None:
                lock_keys.add(key)
    with ExitStack() as stack:
        for key in sorted(lock_keys):
            stack.enter_context(
                _global_named_lock(
                    rank=LockRank.TARGET,
                    key=key,
                    prefix="target",
                    timeout=timeout,
                )
            )
        guards = _open_target_guards(snapshots, stack)
        yield tuple(guards)
        for guard in guards:
            guard.verify_expected()


def _open_target_guards(
    snapshots: tuple[_TargetLockSnapshot, ...], stack: ExitStack
) -> list[TargetLockGuard]:
    guards: list[TargetLockGuard] = []
    for snapshot in snapshots:
        if _filesystem_identity(snapshot.parent) != snapshot.parent_identity:
            raise SetforgeError("target parent changed while acquiring target locks")
        try:
            current_identity = _filesystem_identity(
                snapshot.target.resolve(strict=True)
            )
        except FileNotFoundError:
            current_identity = None
        if current_identity != snapshot.target_identity:
            raise SetforgeError("target changed while acquiring target locks; retry")
        parent_fd = os.open(
            snapshot.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        stack.callback(os.close, parent_fd)
        parent_info = os.fstat(parent_fd)
        if (parent_info.st_dev, parent_info.st_ino) != snapshot.parent_identity:
            raise SetforgeError("target parent changed before descriptor binding")
        target_fd = (
            os.open(
                snapshot.target.resolve(strict=True),
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            if snapshot.target_identity is not None
            else None
        )
        if target_fd is not None:
            target_info = os.fstat(target_fd)
            if (target_info.st_dev, target_info.st_ino) != snapshot.target_identity:
                os.close(target_fd)
                raise SetforgeError("target changed before descriptor binding")
        guard = TargetLockGuard(
            snapshot.target,
            parent_fd,
            snapshot.parent_identity,
            snapshot.target_identity,
            target_fd,
        )
        stack.callback(guard.close)
        guards.append(guard)
    return guards


@dataclass(frozen=True, slots=True)
class MutationLockGuards:
    """Descriptor-bound guards returned by canonical mutation lock composition."""

    targets: tuple[TargetLockGuard, ...] = ()

    def verify_targets(self) -> None:
        """Revalidate every target immediately before coupled publication."""
        for target in self.targets:
            target.verify_expected()


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
    target_roots: tuple[Path, ...] = (),
    profile: str | None = None,
    profiles: tuple[str, ...] = (),
    timeout: float | None = None,
    allow_operation_id: str | None = None,
) -> Iterator[MutationLockGuards]:
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
        requested_targets = tuple(
            TargetLockRequest(path)
            for path in sorted({path.absolute() for path in target_roots}, key=str)
        )
        target_guards: tuple[TargetLockGuard, ...] = ()
        if requested_targets:
            target_guards = stack.enter_context(
                target_locks(requested_targets, timeout=timeout)
            )
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
        yield MutationLockGuards(target_guards)
