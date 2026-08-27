"""Private exact-path Git visibility for injected project files."""

from __future__ import annotations

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
        resolved_parent = path.parent.resolve(strict=True)
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
    except OSError as exc:
        raise SetforgeError(
            f"Git visibility state cannot be read: {path}: {exc}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return payload, stat.S_IMODE(info.st_mode)


def read_claims(
    target: Path,
) -> tuple[Path, bytes, int, tuple[VisibilityClaim, ...]]:
    """Read and strictly validate SetForge claims without changing Git state."""
    path = info_exclude_path(target)
    parent_fd, parent_info = _open_exclude_parent(path)
    try:
        _require_parent_identity(path, parent_info)
        payload, mode = _read_exclude_at(parent_fd, path)
        _require_parent_identity(path, parent_info)
    finally:
        os.close(parent_fd)
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
    parent_fd, parent_info = _open_exclude_parent(path)
    try:
        _require_parent_identity(path, parent_info)
        before, before_mode = _read_exclude_at(parent_fd, path)
        _require_parent_identity(path, parent_info)
    finally:
        os.close(parent_fd)
    _, current, _ = _parse(before)
    prefix, parsed, suffix = _parse(before)
    assert parsed == current
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
    )


def plan_file_visibility(
    target: Path,
    *,
    claim: VisibilityClaim,
    hidden: bool,
    tracked_sibling_paths: frozenset[str],
) -> VisibilityPlan:
    """Plan one new/untracked injected file's hidden/tracked transition."""
    relative = Path(claim.relative_path)
    if relative.is_absolute() or relative == Path() or ".." in relative.parts:
        raise SetforgeError("Git visibility path is not normalized")
    destination = target / relative
    try:
        info = destination.lstat()
    except OSError as exc:
        raise SetforgeError(
            f"Git visibility destination cannot be read: {destination}: {exc}"
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SetforgeError("Git visibility destination is not an ordinary file")
    tracked = _run_git(
        target,
        ["ls-files", "--error-unmatch", "--", claim.relative_path],
        check=False,
    )
    if tracked.returncode == 0:
        raise SetforgeError(
            "Git visibility for an already-committed file is planned for G5"
        )
    if tracked.returncode != 1:
        detail = tracked.stderr.strip() or "unknown Git error"
        raise SetforgeError(f"cannot classify Git visibility destination: {detail}")
    _, _, _, current = read_claims(target)
    own = next((item for item in current if item.claim_id == claim.claim_id), None)
    if own is not None and own != claim:
        raise SetforgeError("Git visibility claim identity collides")
    if hidden:
        if claim.relative_path in tracked_sibling_paths:
            raise SetforgeError(
                "Git visibility conflicts with a tracked linked-worktree claim"
            )
        return plan_claims(target, add=(claim,))
    siblings = tuple(
        item
        for item in current
        if item.relative_path == claim.relative_path and item.claim_id != claim.claim_id
    )
    if siblings:
        raise SetforgeError(
            "Git visibility remains hidden by another linked-worktree claim"
        )
    return plan_claims(target, remove=(claim,) if own is not None else ())


def apply_claims(plan: VisibilityPlan) -> None:
    """Apply a byte-bound visibility plan atomically."""
    parent_fd, parent_info = _open_exclude_parent(plan.exclude_path)
    try:
        expected = (plan.parent_device, plan.parent_inode)
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
