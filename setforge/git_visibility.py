"""Private exact-path Git visibility for injected project files."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from setforge import atomicio
from setforge.errors import SetforgeError

_BEGIN = b"\n# >>> setforge project visibility v1 >>>\n"
_END = b"# <<< setforge project visibility v1 <<<\n"
_CLAIM = re.compile(rb"# claim ([0-9a-f]{64}) (.+)\n")
_CLAIM_ID = re.compile(r"[0-9a-f]{64}")
_DEFAULT_MODE = 0o644


@dataclass(frozen=True, slots=True, order=True)
class VisibilityClaim:
    """One injection's private claim on an exact repository-relative path."""

    claim_id: str
    relative_path: str


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


def _run_git(
    target: Path, args: list[str], *, check: bool = True
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
        )
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        raise SetforgeError(f"cannot inspect project Git visibility: {detail}") from exc


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
    payload = json.dumps(
        {
            "git_dir": str(target_git_dir),
            "profile": profile,
            "relative_path": relative_path,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


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


def _validate_claim(claim: VisibilityClaim) -> None:
    if _CLAIM_ID.fullmatch(claim.claim_id) is None:
        raise SetforgeError("Git visibility claim has an invalid identity")
    _pattern(claim.relative_path)


def _parse(  # noqa: C901 - one strict parser for an externally editable file
    payload: bytes,
) -> tuple[bytes, tuple[VisibilityClaim, ...], bytes]:
    starts = payload.count(_BEGIN)
    ends = payload.count(_END)
    if starts == 0 and ends == 0:
        return payload, (), b""
    if starts != 1 or ends != 1:
        raise SetforgeError("Git visibility block is missing, duplicated, or ambiguous")
    start = payload.index(_BEGIN)
    end_start = payload.index(_END)
    if end_start < start:
        raise SetforgeError("Git visibility block markers are out of order")
    end = end_start + len(_END)
    body = payload[start + len(_BEGIN) : end_start]
    claims: list[VisibilityClaim] = []
    seen_ids: set[str] = set()
    seen_paths: set[tuple[str, str]] = set()
    offset = 0
    while offset < len(body):
        match = _CLAIM.match(body, offset)
        if match is None:
            raise SetforgeError("Git visibility block has an invalid claim")
        claim = match.group(1).decode("ascii")
        try:
            relative = json.loads(match.group(2))
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise SetforgeError("Git visibility block has an invalid path") from exc
        if not isinstance(relative, str):
            raise SetforgeError("Git visibility block has a non-string path")
        _validate_claim(VisibilityClaim(claim, relative))
        pattern = _pattern(relative)
        pattern_start = match.end()
        if body[pattern_start : pattern_start + len(pattern)] != pattern:
            raise SetforgeError("Git visibility block claim pattern is inconsistent")
        if claim in seen_ids or (claim, relative) in seen_paths:
            raise SetforgeError("Git visibility block has a duplicate claim")
        seen_ids.add(claim)
        seen_paths.add((claim, relative))
        claims.append(VisibilityClaim(claim, relative))
        offset = pattern_start + len(pattern)
    if tuple(claims) != tuple(sorted(claims)):
        raise SetforgeError("Git visibility block claims are not canonical")
    return payload[:start], tuple(claims), payload[end:]


def _render(prefix: bytes, claims: tuple[VisibilityClaim, ...], suffix: bytes) -> bytes:
    if not claims:
        return prefix + suffix
    body = bytearray(_BEGIN)
    for claim in sorted(claims):
        encoded_path = json.dumps(
            claim.relative_path, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        body.extend(f"# claim {claim.claim_id} ".encode("ascii"))
        body.extend(encoded_path)
        body.extend(b"\n")
        body.extend(_pattern(claim.relative_path))
    body.extend(_END)
    return prefix + bytes(body) + suffix


def _open_exclude_parent(path: Path) -> tuple[int, os.stat_result]:
    try:
        descriptor = os.open(
            path.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        info = os.fstat(descriptor)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise SetforgeError(
            f"Git visibility parent cannot be opened safely: {path.parent}: {exc}"
        ) from exc
    return descriptor, info


def _require_parent_identity(
    path: Path, parent_info: os.stat_result, *, expected: tuple[int, int] | None = None
) -> None:
    identity = (parent_info.st_dev, parent_info.st_ino)
    if expected is not None and identity != expected:
        raise SetforgeError("Git visibility parent changed before apply; retry")
    try:
        current = path.parent.stat(follow_symlinks=False)
    except OSError as exc:
        raise SetforgeError(
            "Git visibility parent changed before apply; retry"
        ) from exc
    if (
        not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != identity
    ):
        raise SetforgeError("Git visibility parent changed before apply; retry")


def _read_exclude_at(parent_fd: int, path: Path) -> tuple[bytes, int]:
    descriptor = -1
    try:
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise SetforgeError("Git visibility state is not a regular file")
        if info.st_size > 16 * 1024 * 1024:
            raise SetforgeError("Git visibility state is too large")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            payload = handle.read(16 * 1024 * 1024 + 1)
        if len(payload) > 16 * 1024 * 1024:
            raise SetforgeError("Git visibility state is too large")
    except FileNotFoundError:
        return b"", _DEFAULT_MODE
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SetforgeError(
                f"Git visibility state cannot be read: {path} is a symbolic link; "
                "replace it with a regular file or inject with --git-tracked"
            ) from exc
        raise SetforgeError(
            f"Git visibility state cannot be read: {path}: {exc}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return payload, stat.S_IMODE(info.st_mode)


def _read_state(path: Path) -> tuple[bytes, int, os.stat_result, bool]:
    """Read the exclude bytes and the directory identity that binds a plan.

    Git ignores a missing ``info`` directory or exclude file, so both read as
    empty. Without ``info`` the Git common directory binds the plan instead and
    the last value tells apply to create ``info`` there.
    """
    try:
        parent_fd, parent_info = _open_exclude_parent(path)
    except FileNotFoundError:
        try:
            return (
                b"",
                _DEFAULT_MODE,
                path.parent.parent.stat(follow_symlinks=False),
                True,
            )
        except OSError as exc:
            raise SetforgeError(
                f"Git visibility parent cannot be opened safely: {path.parent}: {exc}"
            ) from exc
    try:
        _require_parent_identity(path, parent_info)
        payload, mode = _read_exclude_at(parent_fd, path)
        _require_parent_identity(path, parent_info)
    finally:
        os.close(parent_fd)
    return payload, mode, parent_info, False


def read_claims(
    target: Path,
) -> tuple[Path, bytes, int, tuple[VisibilityClaim, ...]]:
    """Read and strictly validate SetForge claims without changing Git state."""
    path = info_exclude_path(target)
    payload, mode, _, _ = _read_state(path)
    _, claims, _ = _parse(payload)
    return path, payload, mode, claims


def plan_claims(
    target: Path,
    *,
    add: tuple[VisibilityClaim, ...] = (),
    remove: tuple[VisibilityClaim, ...] = (),
) -> VisibilityPlan:
    """Plan an exact claim update against the current exclude bytes."""
    path = info_exclude_path(target)
    before, before_mode, parent_info, create_parent = _read_state(path)
    prefix, current, suffix = _parse(before)
    by_id = {claim.claim_id: claim for claim in current}
    removed: list[VisibilityClaim] = []
    for claim in remove:
        _validate_claim(claim)
        observed = by_id.get(claim.claim_id)
        if observed != claim:
            raise SetforgeError("Git visibility claim is missing or mismatched")
        removed.append(by_id.pop(claim.claim_id))
    added: list[VisibilityClaim] = []
    for claim in add:
        _validate_claim(claim)
        observed = by_id.get(claim.claim_id)
        if observed is not None and observed != claim:
            raise SetforgeError("Git visibility claim identity collides")
        if observed is None:
            by_id[claim.claim_id] = claim
            added.append(claim)
    claims = tuple(sorted(by_id.values()))
    return VisibilityPlan(
        exclude_path=path,
        before=before,
        before_mode=before_mode,
        parent_device=parent_info.st_dev,
        parent_inode=parent_info.st_ino,
        after=_render(prefix, claims, suffix),
        added=tuple(added),
        removed=tuple(removed),
        create_parent=create_parent,
    )


def _create_exclude_parent(plan: VisibilityPlan) -> None:
    """Create the missing ``info`` directory inside the bound common directory."""
    try:
        common_fd = os.open(
            plan.exclude_path.parent.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError as exc:
        raise SetforgeError(
            "Git visibility parent changed before apply; retry"
        ) from exc
    try:
        info = os.fstat(common_fd)
        if (info.st_dev, info.st_ino) != (plan.parent_device, plan.parent_inode):
            raise SetforgeError("Git visibility parent changed before apply; retry")
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
    try:
        parent_fd, parent_info = _open_exclude_parent(plan.exclude_path)
    except FileNotFoundError as exc:
        raise SetforgeError(
            "Git visibility parent changed before apply; retry"
        ) from exc
    try:
        expected = (
            None if plan.create_parent else (plan.parent_device, plan.parent_inode)
        )
        _require_parent_identity(plan.exclude_path, parent_info, expected=expected)
        current, current_mode = _read_exclude_at(parent_fd, plan.exclude_path)
        if current != plan.before or current_mode != plan.before_mode:
            raise SetforgeError("Git visibility state changed before apply; retry")
        _require_parent_identity(plan.exclude_path, parent_info, expected=expected)
        if plan.changed:
            atomicio.atomic_write_bytes_at(
                parent_fd,
                plan.exclude_path.name,
                plan.after,
                mode=plan.before_mode,
            )
            _require_parent_identity(plan.exclude_path, parent_info, expected=expected)
    finally:
        os.close(parent_fd)
