"""Selective Git filtering for project-profile overlays on tracked files."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO

from setforge import atomicio
from setforge.errors import SetforgeError
from setforge.reconcile.hunks import classify, extract_hunks, serialize
from setforge.reconcile.merge import split_lines
from setforge.reconcile.types import HunkClass
from setforge.transitions import state_root

_SCHEMA = 1
_MAX_STATE = 16 * 1024 * 1024
_MAX_PACKET = 65516
_DRIVER = "setforge-project"


@dataclass(frozen=True, slots=True)
class ProjectOverlay:
    """One exact tracked-file overlay and its LOCAL hunk classifications."""

    target: Path
    target_device: int
    target_inode: int
    relative_path: Path
    base: bytes
    local: bytes
    hunks: tuple[dict[str, object], ...]


def overlay_path(target: Path, relative_path: Path) -> Path:
    """Return the private state path for one canonical target/path pair."""
    key = json.dumps(
        {"path": relative_path.as_posix(), "target": str(target)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return state_root() / "project-overlays" / f"{hashlib.sha256(key).hexdigest()}.json"


def build_overlay(
    target: Path, relative_path: Path, base: bytes, local: bytes
) -> ProjectOverlay:
    """Classify every initial base-to-local difference as private LOCAL content."""
    root = target.resolve(strict=True)
    relative = _validated_relative(relative_path)
    info = root.stat()
    hunks = tuple(
        serialize(
            [replace(hunk, cls=HunkClass.LOCAL) for hunk in extract_hunks(base, local)]
        )
    )
    return ProjectOverlay(
        target=root,
        target_device=info.st_dev,
        target_inode=info.st_ino,
        relative_path=relative,
        base=base,
        local=local,
        hunks=hunks,
    )


def write_overlay(overlay: ProjectOverlay) -> None:
    """Atomically persist one prevalidated overlay."""
    payload = {
        "base": base64.b64encode(overlay.base).decode("ascii"),
        "hunks": list(overlay.hunks),
        "local": base64.b64encode(overlay.local).decode("ascii"),
        "path": overlay.relative_path.as_posix(),
        "schema": _SCHEMA,
        "target": str(overlay.target),
        "target_device": overlay.target_device,
        "target_inode": overlay.target_inode,
    }
    atomicio.atomic_write_text(
        overlay_path(overlay.target, overlay.relative_path),
        json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n",
        mode=0o600,
    )


def read_overlay(target: Path, relative_path: Path) -> ProjectOverlay | None:
    """Read and verify one overlay, returning ``None`` when it is not managed."""
    root = target.resolve(strict=True)
    relative = _validated_relative(relative_path)
    path = overlay_path(root, relative)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SetforgeError(f"project overlay cannot be opened: {path}: {exc}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_STATE:
            raise SetforgeError(f"project overlay is not bounded state: {path}")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            raw = json.loads(handle.read(_MAX_STATE + 1))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise SetforgeError(f"project overlay is corrupt: {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    required = {
        "base",
        "hunks",
        "local",
        "path",
        "schema",
        "target",
        "target_device",
        "target_inode",
    }
    if not isinstance(raw, dict) or set(raw) != required or raw["schema"] != _SCHEMA:
        raise SetforgeError(f"project overlay has invalid fields: {path}")
    current = root.stat()
    if (
        raw["target"] != str(root)
        or raw["path"] != relative.as_posix()
        or raw["target_device"] != current.st_dev
        or raw["target_inode"] != current.st_ino
        or not isinstance(raw["hunks"], list)
    ):
        raise SetforgeError(f"project overlay identity changed: {path}")
    try:
        base = base64.b64decode(raw["base"], validate=True)
        local = base64.b64decode(raw["local"], validate=True)
    except (TypeError, ValueError) as exc:
        raise SetforgeError(f"project overlay payload is invalid: {path}") from exc
    classified = classify(extract_hunks(base, local), raw["hunks"])
    if any(hunk.cls is not HunkClass.LOCAL or hunk.changed for hunk in classified):
        raise SetforgeError(f"project overlay hunk state is inconsistent: {path}")
    return ProjectOverlay(
        target=root,
        target_device=current.st_dev,
        target_inode=current.st_ino,
        relative_path=relative,
        base=base,
        local=local,
        hunks=tuple(raw["hunks"]),
    )


def clean_content(overlay: ProjectOverlay, content: bytes) -> bytes:
    """Remove exact LOCAL units while retaining every unrelated live edit."""
    base_lines = split_lines(overlay.base)
    local_lines = split_lines(overlay.local)
    output = split_lines(content)
    hunks = extract_hunks(overlay.base, overlay.local)
    for hunk in reversed(hunks):
        i1, i2 = hunk.base_span
        j1, j2 = hunk.live_span
        local_region = local_lines[j1:j2]
        if local_region:
            matches = _subsequence_matches(output, local_region)
        else:
            matches = _boundary_matches(output, local_lines, j1)
        if len(matches) != 1:
            raise SetforgeError(
                "tracked project overlay overlaps another edit; "
                "resolve it before Git staging"
            )
        start = matches[0]
        output[start : start + len(local_region)] = base_lines[i1:i2]
    return b"".join(output)


def smudge_content(overlay: ProjectOverlay, content: bytes) -> bytes:
    """Reapply LOCAL units to their exact recorded Git-facing base."""
    if content == overlay.local:
        return content
    if content != overlay.base:
        raise SetforgeError(
            "tracked project file changed upstream; "
            "run setforge project sync before checkout"
        )
    return overlay.local


def update_local_content(old_local: bytes, new_local: bytes, content: bytes) -> bytes:
    """Replay exact profile-update hunks while preserving unrelated live edits."""
    old_lines = split_lines(old_local)
    new_lines = split_lines(new_local)
    output = split_lines(content)
    for hunk in reversed(extract_hunks(old_local, new_local)):
        i1, i2 = hunk.base_span
        j1, j2 = hunk.live_span
        old_region = old_lines[i1:i2]
        if old_region:
            matches = _subsequence_matches(output, old_region)
        else:
            matches = _boundary_matches(output, old_lines, i1)
        if len(matches) != 1:
            raise SetforgeError(
                "tracked project profile update overlaps another edit; "
                "resolve it explicitly"
            )
        start = matches[0]
        output[start : start + len(old_region)] = new_lines[j1:j2]
    return b"".join(output)


def process_filter(
    stdin: BinaryIO = sys.stdin.buffer, stdout: BinaryIO = sys.stdout.buffer
) -> None:
    """Serve Git's version-2 long-running filter protocol until EOF."""
    hello = _read_list(stdin)
    if hello != [b"git-filter-client", b"version=2"]:
        raise SetforgeError("unsupported Git filter handshake")
    _write_list(stdout, [b"git-filter-server", b"version=2"])
    stdout.flush()
    capabilities = _read_list(stdin)
    if capabilities is None or not {
        b"capability=clean",
        b"capability=smudge",
    }.issubset(capabilities):
        raise SetforgeError("Git filter lacks required clean/smudge capabilities")
    _write_list(stdout, [b"capability=clean", b"capability=smudge"])
    stdout.flush()
    target = _filter_target()
    while True:
        headers = _read_list(stdin, allow_eof=True)
        if headers is None:
            return
        values = _headers(headers)
        command = values.get("command")
        pathname = values.get("pathname")
        if command not in {"clean", "smudge"} or pathname is None:
            raise SetforgeError("Git filter request is invalid")
        content = b"".join(_read_packets(stdin))
        relative = _validated_relative(Path(pathname))
        overlay = read_overlay(target, relative)
        if overlay is None:
            result = content
        elif command == "clean":
            result = clean_content(overlay, content)
        else:
            result = smudge_content(overlay, content)
        _write_list(stdout, [b"status=success"])
        _write_packets(stdout, result)
        _write_list(stdout, [])
        stdout.flush()


