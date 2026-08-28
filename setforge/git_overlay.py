"""Repository-common Git plumbing for tracked project overlays."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from setforge import atomicio
from setforge.errors import SetforgeError

_BEGIN = b"\n# >>> setforge project overlays v1 >>>\n"
_END = b"# <<< setforge project overlays v1 <<<\n"
_CLAIM = re.compile(rb"# claim ([0-9a-f]{64}) (.+)\n")
_CLAIM_ID = re.compile(r"[0-9a-f]{64}")
_DRIVER = "setforge-project"
_PROCESS = "setforge project filter-process"
_OWNED_KEY = f"filter.{_DRIVER}.setforgeOwned"
_MAX_SHARED_FILE = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True, order=True)
class OverlayClaim:
    """One injection's shared attribute claim for an exact path."""

    claim_id: str
    relative_path: str


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

    @property
    def changed(self) -> bool:
        return (
            self.attributes_before != self.attributes_after
            or self.configure_driver
            or self.remove_driver
        )


def overlay_claim_id(*, git_dir: Path, profile: str, relative_path: str) -> str:
    """Return a stable identity for one worktree/profile/path overlay claim."""
    payload = json.dumps(
        {
            "git_dir": str(git_dir),
            "profile": profile,
            "relative_path": relative_path,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    import hashlib

    return hashlib.sha256(payload).hexdigest()


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
    common_fd, common_info = _open_directory(common_dir)
    info_fd, info_info = _open_directory(attributes_path.parent)
    try:
        _require_directory_identity(common_dir, common_info)
        _require_directory_identity(attributes_path.parent, info_info)
        config_before, config_mode = _read_bounded_at(
            common_fd, "config", config_path, missing_mode=0o600
        )
        attributes_before, attributes_mode = _read_bounded_at(
            info_fd, "attributes", attributes_path, missing_mode=0o644
        )
        process = _config_values_at(target, common_fd, f"filter.{_DRIVER}.process")
        required = _config_values_at(target, common_fd, f"filter.{_DRIVER}.required")
        owned = _config_values_at(target, common_fd, _OWNED_KEY)
        _require_directory_identity(common_dir, common_info)
        _require_directory_identity(attributes_path.parent, info_info)
        if _read_bounded_at(common_fd, "config", config_path, missing_mode=0o600) != (
            config_before,
            config_mode,
        ) or _read_bounded_at(
            info_fd, "attributes", attributes_path, missing_mode=0o644
        ) != (attributes_before, attributes_mode):
            raise SetforgeError("Git overlay state changed while planning; retry")
    finally:
        os.close(info_fd)
        os.close(common_fd)
    prefix, current, suffix = _parse_attributes(attributes_before)
    by_id = {claim.claim_id: claim for claim in current}
    removed: list[OverlayClaim] = []
    for claim in remove:
        _validate_claim(claim)
        if by_id.get(claim.claim_id) != claim:
            raise SetforgeError("Git overlay claim is missing or mismatched")
        removed.append(by_id.pop(claim.claim_id))
    added: list[OverlayClaim] = []
    for claim in add:
        _validate_claim(claim)
        observed = by_id.get(claim.claim_id)
        if observed is not None and observed != claim:
            raise SetforgeError("Git overlay claim identity collides")
        if observed is None:
            by_id[claim.claim_id] = claim
            added.append(claim)
    remaining = tuple(sorted(by_id.values()))
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
        info_identity=(info_info.st_dev, info_info.st_ino),
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
        attributes_after=_render_attributes(prefix, remaining, suffix),
        added=tuple(added),
        removed=tuple(removed),
        configure_driver=configure,
        remove_driver=remove_driver,
    )


def apply_overlay_git(plan: OverlayGitPlan) -> None:
    """Apply a byte-bound shared Git plan after revalidating both files."""
    common_fd, common_info = _open_directory(plan.common_dir)
    info_fd, info_info = _open_directory(plan.attributes_path.parent)
    try:
        _require_directory_identity(
            plan.common_dir,
            common_info,
            expected=(plan.common_device, plan.common_inode),
        )
        _require_directory_identity(
            plan.attributes_path.parent,
            info_info,
            expected=(plan.info_device, plan.info_inode),
        )
        current_config, current_config_mode = _read_bounded_at(
            common_fd, "config", plan.config_path, missing_mode=plan.config_mode
        )
        current_attributes, current_attributes_mode = _read_bounded_at(
            info_fd,
            "attributes",
            plan.attributes_path,
            missing_mode=plan.attributes_mode,
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
        elif plan.remove_driver:
            for key in (
                f"filter.{_DRIVER}.process",
                f"filter.{_DRIVER}.required",
                _OWNED_KEY,
            ):
                _config_unset_at(plan.target, common_fd, key)
        _require_directory_identity(plan.common_dir, common_info)
        if plan.attributes_before != plan.attributes_after:
            atomicio.atomic_write_bytes_at(
                info_fd,
                "attributes",
                plan.attributes_after,
                mode=plan.attributes_mode,
            )
        _require_directory_identity(plan.attributes_path.parent, info_info)
    finally:
        os.close(info_fd)
        os.close(common_fd)


def _run_git(
    target: Path,
    args: list[str],
    *,
    check: bool = True,
    pass_fds: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "Never",
        "LANG": "C",
        "LC_ALL": "C",
    }
    try:
        return subprocess.run(
            ["git", "-C", str(target), *args],
            check=check,
            text=True,
            capture_output=True,
            timeout=30,
            env=environment,
            pass_fds=pass_fds,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        raise SetforgeError(
            f"cannot manage tracked project Git filter: {detail}"
        ) from exc


def _git_common_dir(target: Path) -> Path:
    common_raw = _run_git(target, ["rev-parse", "--git-common-dir"]).stdout.strip()
    common = Path(common_raw)
    if not common.is_absolute():
        common = target / common
    common = common.resolve(strict=True)
    return common


def _open_directory(path: Path) -> tuple[int, os.stat_result]:
    try:
        descriptor = os.open(
            path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        return descriptor, os.fstat(descriptor)
    except OSError as exc:
        raise SetforgeError(
            f"Git overlay directory cannot be opened: {path}: {exc}"
        ) from exc


def _require_directory_identity(
    path: Path, info: os.stat_result, *, expected: tuple[int, int] | None = None
) -> None:
    identity = (info.st_dev, info.st_ino)
    if expected is not None and identity != expected:
        raise SetforgeError("Git overlay directory changed before apply; retry")
    try:
        current = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise SetforgeError(
            "Git overlay directory changed before apply; retry"
        ) from exc
    if (
        not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != identity
    ):
        raise SetforgeError("Git overlay directory changed before apply; retry")


def _read_bounded_at(
    parent_fd: int, name: str, path: Path, *, missing_mode: int
) -> tuple[bytes, int]:
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd
        )
    except FileNotFoundError:
        return b"", missing_mode
    except OSError as exc:
        raise SetforgeError(
            f"Git overlay state cannot be opened: {path}: {exc}"
        ) from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_SHARED_FILE:
            raise SetforgeError(f"Git overlay state is not a bounded file: {path}")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            payload = handle.read(_MAX_SHARED_FILE + 1)
            if len(payload) > _MAX_SHARED_FILE:
                raise SetforgeError(f"Git overlay state is not a bounded file: {path}")
            return payload, stat.S_IMODE(info.st_mode)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _revalidate_plan_state(
    *,
    common_dir: Path,
    common_identity: tuple[int, int],
    config_path: Path,
    config_before: bytes,
    config_mode: int,
    attributes_path: Path,
    info_identity: tuple[int, int],
    attributes_before: bytes,
    attributes_mode: int,
) -> None:
    common_fd, common_info = _open_directory(common_dir)
    info_fd, info_info = _open_directory(attributes_path.parent)
    try:
        _require_directory_identity(common_dir, common_info, expected=common_identity)
        _require_directory_identity(
            attributes_path.parent, info_info, expected=info_identity
        )
        if _read_bounded_at(
            common_fd, "config", config_path, missing_mode=config_mode
        ) != (config_before, config_mode) or _read_bounded_at(
            info_fd,
            "attributes",
            attributes_path,
            missing_mode=attributes_mode,
        ) != (attributes_before, attributes_mode):
            raise SetforgeError("Git overlay state changed while planning; retry")
    finally:
        os.close(info_fd)
        os.close(common_fd)


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


def _validate_claim(claim: OverlayClaim) -> None:
    if _CLAIM_ID.fullmatch(claim.claim_id) is None:
        raise SetforgeError("Git overlay claim has an invalid identity")
    _attribute_pattern(claim.relative_path)


def _claim_from_match(match: re.Match[bytes]) -> OverlayClaim:
    try:
        relative = json.loads(match.group(2))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise SetforgeError("Git overlay claim path is invalid") from exc
    if not isinstance(relative, str):
        raise SetforgeError("Git overlay claim path is invalid")
    claim = OverlayClaim(match.group(1).decode("ascii"), relative)
    _validate_claim(claim)
    return claim


def _parse_attributes(
    payload: bytes,
) -> tuple[bytes, tuple[OverlayClaim, ...], bytes]:
    if payload.count(_BEGIN) == 0 and payload.count(_END) == 0:
        return payload, (), b""
    if payload.count(_BEGIN) != 1 or payload.count(_END) != 1:
        raise SetforgeError("Git overlay attributes block is ambiguous")
    start = payload.index(_BEGIN)
    end_start = payload.index(_END)
    if end_start < start:
        raise SetforgeError("Git overlay attributes markers are out of order")
    body = payload[start + len(_BEGIN) : end_start]
    claims: list[OverlayClaim] = []
    offset = 0
    while offset < len(body):
        match = _CLAIM.match(body, offset)
        if match is None:
            raise SetforgeError("Git overlay attributes block is invalid")
        claim = _claim_from_match(match)
        pattern = _attribute_pattern(claim.relative_path)
        if body[match.end() : match.end() + len(pattern)] != pattern:
            raise SetforgeError("Git overlay claim pattern is inconsistent")
        claims.append(claim)
        offset = match.end() + len(pattern)
    if len({claim.claim_id for claim in claims}) != len(claims):
        raise SetforgeError("Git overlay attributes block has duplicate claims")
    if tuple(claims) != tuple(sorted(claims)):
        raise SetforgeError("Git overlay attributes claims are not canonical")
    end = end_start + len(_END)
    return payload[:start], tuple(claims), payload[end:]


def _render_attributes(
    prefix: bytes, claims: tuple[OverlayClaim, ...], suffix: bytes
) -> bytes:
    if not claims:
        return prefix + suffix
    body = bytearray(_BEGIN)
    for claim in sorted(claims):
        body.extend(f"# claim {claim.claim_id} ".encode())
        body.extend(
            json.dumps(
                claim.relative_path,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        )
        body.extend(b"\n")
        body.extend(_attribute_pattern(claim.relative_path))
    body.extend(_END)
    return prefix + bytes(body) + suffix
