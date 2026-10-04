"""Repository-common Git plumbing for tracked project overlays."""

from __future__ import annotations

import contextlib
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

_DRIVER = "setforge-project"
_PROCESS = "setforge project filter-process"
_OWNED_KEY = f"filter.{_DRIVER}.setforgeOwned"
_CREATED_ATTRIBUTES_KEY = f"filter.{_DRIVER}.setforgeCreatedAttributes"
_DIRECTORY = "Git overlay directory"
_run_git = partial(run_git, failure="cannot manage tracked project Git filter")
_require_directory_identity = partial(require_directory_identity, what=_DIRECTORY)
_read_bounded_at = partial(read_bounded_at, label="Git overlay")


@dataclass(frozen=True, slots=True, order=True)
class OverlayClaim(GitClaim):
    """One injection's shared attribute claim for an exact path."""


@dataclass(frozen=True, slots=True)
class OverlayGitPlan:
    """Exact-byte-bound shared Git configuration and attribute update."""

    target: Path
    common_dir: Path
    common_device: int
    common_inode: int
    info_device: int
    info_inode: int
    config_path: Path
    config_before: bytes
    config_mode: int
    attributes_path: Path
    attributes_before: bytes
    attributes_mode: int
    attributes_after: bytes
    added: tuple[OverlayClaim, ...]
    removed: tuple[OverlayClaim, ...]
    configure_driver: bool
    remove_driver: bool
    create_attributes: bool = False
    created_attributes: bool = False
    create_info: bool = False

    @property
    def changed(self) -> bool:
        return (
            self.attributes_before != self.attributes_after
            or self.configure_driver
            or self.remove_driver
        )

    @property
    def remove_attributes(self) -> bool:
        """Return whether removal must delete the file SetForge itself created."""
        return (
            self.remove_driver and self.created_attributes and not self.attributes_after
        )


def overlay_claim_id(*, git_dir: Path, profile: str, relative_path: str) -> str:
    """Return a stable identity for one worktree/profile/path overlay claim."""
    return claim_identity(git_dir, profile, relative_path)


def read_overlay_claims(attributes_path: Path) -> tuple[OverlayClaim, ...]:
    """Return the claims a shared attributes file holds; none when it is missing."""
    payload = attributes_path.read_bytes() if attributes_path.is_file() else b""
    return _BLOCK.parse(payload)[1]


def plan_overlay_git(
    target: Path,
    *,
    add: tuple[OverlayClaim, ...] = (),
    remove: tuple[OverlayClaim, ...] = (),
) -> OverlayGitPlan:
    """Plan additive shared Git plumbing without changing repository state."""
    common_dir = _git_common_dir(target)
    config_path = common_dir / "config"
    attributes_path = common_dir / "info" / "attributes"
    common_fd, common_info = open_directory(common_dir, _DIRECTORY)
    info_fd, info_info = _open_info(attributes_path.parent, common_info)
    try:
        _require_directory_identity(common_dir, common_info)
        _require_info_identity(attributes_path.parent, info_fd, info_info)
        config_before, config_mode = _read_bounded_at(
            common_fd, config_path, missing_mode=0o600
        )
        attributes_before, attributes_mode = _read_attributes_at(
            info_fd, attributes_path, missing_mode=0o644
        )
        process = _config_values_at(target, common_fd, f"filter.{_DRIVER}.process")
        required = _config_values_at(target, common_fd, f"filter.{_DRIVER}.required")
        owned = _config_values_at(target, common_fd, _OWNED_KEY)
        created_attributes = _config_values_at(
            target, common_fd, _CREATED_ATTRIBUTES_KEY
        ) == ("true",)
        attributes_missing = info_fd is None or not _exists_at(info_fd, "attributes")
        _require_directory_identity(common_dir, common_info)
        _require_info_identity(attributes_path.parent, info_fd, info_info)
        if _read_bounded_at(common_fd, config_path, missing_mode=0o600) != (
            config_before,
            config_mode,
        ) or _read_attributes_at(info_fd, attributes_path, missing_mode=0o644) != (
            attributes_before,
            attributes_mode,
        ):
            raise SetforgeError("Git overlay state changed while planning; retry")
    finally:
        _close(info_fd, common_fd)
    attributes_after, added, removed, remaining = _BLOCK.update(
        attributes_before, add=add, remove=remove
    )
    config_pair = (process, required)
    if (
        config_pair not in {((), ()), ((_PROCESS,), ("true",))}
        or owned not in {(), ("true",)}
        or (owned == ("true",) and (process != (_PROCESS,) or required != ("true",)))
    ):
        raise SetforgeError(
            "Git already has an incompatible setforge-project filter configuration"
        )
    for claim in remaining:
        value = _attribute_value(target, claim.relative_path)
        if value not in {None, _DRIVER}:
            raise SetforgeError(
                "Git already has an incompatible filter attribute for "
                f"{claim.relative_path}"
            )
    _revalidate_plan_state(
        common_dir=common_dir,
        common_identity=(common_info.st_dev, common_info.st_ino),
        config_path=config_path,
        config_before=config_before,
        config_mode=config_mode,
        attributes_path=attributes_path,
        info_identity=(
            (info_info.st_dev, info_info.st_ino) if info_fd is not None else None
        ),
        attributes_before=attributes_before,
        attributes_mode=attributes_mode,
    )
    configure = bool(remaining) and not (
        process == (_PROCESS,) and required == ("true",)
    )
    remove_driver = not remaining and owned == ("true",)
    return OverlayGitPlan(
        target=target,
        common_dir=common_dir,
        common_device=common_info.st_dev,
        common_inode=common_info.st_ino,
        info_device=info_info.st_dev,
        info_inode=info_info.st_ino,
        config_path=config_path,
        config_before=config_before,
        config_mode=config_mode,
        attributes_path=attributes_path,
        attributes_before=attributes_before,
        attributes_mode=attributes_mode,
        attributes_after=attributes_after,
        added=added,
        removed=removed,
        configure_driver=configure,
        remove_driver=remove_driver,
        create_attributes=configure and attributes_missing,
        created_attributes=created_attributes,
        create_info=info_fd is None,
    )