def driver_name() -> str:
    """Return the stable Git filter driver name."""
    return _DRIVER


def _filter_target() -> Path:
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
    try:
        value = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            env=environment,
            text=True,
            timeout=10,
        ).stdout.strip()
        return Path(value).resolve(strict=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SetforgeError("Git filter is not running in a worktree") from exc


def _validated_relative(path: Path) -> Path:
    if path.is_absolute() or path == Path() or ".." in path.parts:
        raise SetforgeError("project overlay path is not normalized")
    normalized = Path(*path.parts)
    if normalized.as_posix() != path.as_posix() or "\x00" in path.as_posix():
        raise SetforgeError("project overlay path is not normalized")
    return normalized


def _subsequence_matches(haystack: list[bytes], needle: list[bytes]) -> list[int]:
    return [
        offset
        for offset in range(len(haystack) - len(needle) + 1)
        if haystack[offset : offset + len(needle)] == needle
    ]


def _boundary_matches(
    content: list[bytes], local: list[bytes], boundary: int
) -> list[int]:
    before = local[max(0, boundary - 3) : boundary]
    after = local[boundary : boundary + 3]
    return [
        offset
        for offset in range(len(content) + 1)
        if content[max(0, offset - len(before)) : offset] == before
        and content[offset : offset + len(after)] == after
    ]


def _read_packet(stream: BinaryIO, *, allow_eof: bool = False) -> bytes | None:
    header = stream.read(4)
    if not header and allow_eof:
        return None
    if len(header) != 4:
        raise SetforgeError("truncated Git filter packet")
    try:
        length = int(header, 16)
    except ValueError as exc:
        raise SetforgeError("invalid Git filter packet length") from exc
    if length == 0:
        return b""
    if length < 4 or length > 65520:
        raise SetforgeError("invalid Git filter packet length")
    payload = stream.read(length - 4)
    if len(payload) != length - 4:
        raise SetforgeError("truncated Git filter packet")
    return payload


def _read_list(stream: BinaryIO, *, allow_eof: bool = False) -> list[bytes] | None:
    first = _read_packet(stream, allow_eof=allow_eof)
    if first is None:
        return None
    rows: list[bytes] = []
    packet: bytes | None = first
    while packet:
        rows.append(packet.rstrip(b"\n"))
        packet = _read_packet(stream)
        assert packet is not None
    return rows


def _read_packets(stream: BinaryIO) -> list[bytes]:
    rows: list[bytes] = []
    packet = _read_packet(stream)
    assert packet is not None
    while packet:
        rows.append(packet)
        packet = _read_packet(stream)
        assert packet is not None
    return rows


def _headers(rows: list[bytes]) -> dict[str, str]:
    values: dict[str, str] = {}
    for row in rows:
        try:
            key, value = row.decode("utf-8").split("=", 1)
        except (UnicodeDecodeError, ValueError) as exc:
            raise SetforgeError("Git filter header is invalid") from exc
        if key in values:
            raise SetforgeError("Git filter header is duplicated")
        values[key] = value
    return values


def _write_list(stream: BinaryIO, rows: list[bytes]) -> None:
    for row in rows:
        _write_packet(stream, row + b"\n")
    stream.write(b"0000")


def _write_packets(stream: BinaryIO, content: bytes) -> None:
    for offset in range(0, len(content), _MAX_PACKET):
        _write_packet(stream, content[offset : offset + _MAX_PACKET])
    stream.write(b"0000")


def _write_packet(stream: BinaryIO, payload: bytes) -> None:
    stream.write(f"{len(payload) + 4:04x}".encode("ascii") + payload)
