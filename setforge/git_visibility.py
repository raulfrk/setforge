"""Private exact-path Git visibility for injected project files."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from setforge import atomicio
from setforge.errors import SetforgeError
from setforge.git_info import (
    ClaimBlock,
    GitClaim,
    claim_identity,
    open_directory,
    open_info,
    read_bounded_at,
    require_directory_identity,
    run_git,
)

_DEFAULT_MODE = 0o644
_PARENT = "Git visibility parent"
_PARENT_CHANGED = f"{_PARENT} changed before apply; retry"
_run_git = partial(run_git, failure="cannot inspect project Git visibility")


@dataclass(frozen=True, slots=True, order=True)
class VisibilityClaim(GitClaim):
    """One injection's private claim on an exact repository-relative path."""


@dataclass(frozen=True, slots=True)
class VisibilityPlan:
    """An exact-byte-bound update to the repository-common exclude file."""

    exclude_path: Path
    before: bytes
    before_mode: int
    parent_device: int
    parent_inode: int
    after: bytes
    added: tuple[VisibilityClaim, ...]
    removed: tuple[VisibilityClaim, ...]
    create_parent: bool = False

    @property
    def changed(self) -> bool:
        return self.before != self.after


def info_exclude_path(target: Path) -> Path:
    """Resolve the repository-common private exclude path for TARGET."""
    raw = _run_git(target, ["rev-parse", "--git-path", "info/exclude"]).stdout.strip()
    path = Path(raw)
    if not path.is_absolute():
        path = target / path
    try:
        common = Path(
            _run_git(target, ["rev-parse", "--git-common-dir"]).stdout.strip()
        )
        if not common.is_absolute():
            common = target / common
        expected = common.resolve(strict=True) / "info" / "exclude"
        resolved_parent = path.parent.resolve()
    except OSError as exc:
        raise SetforgeError(f"Git visibility path cannot be resolved: {path}") from exc
    resolved = resolved_parent / path.name
    if resolved != expected or path.name != "exclude":
        raise SetforgeError("Git visibility path is not the common info/exclude file")
    return resolved


def claim_id(*, target_git_dir: Path, profile: str, relative_path: str) -> str:
    """Return a stable identity for one worktree/profile/path visibility claim."""
    return claim_identity(target_git_dir, profile, relative_path)


def _pattern(relative_path: str) -> bytes:
    relative = Path(relative_path)
    if (
        not relative_path
        or relative.is_absolute()
        or relative == Path()
        or ".." in relative.parts
        or relative.as_posix() != relative_path
    ):
        raise SetforgeError("Git visibility path is not normalized")
    if "\n" in relative_path or "\r" in relative_path:
        raise SetforgeError(
            "Git visibility cannot represent a path containing a line break"
        )
    escaped = "".join(
        f"\\{character}" if character in "\\*?[]" else character
        for character in relative_path
    )
    trailing_spaces = len(escaped) - len(escaped.rstrip(" "))
    if trailing_spaces:
        escaped = escaped[:-trailing_spaces] + "\\ " * trailing_spaces
    return ("/" + escaped + "\n").encode("utf-8")


_BLOCK = ClaimBlock(
    "Git visibility",
    b"\n# >>> setforge project visibility v1 >>>\n",
    b"# <<< setforge project visibility v1 <<<\n",
    VisibilityClaim,
    _pattern,
)


def _read_state(path: Path) -> tuple[bytes, int, os.stat_result, bool]:
    """Read the exclude bytes and the directory identity that binds a plan.

    Git ignores a missing ``info`` directory or exclude file, so both read as
    empty. Without ``info`` the Git common directory binds the plan instead and
    the last value tells apply to create ``info`` there.
    """
    opened = open_info(path.parent, _PARENT)
    if opened is None:
        try:
            common_info = path.parent.parent.stat(follow_symlinks=False)
        except OSError as exc:
            raise SetforgeError(
                f"{_PARENT} cannot be opened safely: {path.parent}: {exc}"
            ) from exc
        return b"", _DEFAULT_MODE, common_info, True
    parent_fd, parent_info = opened
    try:
        require_directory_identity(path.parent, parent_info, _PARENT)
        payload, mode = read_bounded_at(
            parent_fd, path, _BLOCK.label, missing_mode=_DEFAULT_MODE
        )
        require_directory_identity(path.parent, parent_info, _PARENT)
    finally:
        os.close(parent_fd)
    return payload, mode, parent_info, False


def read_claims(
    target: Path,
) -> tuple[Path, bytes, int, tuple[VisibilityClaim, ...]]:
    """Read and strictly validate SetForge claims without changing Git state."""
    path = info_exclude_path(target)
    payload, mode, _, _ = _read_state(path)
    return path, payload, mode, _BLOCK.parse(payload)[1]


def plan_claims(
    target: Path,
    *,
    add: tuple[VisibilityClaim, ...] = (),
    remove: tuple[VisibilityClaim, ...] = (),
) -> VisibilityPlan:
    """Plan an exact claim update against the current exclude bytes."""
    path = info_exclude_path(target)
    before, before_mode, parent_info, create_parent = _read_state(path)
    after, added, removed, _ = _BLOCK.update(before, add=add, remove=remove)
    return VisibilityPlan(
        exclude_path=path,
        before=before,
        before_mode=before_mode,
        parent_device=parent_info.st_dev,
        parent_inode=parent_info.st_ino,
        after=after,
        added=added,
        removed=removed,
        create_parent=create_parent,
    )


def _create_exclude_parent(plan: VisibilityPlan) -> None:
    """Create the missing ``info`` directory inside the bound common directory."""
    try:
        common_fd, info = open_directory(plan.exclude_path.parent.parent, _PARENT)
    except SetforgeError as exc:
        raise SetforgeError(_PARENT_CHANGED) from exc
    try:
        if (info.st_dev, info.st_ino) != (plan.parent_device, plan.parent_inode):
            raise SetforgeError(_PARENT_CHANGED)
        try:
            os.mkdir(plan.exclude_path.parent.name, mode=0o755, dir_fd=common_fd)
        except FileExistsError as exc:
            raise SetforgeError(
                "Git visibility state changed before apply; retry"
            ) from exc
        os.fsync(common_fd)
    finally:
        os.close(common_fd)


def apply_claims(plan: VisibilityPlan) -> None:
    """Apply a byte-bound visibility plan atomically."""
    if plan.create_parent:
        if not plan.changed:
            return
        _create_exclude_parent(plan)
    parent = plan.exclude_path.parent
    opened = open_info(parent, _PARENT)
    if opened is None:
        raise SetforgeError(_PARENT_CHANGED)
    parent_fd, parent_info = opened
    try:
        expected = (
            None if plan.create_parent else (plan.parent_device, plan.parent_inode)
        )
        require_directory_identity(parent, parent_info, _PARENT, expected=expected)
        current, current_mode = read_bounded_at(
            parent_fd, plan.exclude_path, _BLOCK.label, missing_mode=_DEFAULT_MODE
        )
        if current != plan.before or current_mode != plan.before_mode:
            raise SetforgeError("Git visibility state changed before apply; retry")
        require_directory_identity(parent, parent_info, _PARENT, expected=expected)
        if plan.changed:
            atomicio.atomic_write_bytes_at(
                parent_fd,
                plan.exclude_path.name,
                plan.after,
                mode=plan.before_mode,
            )
            require_directory_identity(parent, parent_info, _PARENT, expected=expected)
    finally:
        os.close(parent_fd)