def apply_overlay_git(plan: OverlayGitPlan) -> None:
    """Apply a byte-bound shared Git plan after revalidating both files."""
    if plan.create_info and not plan.changed:
        return
    common_fd, common_info = open_directory(plan.common_dir, _DIRECTORY)
    info_fd: int | None = None
    try:
        _require_directory_identity(
            plan.common_dir,
            common_info,
            expected=(plan.common_device, plan.common_inode),
        )
        info_fd, info_info = _open_planned_info(plan, common_fd)
        _require_directory_identity(
            plan.attributes_path.parent,
            info_info,
            expected=(
                None if plan.create_info else (plan.info_device, plan.info_inode)
            ),
        )
        current_config, current_config_mode = _read_bounded_at(
            common_fd, plan.config_path, missing_mode=plan.config_mode
        )
        current_attributes, current_attributes_mode = _read_bounded_at(
            info_fd, plan.attributes_path, missing_mode=plan.attributes_mode
        )
        if (
            current_config != plan.config_before
            or current_config_mode != plan.config_mode
            or current_attributes != plan.attributes_before
            or current_attributes_mode != plan.attributes_mode
        ):
            raise SetforgeError("Git overlay state changed before apply; retry")
        if plan.configure_driver:
            for key, value in (
                (f"filter.{_DRIVER}.process", _PROCESS),
                (f"filter.{_DRIVER}.required", "true"),
                (_OWNED_KEY, "true"),
            ):
                _config_set_at(plan.target, common_fd, key, value)
            if plan.create_attributes:
                _config_set_at(plan.target, common_fd, _CREATED_ATTRIBUTES_KEY, "true")
        elif plan.remove_driver:
            for key in (
                f"filter.{_DRIVER}.process",
                f"filter.{_DRIVER}.required",
                _OWNED_KEY,
                *((_CREATED_ATTRIBUTES_KEY,) if plan.created_attributes else ()),
            ):
                _config_unset_at(plan.target, common_fd, key)
        _require_directory_identity(plan.common_dir, common_info)
        if plan.remove_attributes:
            os.unlink("attributes", dir_fd=info_fd)
            os.fsync(info_fd)
        elif plan.attributes_before != plan.attributes_after:
            atomicio.atomic_write_bytes_at(
                info_fd,
                "attributes",
                plan.attributes_after,
                mode=plan.attributes_mode,
            )
        _require_directory_identity(plan.attributes_path.parent, info_info)
    finally:
        _close(info_fd, common_fd)


def _git_common_dir(target: Path) -> Path:
    common_raw = _run_git(target, ["rev-parse", "--git-common-dir"]).stdout.strip()
    common = Path(common_raw)
    if not common.is_absolute():
        common = target / common
    common = common.resolve(strict=True)
    return common


def _open_info(
    path: Path, common_info: os.stat_result
) -> tuple[int | None, os.stat_result]:
    """Open ``info``, or bind the plan to the common directory when it is missing."""
    opened = open_info(path, _DIRECTORY)
    return (None, common_info) if opened is None else opened


