"""Typed inventory, planning, persistence, and apply for managed trees."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import stat
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

import pathspec

from setforge import atomicio
from setforge.config import TreeOrphanPolicy, TreePolicy, TreeSymlinkPolicy
from setforge.errors import InvariantViolation, SetforgeError
from setforge.reconcile.types import file_id
from setforge.transitions import state_root

_SCHEMA = "1.0"
_DIR_MODE = 0o700
_FILE_MODE = 0o600
_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2
_RESERVED_SUFFIXES = tuple(
    f".setforge-{item}" for item in ("create", "update", "remove")
)


def temporary_entry_name(name: str, purpose: str) -> str:
    """Return the deterministic journal-owned sibling for one tree effect."""
    if purpose not in {"create", "update", "remove"}:
        raise InvariantViolation(f"unknown managed tree temporary purpose: {purpose}")
    return f".{name}.setforge-{purpose}"


class TreeEntryKind(StrEnum):
    """Supported no-follow directory entry kinds."""

    DIRECTORY = "directory"
    FILE = "file"
    SYMLINK = "symlink"


@dataclass(frozen=True, slots=True, order=True)
class TreeEntry:
    """One canonical relative entry in a managed tree inventory."""

    path: str
    kind: TreeEntryKind
    mode: int
    content_hash: str | None = None
    link_target: str | None = None

    def __post_init__(self) -> None:
        relative = PurePosixPath(self.path)
        if (
            not self.path
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise InvariantViolation(f"unsafe tree inventory path: {self.path!r}")
        if not 0 <= self.mode <= 0o7777 or self.mode & 0o6000:
            raise InvariantViolation(
                f"unsafe tree inventory mode for {self.path!r}: {oct(self.mode)}"
            )
        if self.kind is TreeEntryKind.FILE:
            if self.content_hash is None or self.link_target is not None:
                raise InvariantViolation("tree file entry has invalid payload fields")
        elif self.kind is TreeEntryKind.SYMLINK:
            if self.link_target is None or self.content_hash is not None:
                raise InvariantViolation(
                    "tree symlink entry has invalid payload fields"
                )
        elif self.content_hash is not None or self.link_target is not None:
            raise InvariantViolation("tree directory entry has payload fields")


@dataclass(frozen=True, slots=True)
class TreeInventory:
    """Canonical root metadata and ordered entries for one tree."""

    root_present: bool
    root_mode: int | None
    entries: tuple[TreeEntry, ...]
    fingerprint: str
    owned_paths: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class FrozenTree:
    """Inventory plus frozen regular-file bytes used by apply."""

    inventory: TreeInventory
    payloads: tuple[tuple[str, bytes], ...] = ()

    def payload_map(self) -> dict[str, bytes]:
        return dict(self.payloads)


class TreeActionKind(StrEnum):
    """One planned per-entry tree effect or hold."""

    CREATE = "create"
    UPDATE = "update"
    CHMOD = "chmod"
    REMOVE = "remove"
    KEEP = "keep"
    HOLD = "hold"


@dataclass(frozen=True, slots=True)
class TreeAction:
    """One deterministic action for a relative path."""

    path: str
    kind: TreeActionKind
    detail: str


@dataclass(frozen=True, slots=True)
class TreePlan:
    """Frozen desired/live/prior tree comparison."""

    desired: FrozenTree
    live: TreeInventory
    prior: TreeInventory | None
    actions: tuple[TreeAction, ...]

    @property
    def blocked(self) -> bool:
        return any(action.kind is TreeActionKind.HOLD for action in self.actions)

    @property
    def changed(self) -> bool:
        return any(
            action.kind
            in {
                TreeActionKind.CREATE,
                TreeActionKind.UPDATE,
                TreeActionKind.CHMOD,
                TreeActionKind.REMOVE,
            }
            for action in self.actions
        )


def _inventory_fingerprint(
    *, root_present: bool, root_mode: int | None, entries: tuple[TreeEntry, ...]
) -> str:
    payload = {
        "entries": [
            {
                "content_hash": entry.content_hash,
                "kind": entry.kind.value,
                "link_target": entry.link_target,
                "mode": entry.mode,
                "path": entry.path,
            }
            for entry in entries
        ],
        "root_mode": root_mode,
        "root_present": root_present,
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _excluded(spec: pathspec.PathSpec, relative: str, *, directory: bool) -> bool:
    candidate = f"{relative}/" if directory else relative
    return spec.match_file(candidate)


def _stable_file_at(
    directory_fd: int, name: str, before: os.stat_result, display: Path
) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise SetforgeError(f"tree entry changed while reading: {display}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ):
        raise SetforgeError(f"tree entry changed while reading: {display}")
    return b"".join(chunks), after


@dataclass(slots=True)
class _ScanContext:
    root_device: int
    policy: TreePolicy
    matcher: pathspec.PathSpec
    capture_payloads: bool
    entries: list[TreeEntry]
    payloads: list[tuple[str, bytes]]


def _scan_entry(  # noqa: C901 - entry kinds require distinct no-follow handling
    context: _ScanContext,
    directory_fd: int,
    directory: Path,
    name: str,
    relative_path: PurePosixPath,
) -> None:
    if name.startswith(".") and name.endswith(_RESERVED_SUFFIXES):
        raise SetforgeError(
            f"managed tree entry uses a reserved name: {directory / name}"
        )
    relative = relative_path.as_posix()
    before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    is_directory = stat.S_ISDIR(before.st_mode)
    if _excluded(context.matcher, relative, directory=is_directory):
        return
    mode = stat.S_IMODE(before.st_mode)
    path = directory / name
    if stat.S_ISREG(before.st_mode):
        payload, stable = _stable_file_at(directory_fd, name, before, path)
        context.entries.append(
            TreeEntry(
                relative,
                TreeEntryKind.FILE,
                stat.S_IMODE(stable.st_mode),
                hashlib.sha256(payload).hexdigest(),
            )
        )
        if context.capture_payloads:
            context.payloads.append((relative, payload))
        return
    if is_directory:
        if before.st_dev != context.root_device:
            raise SetforgeError(f"managed tree crosses a filesystem boundary: {path}")
        context.entries.append(TreeEntry(relative, TreeEntryKind.DIRECTORY, mode))
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        child_fd = os.open(name, flags, dir_fd=directory_fd)
        try:
            opened = os.fstat(child_fd)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise SetforgeError(f"tree directory changed while scanning: {path}")
            _walk_tree(context, child_fd, path, relative_path)
            after = os.fstat(child_fd)
        finally:
            os.close(child_fd)
        if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
            raise SetforgeError(f"tree directory changed while scanning: {path}")
        return
    if stat.S_ISLNK(before.st_mode):
        if context.policy.symlinks is TreeSymlinkPolicy.REFUSE:
            raise SetforgeError(f"managed tree contains a symlink: {path}")
        target = os.readlink(name, dir_fd=directory_fd)
        after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
            raise SetforgeError(f"tree symlink changed while scanning: {path}")
        context.entries.append(
            TreeEntry(relative, TreeEntryKind.SYMLINK, mode, link_target=target)
        )
        return
    raise SetforgeError(f"managed tree contains an unsupported entry: {path}")


def _walk_tree(
    context: _ScanContext,
    directory_fd: int,
    directory: Path,
    relative_parent: PurePosixPath,
) -> None:
    try:
        names = sorted(child.name for child in os.scandir(directory_fd))
    except OSError as exc:
        raise SetforgeError(f"cannot scan managed tree {directory}: {exc}") from exc
    for name in names:
        try:
            _scan_entry(context, directory_fd, directory, name, relative_parent / name)
        except OSError as exc:
            raise SetforgeError(
                f"managed tree entry changed while scanning: {directory / name}"
            ) from exc


def scan_tree(
    root: Path,
    policy: TreePolicy,
    *,
    capture_payloads: bool = False,
) -> FrozenTree:
    """Build a stable, no-follow tree inventory without crossing devices."""
    root = root.absolute()
    try:
        root_before = root.lstat()
    except FileNotFoundError:
        inventory = TreeInventory(
            False,
            None,
            (),
            _inventory_fingerprint(root_present=False, root_mode=None, entries=()),
        )
        return FrozenTree(inventory)
    if not stat.S_ISDIR(root_before.st_mode) or stat.S_ISLNK(root_before.st_mode):
        raise SetforgeError(f"managed tree root is not a real directory: {root}")

    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open(root, flags)
    try:
        opened = os.fstat(root_fd)
        if (opened.st_dev, opened.st_ino) != (root_before.st_dev, root_before.st_ino):
            raise SetforgeError(f"managed tree root changed while scanning: {root}")
        return _scan_tree_fd(root_fd, root, policy, capture_payloads=capture_payloads)
    finally:
        os.close(root_fd)


def _scan_tree_fd(
    root_fd: int,
    display: Path,
    policy: TreePolicy,
    *,
    capture_payloads: bool = False,
) -> FrozenTree:
    root_before = os.fstat(root_fd)
    context = _ScanContext(
        root_before.st_dev,
        policy,
        pathspec.GitIgnoreSpec.from_lines(policy.exclude),
        capture_payloads,
        [],
        [],
    )
    _walk_tree(context, root_fd, display, PurePosixPath())
    root_after = os.fstat(root_fd)
    if (root_after.st_dev, root_after.st_ino) != (
        root_before.st_dev,
        root_before.st_ino,
    ):
        raise SetforgeError(f"managed tree root changed while scanning: {display}")
    ordered = tuple(sorted(context.entries, key=lambda entry: entry.path))
    inventory = TreeInventory(
        True,
        stat.S_IMODE(root_after.st_mode),
        ordered,
        _inventory_fingerprint(
            root_present=True,
            root_mode=stat.S_IMODE(root_after.st_mode),
            entries=ordered,
        ),
    )
    return FrozenTree(inventory, tuple(sorted(context.payloads)))


def _same_content(left: TreeEntry, right: TreeEntry) -> bool:
    return (
        left.kind is right.kind
        and left.content_hash == right.content_hash
        and left.link_target == right.link_target
    )


def plan_tree(
    desired: FrozenTree,
    live: TreeInventory,
    prior: TreeInventory | None,
    policy: TreePolicy,
) -> TreePlan:
    """Plan desired/live/prior entry effects without granting authority."""
    desired_by = {entry.path: entry for entry in desired.inventory.entries}
    live_by = {entry.path: entry for entry in live.entries}
    prior_by = (
        {
            entry.path: entry
            for entry in prior.entries
            if prior.owned_paths is None or entry.path in prior.owned_paths
        }
        if prior
        else {}
    )
    actions: list[TreeAction] = []
    if not live.root_present:
        actions.append(TreeAction(".", TreeActionKind.CREATE, "tree root missing"))
    elif desired.inventory.root_mode != live.root_mode:
        actions.append(TreeAction(".", TreeActionKind.CHMOD, "tree root mode differs"))
    actions.extend(_plan_desired_entries(desired_by, live_by))
    actions.extend(_plan_live_extras(desired_by, live_by, prior_by, policy))
    return TreePlan(
        desired, live, prior, tuple(sorted(actions, key=lambda item: item.path))
    )


def _plan_desired_entries(
    desired_by: dict[str, TreeEntry], live_by: dict[str, TreeEntry]
) -> list[TreeAction]:
    actions: list[TreeAction] = []
    for path, wanted in desired_by.items():
        current = live_by.get(path)
        if current is None:
            actions.append(
                TreeAction(path, TreeActionKind.CREATE, "desired entry missing")
            )
        elif not _same_content(wanted, current):
            if wanted.kind is not current.kind:
                actions.append(
                    TreeAction(path, TreeActionKind.HOLD, "entry kind conflicts")
                )
            else:
                actions.append(
                    TreeAction(path, TreeActionKind.UPDATE, "content differs")
                )
        elif wanted.mode != current.mode:
            actions.append(TreeAction(path, TreeActionKind.CHMOD, "mode differs"))
    return actions


def _plan_live_extras(
    desired_by: dict[str, TreeEntry],
    live_by: dict[str, TreeEntry],
    prior_by: dict[str, TreeEntry],
    policy: TreePolicy,
) -> list[TreeAction]:
    actions: list[TreeAction] = []
    for path, current in live_by.items():
        if path in desired_by:
            continue
        previous = prior_by.get(path)
        if previous is None:
            actions.append(TreeAction(path, TreeActionKind.KEEP, "unowned live entry"))
        elif policy.orphans is TreeOrphanPolicy.KEEP:
            actions.append(
                TreeAction(path, TreeActionKind.KEEP, "owned orphan kept by policy")
            )
        elif current == previous:
            actions.append(
                TreeAction(path, TreeActionKind.REMOVE, "unchanged owned orphan")
            )
        else:
            actions.append(
                TreeAction(path, TreeActionKind.HOLD, "owned orphan drifted")
            )
    return actions


def _parts(relative: str) -> tuple[str, ...]:
    path = PurePosixPath(relative)
    if (
        not relative
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise InvariantViolation(f"tree entry escapes its root: {relative!r}")
    return path.parts


def _open_parent(root_fd: int, relative: str) -> tuple[int, str]:
    parts = _parts(relative)
    current = os.dup(root_fd)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = next_fd
        return current, parts[-1]
    except BaseException:
        os.close(current)
        raise


def _create_directory_at(root_fd: int, relative: str, mode: int) -> None:
    parent_fd, name = _open_parent(root_fd, relative)
    try:
        os.mkdir(name, mode, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _chmod_directory_at(
    root_fd: int, relative: str, expected: TreeEntry, mode: int
) -> None:
    parent_fd, name = _open_parent(root_fd, relative)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        child_fd = os.open(name, flags, dir_fd=parent_fd)
        try:
            observed = os.fstat(child_fd)
            if stat.S_IMODE(observed.st_mode) != expected.mode:
                raise SetforgeError(
                    f"managed tree directory changed before chmod: {relative}"
                )
            os.fchmod(child_fd, mode)
            os.fsync(child_fd)
        finally:
            os.close(child_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _atomic_file_at(parent_fd: int, name: str, payload: bytes, mode: int) -> None:
    temporary = temporary_entry_name(name, "create")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
        dir_fd=parent_fd,
    )
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        _renameat2(parent_fd, temporary, parent_fd, name, _RENAME_NOREPLACE)
        os.fsync(parent_fd)
    finally:
        os.close(descriptor)
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=parent_fd)


def _atomic_symlink_at(parent_fd: int, name: str, target: str) -> None:
    temporary = temporary_entry_name(name, "create")
    try:
        os.symlink(target, temporary, dir_fd=parent_fd)
        _renameat2(parent_fd, temporary, parent_fd, name, _RENAME_NOREPLACE)
        os.fsync(parent_fd)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=parent_fd)


def _renameat2(
    source_fd: int, source: str, destination_fd: int, destination: str, flags: int
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise SetforgeError(
            "managed tree publication requires renameat2 support on this platform"
        )
    result = renameat2(
        source_fd,
        os.fsencode(source),
        destination_fd,
        os.fsencode(destination),
        flags,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def _open_or_create_root(destination: Path, *, create: bool) -> int:
    destination = destination.absolute()
    missing: list[str] = []
    ancestor = destination
    while True:
        try:
            ancestor_fd = os.open(
                ancestor,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            )
            break
        except FileNotFoundError:
            missing.append(ancestor.name)
            ancestor = ancestor.parent
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    current = ancestor_fd
    try:
        if create == (not missing):
            raise SetforgeError("managed tree root changed before apply")
        for part in reversed(missing):
            os.mkdir(part, _DIR_MODE, dir_fd=current)
            next_fd = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = next_fd
        return current
    except BaseException:
        os.close(current)
        raise


def _open_or_create_root_at(
    anchor_fd: int, relative_parts: tuple[str, ...], *, create: bool
) -> int:
    current = os.dup(anchor_fd)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        for part in relative_parts:
            if create:
                os.mkdir(part, _DIR_MODE, dir_fd=current)
            next_fd = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = next_fd
        return current
    except BaseException:
        os.close(current)
        raise


def apply_tree(
    plan: TreePlan,
    destination: Path,
    policy: TreePolicy,
    *,
    anchor_fd: int | None = None,
    anchor_relative: tuple[str, ...] = (),
) -> TreeInventory:
    """Apply one unblocked frozen plan and return its verified inventory."""
    if plan.blocked:
        raise SetforgeError("managed tree has unresolved entry conflicts")
    destination = destination.absolute()
    payloads = plan.desired.payload_map()
    desired_by = {entry.path: entry for entry in plan.desired.inventory.entries}
    root_mode = plan.desired.inventory.root_mode or _DIR_MODE
    root_create = any(
        action.path == "." and action.kind is TreeActionKind.CREATE
        for action in plan.actions
    )
    root_chmod = any(
        action.path == "." and action.kind is TreeActionKind.CHMOD
        for action in plan.actions
    )
    try:
        root_fd = (
            _open_or_create_root(destination, create=root_create)
            if anchor_fd is None
            else _open_or_create_root_at(
                anchor_fd,
                anchor_relative,
                create=root_create and bool(anchor_relative),
            )
        )
    except OSError as exc:
        raise SetforgeError(f"cannot open managed tree root: {destination}") from exc
    try:
        if root_create:
            os.fchmod(root_fd, root_mode)
        elif root_chmod:
            observed_root = os.fstat(root_fd)
            if stat.S_IMODE(observed_root.st_mode) != plan.live.root_mode:
                raise SetforgeError("managed tree root changed before chmod")
            os.fchmod(root_fd, root_mode)
        _apply_directories(plan, root_fd, desired_by)
        _apply_updates(plan, root_fd, desired_by, payloads)
        _apply_removals(plan, root_fd)
        os.fsync(root_fd)
        live_policy = policy.model_copy(update={"symlinks": TreeSymlinkPolicy.PRESERVE})
        result = _scan_tree_fd(root_fd, destination, live_policy).inventory
        if anchor_fd is None:
            _verify_path_binding(destination, root_fd)
        else:
            _verify_relative_binding(anchor_fd, anchor_relative, root_fd)
    except OSError as exc:
        raise SetforgeError(
            f"managed tree changed during apply: {destination}"
        ) from exc
    finally:
        os.close(root_fd)
    prior_owned: set[str] = set()
    if plan.prior is not None:
        if plan.prior.owned_paths is None:
            prior_owned = {entry.path for entry in plan.prior.entries}
        else:
            prior_owned = set(plan.prior.owned_paths)
    owned_paths = {entry.path for entry in plan.desired.inventory.entries} | prior_owned
    return TreeInventory(
        result.root_present,
        result.root_mode,
        result.entries,
        result.fingerprint,
        tuple(entry.path for entry in result.entries if entry.path in owned_paths),
    )


def _same_object(left_fd: int, right_fd: int) -> bool:
    left = os.fstat(left_fd)
    right = os.fstat(right_fd)
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _verify_path_binding(destination: Path, expected_fd: int) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    current_fd = os.open(destination, flags)
    try:
        if not _same_object(current_fd, expected_fd):
            raise SetforgeError("managed tree root binding changed during apply")
    finally:
        os.close(current_fd)


def _verify_relative_binding(
    anchor_fd: int, relative_parts: tuple[str, ...], expected_fd: int
) -> None:
    current_fd = os.dup(anchor_fd)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        for part in relative_parts:
            next_fd = os.open(part, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        if not _same_object(current_fd, expected_fd):
            raise SetforgeError("managed tree root binding changed during apply")
    finally:
        os.close(current_fd)


def _apply_directories(
    plan: TreePlan, root_fd: int, desired_by: dict[str, TreeEntry]
) -> None:
    actions = [
        action
        for action in plan.actions
        if action.path != "."
        and action.path in desired_by
        and desired_by[action.path].kind is TreeEntryKind.DIRECTORY
        and action.kind in {TreeActionKind.CREATE, TreeActionKind.CHMOD}
    ]
    live_by = {entry.path: entry for entry in plan.live.entries}
    for action in sorted(
        actions,
        key=lambda item: (item.path.count("/"), item.path),
    ):
        entry = desired_by[action.path]
        if action.kind is TreeActionKind.CREATE:
            _create_directory_at(root_fd, entry.path, entry.mode)
        else:
            _chmod_directory_at(root_fd, entry.path, live_by[entry.path], entry.mode)


def _apply_updates(
    plan: TreePlan,
    root_fd: int,
    desired_by: dict[str, TreeEntry],
    payloads: dict[str, bytes],
) -> None:
    for action in plan.actions:
        if action.path == ".":
            continue
        if action.kind not in {
            TreeActionKind.CREATE,
            TreeActionKind.UPDATE,
            TreeActionKind.CHMOD,
        }:
            continue
        entry = desired_by[action.path]
        if entry.kind is TreeEntryKind.DIRECTORY:
            continue
        elif entry.kind is TreeEntryKind.FILE:
            parent_fd, name = _open_parent(root_fd, entry.path)
            try:
                if action.kind is TreeActionKind.CREATE:
                    _atomic_file_at(parent_fd, name, payloads[entry.path], entry.mode)
                else:
                    _exchange_file_at(
                        parent_fd,
                        name,
                        payloads[entry.path],
                        entry.mode,
                        next(
                            item
                            for item in plan.live.entries
                            if item.path == entry.path
                        ),
                    )
            finally:
                os.close(parent_fd)
        else:
            parent_fd, name = _open_parent(root_fd, entry.path)
            try:
                if action.kind is TreeActionKind.CREATE:
                    _atomic_symlink_at(parent_fd, name, entry.link_target or "")
                else:
                    _exchange_symlink_at(
                        parent_fd,
                        name,
                        entry.link_target or "",
                        next(
                            item
                            for item in plan.live.entries
                            if item.path == entry.path
                        ),
                    )
            finally:
                os.close(parent_fd)


def _entry_at(parent_fd: int, name: str, relative: str) -> TreeEntry:
    before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    mode = stat.S_IMODE(before.st_mode)
    if stat.S_ISREG(before.st_mode):
        payload, stable = _stable_file_at(parent_fd, name, before, Path(relative))
        return TreeEntry(
            relative,
            TreeEntryKind.FILE,
            stat.S_IMODE(stable.st_mode),
            hashlib.sha256(payload).hexdigest(),
        )
    if stat.S_ISDIR(before.st_mode):
        return TreeEntry(relative, TreeEntryKind.DIRECTORY, mode)
    if stat.S_ISLNK(before.st_mode):
        return TreeEntry(
            relative,
            TreeEntryKind.SYMLINK,
            mode,
            link_target=os.readlink(name, dir_fd=parent_fd),
        )
    raise SetforgeError(f"managed tree entry kind changed: {relative}")


def _restore_exchange(parent_fd: int, temporary: str, name: str) -> None:
    _renameat2(parent_fd, temporary, parent_fd, name, _RENAME_EXCHANGE)


def _exchange_file_at(
    parent_fd: int, name: str, payload: bytes, mode: int, expected: TreeEntry
) -> None:
    temporary = temporary_entry_name(name, "update")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
        dir_fd=parent_fd,
    )
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        _renameat2(parent_fd, temporary, parent_fd, name, _RENAME_EXCHANGE)
        if _entry_at(parent_fd, temporary, expected.path) != expected:
            _restore_exchange(parent_fd, temporary, name)
            raise SetforgeError(
                f"managed tree entry changed before update: {expected.path}"
            )
        os.unlink(temporary, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(descriptor)
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=parent_fd)


def _exchange_symlink_at(
    parent_fd: int, name: str, target: str, expected: TreeEntry
) -> None:
    temporary = temporary_entry_name(name, "update")
    try:
        os.symlink(target, temporary, dir_fd=parent_fd)
        _renameat2(parent_fd, temporary, parent_fd, name, _RENAME_EXCHANGE)
        if _entry_at(parent_fd, temporary, expected.path) != expected:
            _restore_exchange(parent_fd, temporary, name)
            raise SetforgeError(
                f"managed tree entry changed before update: {expected.path}"
            )
        os.unlink(temporary, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=parent_fd)


def _apply_removals(plan: TreePlan, root_fd: int) -> None:
    removals = [
        action for action in plan.actions if action.kind is TreeActionKind.REMOVE
    ]
    for action in sorted(
        removals,
        key=lambda item: (-item.path.count("/"), item.path),
    ):
        entry = next(item for item in plan.live.entries if item.path == action.path)
        parent_fd, name = _open_parent(root_fd, action.path)
        quarantine = temporary_entry_name(name, "remove")
        isolated = False
        try:
            _renameat2(parent_fd, name, parent_fd, quarantine, _RENAME_NOREPLACE)
            isolated = True
            if _entry_at(parent_fd, quarantine, entry.path) != entry:
                raise SetforgeError(
                    f"managed tree entry changed before removal: {entry.path}"
                )
            if entry.kind is TreeEntryKind.DIRECTORY:
                os.rmdir(quarantine, dir_fd=parent_fd)
            else:
                os.unlink(quarantine, dir_fd=parent_fd)
            os.fsync(parent_fd)
            isolated = False
        except FileNotFoundError:
            continue
        except BaseException as exc:
            if isolated:
                try:
                    _renameat2(
                        parent_fd,
                        quarantine,
                        parent_fd,
                        name,
                        _RENAME_NOREPLACE,
                    )
                    os.fsync(parent_fd)
                except OSError as restore_exc:
                    raise SetforgeError(
                        "managed tree removal failed and isolated content could "
                        f"not be restored: {action.path}; retained as {quarantine}"
                    ) from restore_exc
            if isinstance(exc, SetforgeError):
                raise
            if isinstance(exc, OSError):
                raise SetforgeError(
                    f"refusing unsafe managed tree removal: {action.path}"
                ) from exc
            raise
        finally:
            os.close(parent_fd)


def inventory_path(profile: str, tracked_id: str) -> Path:
    """Return the traversal-safe state path for one tree inventory."""
    file_id(profile)
    file_id(tracked_id)
    if "/" in profile:
        raise InvariantViolation("tree inventory profile must be one segment")
    root = (state_root() / "tree-inventory").resolve()
    target = (root / profile / f"{tracked_id}.json").resolve()
    if root not in target.parents:
        raise InvariantViolation("tree inventory path escapes state root")
    return target


def dumps_inventory(inventory: TreeInventory) -> str:
    """Serialize one inventory in canonical schema-1 form."""
    return (
        json.dumps(
            {
                "entries": [
                    {
                        "content_hash": entry.content_hash,
                        "kind": entry.kind.value,
                        "link_target": entry.link_target,
                        "mode": entry.mode,
                        "path": entry.path,
                    }
                    for entry in inventory.entries
                ],
                "fingerprint": inventory.fingerprint,
                "owned_paths": inventory.owned_paths,
                "root_mode": inventory.root_mode,
                "root_present": inventory.root_present,
                "schema": _SCHEMA,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )


def loads_inventory(text: str) -> TreeInventory:
    """Parse and fully validate one persisted tree inventory."""
    try:
        raw = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvariantViolation(f"tree inventory is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) != {
        "entries",
        "fingerprint",
        "owned_paths",
        "root_mode",
        "root_present",
        "schema",
    }:
        raise InvariantViolation("tree inventory has unknown or missing fields")
    if raw["schema"] != _SCHEMA or type(raw["root_present"]) is not bool:
        raise InvariantViolation("unsupported tree inventory schema or root state")
    root_mode = raw["root_mode"]
    if root_mode is not None and type(root_mode) is not int:
        raise InvariantViolation("tree inventory root_mode must be int or null")
    if not isinstance(raw["entries"], list):
        raise InvariantViolation("tree inventory entries must be a list")
    owned_paths_raw = raw["owned_paths"]
    if owned_paths_raw is not None and (
        not isinstance(owned_paths_raw, list)
        or not all(isinstance(item, str) for item in owned_paths_raw)
    ):
        raise InvariantViolation("tree inventory owned_paths must be a string list")
    entries = [_decode_entry(item) for item in raw["entries"]]
    ordered = tuple(sorted(entries, key=lambda entry: entry.path))
    if tuple(entries) != ordered or len({entry.path for entry in entries}) != len(
        entries
    ):
        raise InvariantViolation("tree inventory paths must be sorted and unique")
    computed = _inventory_fingerprint(
        root_present=bool(raw["root_present"]), root_mode=root_mode, entries=ordered
    )
    if raw["fingerprint"] != computed:
        raise InvariantViolation("tree inventory fingerprint mismatch")
    owned_paths = tuple(owned_paths_raw) if owned_paths_raw is not None else None
    if owned_paths is not None and (
        owned_paths != tuple(sorted(set(owned_paths)))
        or not set(owned_paths).issubset(entry.path for entry in ordered)
    ):
        raise InvariantViolation("tree inventory owned_paths must be sorted entries")
    return TreeInventory(
        bool(raw["root_present"]), root_mode, ordered, computed, owned_paths
    )


def _decode_entry(item: object) -> TreeEntry:
    if not isinstance(item, dict) or set(item) != {
        "content_hash",
        "kind",
        "link_target",
        "mode",
        "path",
    }:
        raise InvariantViolation("tree inventory entry has invalid fields")
    try:
        return TreeEntry(
            path=item["path"],
            kind=TreeEntryKind(item["kind"]),
            mode=item["mode"],
            content_hash=item["content_hash"],
            link_target=item["link_target"],
        )
    except (TypeError, ValueError) as exc:
        raise InvariantViolation(f"invalid tree inventory entry: {exc}") from exc


def read_inventory(profile: str, tracked_id: str) -> TreeInventory | None:
    """Read one prior inventory without creating state."""
    path = inventory_path(profile, tracked_id)
    try:
        return loads_inventory(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def write_inventory(profile: str, tracked_id: str, inventory: TreeInventory) -> None:
    """Atomically publish one successfully applied tree inventory."""
    path = inventory_path(profile, tracked_id)
    path.parent.mkdir(parents=True, mode=_DIR_MODE, exist_ok=True)
    atomicio.atomic_write_text(path, dumps_inventory(inventory), mode=_FILE_MODE)
