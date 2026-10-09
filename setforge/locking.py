"""Ordered advisory locks for SetForge reads and mutations.

Every command that changes SetForge state, a managed file, a package, an
adapter or a config repository holds the user-global mutation gate for its
whole run, so the gate is the one writer lock: no two such mutations overlap,
whatever they touch. ``completion install`` is the one command that writes
without it: it touches only the completion script and the shell rc file, takes
no lock and is not refused by an unfinished operation. Two narrower locks
remain because commands that do not take the gate also take them: the profile
lock (``compare`` and ``inspect`` read under it)
and the config-identity lock (checkout identity is created on read paths too).
The sole legal order is gate, config identity, then profile state.
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

import errno
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
from typing import TypedDict

from setforge.errors import SetforgeError
from setforge.paths import cache_root, state_root

_POLL_INTERVAL: float = 0.05  # seconds between LOCK_NB retries


class LockRank(IntEnum):
    """Canonical acquisition order for SetForge mutation locks."""

    MUTATION = 0
    CONFIG_IDENTITY = 15
    PROFILE = 30


_HELD_RANKS: ContextVar[tuple[tuple[LockRank, str], ...]] = ContextVar(
    "setforge_held_lock_ranks", default=()
)
# Set while ``mutation_locks(resources=True)`` holds the gate for its caller.
_RESOURCES_DECLARED: ContextVar[bool] = ContextVar(
    "setforge_resources_declared", default=False
)


@contextmanager
def _ranked(rank: LockRank, key: str) -> Iterator[None]:
    """Reject an in-process lock-order inversion before it can deadlock."""
    held = _HELD_RANKS.get()
    if held and (rank < held[-1][0] or (rank == held[-1][0] and key <= held[-1][1])):
        raise SetforgeError(
            f"duplicate or inverted lock order: requested {rank.name.lower()} after "
            f"{held[-1][0].name.lower()}; acquire mutation -> config-identity -> "
            "profile and same-rank locks by sorted identity"
        )
    token = _HELD_RANKS.set((*held, (rank, key)))
    try:
        yield
    finally:
        _HELD_RANKS.reset(token)


def mutation_gate_held() -> bool:
    """Whether the calling context runs inside the mutation gate."""
    return any(rank is LockRank.MUTATION for rank, _ in _HELD_RANKS.get())


def _user_global_locks_dir() -> Path:
    """Return the lock namespace shared by one user's external resources."""
    return cache_root() / "locks"


def require_resources_lock() -> None:
    """Refuse a resource mutation outside ``mutation_locks(resources=True)``."""
    if not _RESOURCES_DECLARED.get():
        raise SetforgeError("ownership mutation requires the global resource lock")


@dataclass(frozen=True, slots=True)
class ConfigIdentityGuard:
    """Verified Git common-directory descriptor held through publication."""

    common_dir: Path
    directory_fd: int


@dataclass(frozen=True, slots=True)
class TargetLockRequest:
    """One target root whose parent must already exist."""

    target: Path


@dataclass(frozen=True, slots=True)
class _TargetLockSnapshot:
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

    def rmdir_if_empty(self) -> bool:
        """Remove the verified target only when empty, preserving other effects."""
        self.verify_expected()
        if self.target_fd is None:
            return False
        try:
            os.rmdir(self.target.name, dir_fd=self.parent_fd)
        except OSError as exc:
            if exc.errno in {errno.ENOTEMPTY, errno.EEXIST}:
                return False
            raise
        self.close()
        self.expected_target_identity = None
        os.fsync(self.parent_fd)
        return True

    def close(self) -> None:
        """Close the optional target descriptor retained across publication."""
        if self.target_fd is not None:
            os.close(self.target_fd)
            self.target_fd = None


