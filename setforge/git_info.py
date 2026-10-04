"""Shared plumbing for the private files SetForge edits inside a Git directory.

``info/exclude`` and ``info/attributes`` each carry one SetForge-managed block
of claims. Every byte outside that block belongs to the user and is preserved.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from setforge.errors import SetforgeError

_CLAIM = re.compile(rb"# claim ([0-9a-f]{64}) (.+)\n")
_CLAIM_ID = re.compile(r"[0-9a-f]{64}")
_MAX_FILE = 16 * 1024 * 1024


def run_git(
    target: Path,
    args: list[str],
    *,
    check: bool = True,
    pass_fds: tuple[int, ...] = (),
    failure: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run Git in ``target`` without prompts and with untranslated output.

    With ``failure``, a Git that cannot run, or exits non-zero under ``check``,
    raises :class:`SetforgeError` starting with that text. Without it the
    subprocess error reaches the caller.
    """
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
        if failure is None:
            raise
        detail = getattr(exc, "stderr", None) or str(exc)
        raise SetforgeError(f"{failure}: {detail}") from exc


def claim_identity(git_dir: Path, profile: str, relative_path: str) -> str:
    """Return the stable identity of one worktree/profile/path claim."""
    payload = json.dumps(
        {"git_dir": str(git_dir), "profile": profile, "relative_path": relative_path},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True, order=True)
class GitClaim:
    """One injection's claim on an exact repository-relative path."""

    claim_id: str
    relative_path: str