def _open_planned_info(
    plan: OverlayGitPlan, common_fd: int
) -> tuple[int, os.stat_result]:
    """Open ``info``, first creating one the plan found missing."""
    if plan.create_info:
        # The private exclude update of the same change may have made it.
        with contextlib.suppress(FileExistsError):
            os.mkdir("info", mode=0o755, dir_fd=common_fd)
        os.fsync(common_fd)
    return open_directory(plan.attributes_path.parent, _DIRECTORY)


def _require_info_identity(
    path: Path,
    info_fd: int | None,
    info: os.stat_result,
    *,
    expected: tuple[int, int] | None = None,
) -> None:
    if info_fd is not None:
        _require_directory_identity(path, info, expected=expected)


def _close(*descriptors: int | None) -> None:
    for descriptor in descriptors:
        if descriptor is not None:
            os.close(descriptor)


def _read_attributes_at(
    info_fd: int | None, path: Path, *, missing_mode: int
) -> tuple[bytes, int]:
    if info_fd is None:
        return b"", missing_mode
    return _read_bounded_at(info_fd, path, missing_mode=missing_mode)


def _exists_at(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _revalidate_plan_state(
    *,
    common_dir: Path,
    common_identity: tuple[int, int],
    config_path: Path,
    config_before: bytes,
    config_mode: int,
    attributes_path: Path,
    info_identity: tuple[int, int] | None,
    attributes_before: bytes,
    attributes_mode: int,
) -> None:
    common_fd, common_info = open_directory(common_dir, _DIRECTORY)
    info_fd, info_info = _open_info(attributes_path.parent, common_info)
    try:
        _require_directory_identity(common_dir, common_info, expected=common_identity)
        if (info_fd is None) != (info_identity is None):
            raise SetforgeError("Git overlay state changed while planning; retry")
        _require_info_identity(
            attributes_path.parent, info_fd, info_info, expected=info_identity
        )
        if _read_bounded_at(common_fd, config_path, missing_mode=config_mode) != (
            config_before,
            config_mode,
        ) or _read_attributes_at(
            info_fd, attributes_path, missing_mode=attributes_mode
        ) != (attributes_before, attributes_mode):
            raise SetforgeError("Git overlay state changed while planning; retry")
    finally:
        _close(info_fd, common_fd)


def _config_args(parent_fd: int) -> list[str]:
    return ["config", "--file", f"/proc/self/fd/{parent_fd}/config"]


def _config_values_at(target: Path, parent_fd: int, key: str) -> tuple[str, ...]:
    result = _run_git(
        target,
        [*_config_args(parent_fd), "--get-all", key],
        check=False,
        pass_fds=(parent_fd,),
    )
    if result.returncode == 1:
        return ()
    if result.returncode != 0:
        raise SetforgeError(f"cannot inspect Git overlay setting: {key}")
    return tuple(result.stdout.splitlines())


def _config_set_at(target: Path, parent_fd: int, key: str, value: str) -> None:
    _run_git(
        target,
        [*_config_args(parent_fd), key, value],
        pass_fds=(parent_fd,),
    )


def _config_unset_at(target: Path, parent_fd: int, key: str) -> None:
    result = _run_git(
        target,
        [*_config_args(parent_fd), "--unset-all", key],
        check=False,
        pass_fds=(parent_fd,),
    )
    if result.returncode not in {0, 1}:
        raise SetforgeError(f"cannot remove Git overlay setting: {key}")


def _attribute_value(target: Path, relative_path: str) -> str | None:
    result = _run_git(target, ["check-attr", "filter", "--", relative_path])
    prefix = f"{relative_path}: filter: "
    if not result.stdout.startswith(prefix):
        raise SetforgeError("cannot inspect Git overlay attribute assignment")
    value = result.stdout[len(prefix) :].strip()
    return None if value == "unspecified" else value


def _attribute_pattern(relative_path: str) -> bytes:
    relative = Path(relative_path)
    if (
        not relative_path
        or relative.is_absolute()
        or relative == Path()
        or ".." in relative.parts
        or relative.as_posix() != relative_path
        or any(character in relative_path for character in "\n\r\0")
    ):
        raise SetforgeError("Git overlay path is not normalized")
    escaped = "".join(
        f"\\{character}" if character in "\\ *?[]" else character
        for character in relative_path
    )
    return f"/{escaped} filter={_DRIVER}\n".encode()


_BLOCK = ClaimBlock(
    "Git overlay",
    b"\n# >>> setforge project overlays v1 >>>\n",
    b"# <<< setforge project overlays v1 <<<\n",
    OverlayClaim,
    _attribute_pattern,
)