def _filesystem_identity(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino


def _config_identity_key(path: Path) -> str:
    """Return the same ordering key used by ``config_identity_lock``."""
    device, inode = _filesystem_identity(path)
    return f"{device}:{inode}"


def _target_snapshot(request: TargetLockRequest) -> _TargetLockSnapshot:
    target = request.target.absolute()
    if not target.name:
        raise SetforgeError("target lock requires a non-root path")
    parent = target.parent.resolve(strict=True)
    parent_identity = _filesystem_identity(parent)
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
    else:
        target_identity = _filesystem_identity(resolved_target)
    return _TargetLockSnapshot(
        parent=parent,
        parent_identity=parent_identity,
        target=target,
        target_identity=target_identity,
    )


@contextmanager
def _flock(
    path: Path, *, timeout: float | None, timeout_message: str
) -> Iterator[None]:
    """Hold an exclusive flock on ``path``, creating the lock file if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fd:
        _acquire_fd(fd, timeout=timeout, timeout_message=timeout_message)
        try:
            yield
        finally:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)


@contextmanager
def _global_named_lock(
    *, rank: LockRank, key: str, prefix: str, timeout: float | None
) -> Iterator[None]:
    digest = hashlib.sha256(key.encode()).hexdigest()[:24]
    with (
        _ranked(rank, key),
        _flock(
            _user_global_locks_dir() / f"{prefix}-{digest}.lock",
            timeout=timeout,
            timeout_message=f"another setforge process holds the {prefix} lock",
        ),
    ):
        yield


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
def target_guards(
    requests: tuple[TargetLockRequest, ...],
) -> Iterator[tuple[TargetLockGuard, ...]]:
    """Bind target roots to descriptors and re-verify them when the body ends.

    This takes no lock: the mutation gate keeps other SetForge writers out. The
    guards detect a root that anything else replaces during the operation.
    """
    snapshots = tuple(_target_snapshot(request) for request in requests)
    with ExitStack() as stack:
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
            raise SetforgeError("target parent changed while binding target roots")
        try:
            current_identity = _filesystem_identity(
                snapshot.target.resolve(strict=True)
            )
        except FileNotFoundError:
            current_identity = None
        if current_identity != snapshot.target_identity:
            raise SetforgeError("target changed while binding target roots; retry")
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
    config_identity: ConfigIdentityGuard | None = None
    config_identities: tuple[ConfigIdentityGuard, ...] = ()

    def verify_targets(self) -> None:
        """Revalidate every target immediately before coupled publication."""
        for target in self.targets:
            target.verify_expected()


class MutationScopes(TypedDict, total=False):
    """Lock scopes of ``operations.transaction``; see ``mutation_locks``."""

    resources: bool
    config_identity_dir: Path | None
    config_dir: Path | None
    target_roots: tuple[Path, ...]
    profile: str | None
    profiles: tuple[str, ...]


def _profile_lock_path(profile: str) -> Path:
    """Return a traversal-safe lock path for an arbitrary profile name."""
    digest = hashlib.sha256(profile.encode("utf-8")).hexdigest()[:24]
    return state_root() / "locks" / f"profile-{digest}.lock"


@contextmanager
def _mutation_gate_lock(timeout: float | None = None) -> Iterator[None]:
    """Hold the one writer lock every mutating command runs under.

    The gate is user-global: it serializes mutations across profiles, config
    repos, target roots, external inventories, and transition-state overrides,
    including the interval before a journal is durably published.
    """
    with (
        _ranked(LockRank.MUTATION, "mutation-gate"),
        _flock(
            _user_global_locks_dir() / "mutation-gate.lock",
            timeout=timeout,
            timeout_message=(
                "another setforge command holds the global mutation gate; retry shortly"
            ),
        ),
    ):
        yield


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
    # state_root() is read once, at acquire time, so a $SETFORGE_STATE_DIR
    # change mid-lock cannot shift the path.
    with (
        _ranked(LockRank.PROFILE, profile),
        _flock(
            _profile_lock_path(profile),
            timeout=timeout,
            timeout_message=(
                f"another setforge process holds the lock for profile "
                f"{profile!r}; retry shortly"
            ),
        ),
    ):
        yield


def _acquire_fd(fd: object, *, timeout: float | None, timeout_message: str) -> None:
    """Acquire one flock, optionally with the shared bounded-poll contract."""
    fileno = fd.fileno()  # type: ignore[attr-defined]
    try:
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
    except OSError as exc:
        raise SetforgeError(
            f"cannot lock {getattr(fd, 'name', 'lock file')}: "
            f"{exc.strerror or exc}; the filesystem refused the lock request "
            "(on a network filesystem its lock service must be reachable)"
        ) from exc


@contextmanager
def mutation_locks(
    *,
    resources: bool = False,
    config_identity_dir: Path | None = None,
    config_identity_dirs: tuple[Path, ...] = (),
    config_dir: Path | None = None,
    config_dirs: tuple[Path, ...] = (),
    target_roots: tuple[Path, ...] = (),
    profile: str | None = None,
    profiles: tuple[str, ...] = (),
    timeout: float | None = None,
    allow_operation_id: str | None = None,
) -> Iterator[MutationLockGuards]:
    """Acquire the mutation gate, then the narrower locks, in canonical order.

    The order is global mutation gate, optional verified Git common-directory
    identity, then profile state. The gate alone excludes every other mutation;
    ``resources``, config directories, and target roots take no lock of their
    own. ``resources`` lets the body mutate ownership, target roots yield
    descriptor guards, and ``config_dir``/``config_dirs`` are accepted but have
    no effect. Callers declare scopes instead of spelling nested context
    managers, making the ordering contract structural and reviewable.

    An unfinished operation refuses every mutation, whatever its scopes; only
    recovery passes that operation's ``allow_operation_id``.
    """
    with ExitStack() as stack:
        stack.enter_context(_mutation_gate_lock(timeout=timeout))
        if resources:
            stack.callback(_RESOURCES_DECLARED.reset, _RESOURCES_DECLARED.set(True))
        requested_identity_dirs = tuple(
            sorted(
                {
                    *(path.resolve(strict=True) for path in config_identity_dirs),
                    *(
                        (config_identity_dir.resolve(strict=True),)
                        if config_identity_dir is not None
                        else ()
                    ),
                },
                key=_config_identity_key,
            )
        )
        identity_guards: list[ConfigIdentityGuard] = []
        for resolved_identity_dir in requested_identity_dirs:
            identity_fd = stack.enter_context(
                config_identity_lock(resolved_identity_dir, timeout=timeout)
            )
            identity_guards.append(
                ConfigIdentityGuard(
                    resolved_identity_dir,
                    identity_fd,
                )
            )
        requested_targets = tuple(
            TargetLockRequest(path)
            for path in sorted({path.absolute() for path in target_roots}, key=str)
        )
        bound_targets: tuple[TargetLockGuard, ...] = ()
        if requested_targets:
            bound_targets = stack.enter_context(target_guards(requested_targets))
        requested_profiles = tuple(
            sorted({*profiles, *((profile,) if profile is not None else ())})
        )
        for requested_profile in requested_profiles:
            stack.enter_context(profile_lock(requested_profile, timeout=timeout))
        from setforge import operations

        operations.refuse_pending(allow_operation_id=allow_operation_id)
        yield MutationLockGuards(
            bound_targets,
            identity_guards[0] if len(identity_guards) == 1 else None,
            tuple(identity_guards),
        )