@dataclass(frozen=True, slots=True)
class ClaimBlock[C: GitClaim]:
    """The SetForge-managed block of one private Git file.

    ``pattern`` renders the Git line a claim stands for and refuses a path the
    file cannot represent. A block without claims is removed entirely, which
    restores the file's earlier bytes.
    """

    label: str
    begin: bytes
    end: bytes
    claim_type: type[C]
    pattern: Callable[[str], bytes]

    def validate(self, claim: C) -> None:
        """Refuse a claim this block cannot store."""
        if _CLAIM_ID.fullmatch(claim.claim_id) is None:
            raise SetforgeError(f"{self.label} claim has an invalid identity")
        self.pattern(claim.relative_path)

    def parse(self, payload: bytes) -> tuple[bytes, tuple[C, ...], bytes]:
        """Split a file into the user's prefix, the claims, and the user's suffix."""
        starts, ends = payload.count(self.begin), payload.count(self.end)
        if starts == 0 and ends == 0:
            return payload, (), b""
        if starts != 1 or ends != 1:
            raise SetforgeError(
                f"{self.label} block is missing, duplicated, or ambiguous"
            )
        start = payload.index(self.begin)
        end_start = payload.index(self.end)
        if end_start < start:
            raise SetforgeError(f"{self.label} block markers are out of order")
        body = payload[start + len(self.begin) : end_start]
        claims: list[C] = []
        offset = 0
        while offset < len(body):
            match = _CLAIM.match(body, offset)
            if match is None:
                raise SetforgeError(f"{self.label} block has an invalid claim")
            claim = self._claim(match)
            pattern = self.pattern(claim.relative_path)
            if body[match.end() : match.end() + len(pattern)] != pattern:
                raise SetforgeError(f"{self.label} claim pattern is inconsistent")
            claims.append(claim)
            offset = match.end() + len(pattern)
        if len({claim.claim_id for claim in claims}) != len(claims):
            raise SetforgeError(f"{self.label} block has a duplicate claim")
        if claims != sorted(claims):
            raise SetforgeError(f"{self.label} block claims are not canonical")
        return payload[:start], tuple(claims), payload[end_start + len(self.end) :]

    def _claim(self, match: re.Match[bytes]) -> C:
        try:
            relative = json.loads(match.group(2))
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise SetforgeError(f"{self.label} claim path is invalid") from exc
        if not isinstance(relative, str):
            raise SetforgeError(f"{self.label} claim path is invalid")
        claim = self.claim_type(match.group(1).decode("ascii"), relative)
        self.validate(claim)
        return claim

    def render(self, prefix: bytes, claims: Iterable[C], suffix: bytes) -> bytes:
        """Join the user's bytes around the canonical block for ``claims``."""
        body = bytearray()
        for claim in sorted(claims):
            body.extend(f"# claim {claim.claim_id} ".encode("ascii"))
            body.extend(
                json.dumps(
                    claim.relative_path, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            )
            body.extend(b"\n")
            body.extend(self.pattern(claim.relative_path))
        if not body:
            return prefix + suffix
        return prefix + self.begin + bytes(body) + self.end + suffix

    def update(
        self, payload: bytes, *, add: tuple[C, ...], remove: tuple[C, ...]
    ) -> tuple[bytes, tuple[C, ...], tuple[C, ...], tuple[C, ...]]:
        """Return the new file bytes and the claims added, removed, and kept.

        Removing a claim the block does not hold is refused; adding one it
        already holds changes nothing.
        """
        prefix, current, suffix = self.parse(payload)
        by_id = {claim.claim_id: claim for claim in current}
        removed: list[C] = []
        for claim in remove:
            self.validate(claim)
            if by_id.get(claim.claim_id) != claim:
                raise SetforgeError(f"{self.label} claim is missing or mismatched")
            removed.append(by_id.pop(claim.claim_id))
        added: list[C] = []
        for claim in add:
            self.validate(claim)
            observed = by_id.get(claim.claim_id)
            if observed is not None and observed != claim:
                raise SetforgeError(f"{self.label} claim identity collides")
            if observed is None:
                by_id[claim.claim_id] = claim
                added.append(claim)
        remaining = tuple(sorted(by_id.values()))
        return (
            self.render(prefix, remaining, suffix),
            tuple(added),
            tuple(removed),
            remaining,
        )


def open_directory(path: Path, what: str) -> tuple[int, os.stat_result]:
    """Open a directory itself, never the target of a link in its place."""
    try:
        descriptor = os.open(
            path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        return descriptor, os.fstat(descriptor)
    except OSError as exc:
        raise SetforgeError(f"{what} cannot be opened safely: {path}: {exc}") from exc


def open_info(path: Path, what: str) -> tuple[int, os.stat_result] | None:
    """Open the Git ``info`` directory, or return ``None`` when it is missing.

    Git ignores a missing ``info`` directory, so its files read as empty. The
    Git common directory then binds a plan and apply creates ``info`` there.
    """
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    return open_directory(path, what)


def require_directory_identity(
    path: Path,
    info: os.stat_result,
    what: str,
    *,
    expected: tuple[int, int] | None = None,
) -> None:
    """Refuse a directory that is no longer the one opened, or the one planned."""
    identity = (info.st_dev, info.st_ino)
    changed = f"{what} changed before apply; retry"
    if expected is not None and identity != expected:
        raise SetforgeError(changed)
    try:
        current = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise SetforgeError(changed) from exc
    if (
        not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != identity
    ):
        raise SetforgeError(changed)


def read_bounded_at(
    parent_fd: int, path: Path, label: str, *, missing_mode: int
) -> tuple[bytes, int]:
    """Read ``path`` by name inside its opened parent, with its mode bits.

    A missing file reads as empty with ``missing_mode``. A symbolic link, a
    non-regular file and a file over 16 MiB are refused.
    """
    descriptor = -1
    try:
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd
        )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise SetforgeError(f"{label} state is not a regular file: {path}")
        if info.st_size > _MAX_FILE:
            raise SetforgeError(f"{label} state is too large: {path}")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            payload = handle.read(_MAX_FILE + 1)
        if len(payload) > _MAX_FILE:
            raise SetforgeError(f"{label} state is too large: {path}")
    except FileNotFoundError:
        return b"", missing_mode
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SetforgeError(
                f"{label} state cannot be read: {path} is a symbolic link; "
                "replace it with a regular file or inject with --git-tracked"
            ) from exc
        raise SetforgeError(f"{label} state cannot be read: {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return payload, stat.S_IMODE(info.st_mode)
