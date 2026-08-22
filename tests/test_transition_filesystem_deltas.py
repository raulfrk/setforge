"""Arbitrary-byte and symlink transition-delta regressions."""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path

import pytest

from setforge import operations, orphan_scan, transitions
from setforge.errors import InvalidTransitionRecord, RevertFailed, SetforgeError


def test_staging_event_witness_rejects_queue_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temporary = ".managed.setforge-remove"
    encoded = temporary.encode() + b"\0"
    padded = encoded + b"\0" * (-len(encoded) % 4)
    overflow = struct.pack("iIII", -1, 0x00004000, 0, 0)
    created = struct.pack("iIII", 1, 0x00000100, 0, len(padded)) + padded
    reads: list[bytes] = [overflow + created]

    def read_once(descriptor: int, size: int) -> bytes:
        del descriptor, size
        if reads:
            return reads.pop()
        raise BlockingIOError

    monkeypatch.setattr(os, "read", read_once)

    with pytest.raises(SetforgeError, match="changed since transition"):
        operations._verify_staging_create_event(123, temporary, tmp_path / "managed")


def _write_transition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    deltas: tuple[transitions.FilesystemDelta, ...],
) -> transitions.TransitionDir:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    return transitions.write_transition(
        transitions.make_meta(transitions.TransitionCommand.CLEANUP_ORPHANS, "p"),
        {},
        {},
        None,
        filesystem_deltas=deltas,
    )


def test_filesystem_delta_round_trips_binary_symlink_mode_and_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "binary"
    binary.write_bytes(b"\x00\xffpayload")
    binary.chmod(0o640)
    binary_mtime = 1_700_000_000_123_456_789
    os.utime(binary, ns=(binary_mtime, binary_mtime))
    target = tmp_path / "target"
    target.write_text("important", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to("target")
    link_mtime = 1_700_000_000_223_456_789
    os.utime(link, ns=(link_mtime, link_mtime), follow_symlinks=False)
    deltas = transitions.filesystem_deletion_deltas((binary, link))
    guards = orphan_scan.capture_parent_path_guards((binary, link))
    transition = _write_transition(monkeypatch, tmp_path, deltas)
    binary.unlink()
    link.unlink()

    loaded = transitions.load_filesystem_deltas(transition)
    operations.apply_filesystem_deltas_reverse_anchored(loaded, guards)

    assert binary.read_bytes() == b"\x00\xffpayload"
    assert binary.stat().st_mode & 0o777 == 0o640
    assert binary.stat().st_mtime_ns == binary_mtime
    assert link.is_symlink()
    assert link.readlink() == Path("target")
    assert link.lstat().st_mtime_ns == link_mtime
    assert target.read_text(encoding="utf-8") == "important"

    operations.apply_filesystem_deltas_reverse_anchored(
        transitions.reverse_filesystem_deltas(loaded), guards
    )
    assert not binary.exists()
    assert not link.is_symlink()


@pytest.mark.parametrize("with_child", [False, True])
def test_filesystem_delta_reverse_restores_parent_directory_metadata(
    tmp_path: Path, with_child: bool
) -> None:
    parent = tmp_path / "managed"
    parent.mkdir()
    parent.chmod(0o700)
    original_mtime = 1_700_000_000_333_456_789
    os.utime(parent, ns=(original_mtime, original_mtime))
    child = parent / "child"
    parent_pre = transitions.snapshot_filesystem_image(parent)
    child_pre = transitions.snapshot_filesystem_image(child)
    if with_child:
        child.write_text("created", encoding="utf-8")
    parent.chmod(0o755)
    deltas = [
        transitions.FilesystemDelta(
            parent, parent_pre, transitions.snapshot_filesystem_image(parent)
        ),
    ]
    if with_child:
        deltas.append(
            transitions.FilesystemDelta(
                child, child_pre, transitions.snapshot_filesystem_image(child)
            )
        )
    guards = orphan_scan.capture_parent_path_guards((parent, child))

    operations.apply_filesystem_deltas_reverse_anchored(tuple(deltas), guards)

    assert not child.exists()
    assert parent.stat().st_mode & 0o777 == 0o700
    assert parent.stat().st_mtime_ns == original_mtime


def test_filesystem_delta_reverse_recreates_deleted_directory_tree(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "managed"
    parent.mkdir()
    child = parent / "child"
    child.write_text("payload", encoding="utf-8")
    deltas = transitions.filesystem_deletion_deltas((parent, child))
    child.unlink()
    parent.rmdir()
    guards = orphan_scan.capture_parent_path_guards((parent, child))

    operations.apply_filesystem_deltas_reverse_anchored(deltas, guards)

    assert child.read_text(encoding="utf-8") == "payload"


def test_filesystem_delta_directory_recreation_refuses_existing_collision(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "managed"
    parent.mkdir()
    delta = transitions.filesystem_deletion_deltas((parent,))[0]
    parent.rmdir()
    guards = orphan_scan.capture_parent_path_guards((parent,))
    parent.mkdir()
    parent.chmod(0o711)

    with pytest.raises(SetforgeError, match="changed since transition"):
        operations.apply_filesystem_deltas_reverse_anchored((delta,), guards)

    assert parent.stat().st_mode & 0o777 == 0o711
    assert not (tmp_path / ".managed.setforge-remove").exists()


def test_filesystem_delta_directory_recreation_refuses_preidentity_staging_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "managed"
    parent.mkdir()
    delta = transitions.filesystem_deletion_deltas((parent,))[0]
    parent.rmdir()
    guards = orphan_scan.capture_parent_path_guards((parent,))
    original_mkdir = os.mkdir
    injected: Path | None = None
    injected_mode: int | None = None

    def collide(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal injected, injected_mode
        if path == ".managed.setforge-remove":
            original_mkdir(path, 0o711, dir_fd=dir_fd)
            injected = tmp_path / path
            injected_mode = injected.stat().st_mode
        original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", collide)
    with pytest.raises(SetforgeError, match="staging collision"):
        operations.apply_filesystem_deltas_reverse_anchored((delta,), guards)

    assert injected is not None
    assert injected.stat().st_mode == injected_mode
    assert not parent.exists()


def test_filesystem_delta_directory_recreation_refuses_swap_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "managed"
    parent.mkdir()
    delta = transitions.filesystem_deletion_deltas((parent,))[0]
    parent.rmdir()
    guards = orphan_scan.capture_parent_path_guards((parent,))
    original_open = os.open
    swapped = False

    def swap_before_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == ".managed.setforge-remove" and dir_fd is not None and not swapped:
            swapped = True
            staged = tmp_path / ".managed.setforge-remove"
            staged.rename(tmp_path / "detached")
            staged.mkdir()
            staged.chmod(0o711)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swap_before_open)
    with pytest.raises(SetforgeError, match="changed since transition"):
        operations.apply_filesystem_deltas_reverse_anchored((delta,), guards)

    assert not parent.exists()
    assert (tmp_path / ".managed.setforge-remove").stat().st_mode & 0o777 == 0o711
    assert (tmp_path / "detached").stat().st_mode & 0o777 != 0o711


def test_filesystem_delta_directory_recreation_refuses_swap_before_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "managed"
    parent.mkdir()
    delta = transitions.filesystem_deletion_deltas((parent,))[0]
    parent.rmdir()
    guards = orphan_scan.capture_parent_path_guards((parent,))
    original_mkdir = os.mkdir
    swapped = False

    def swap_after_mkdir(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        original_mkdir(path, mode, dir_fd=dir_fd)
        if path == ".managed.setforge-remove" and not swapped:
            swapped = True
            staged = tmp_path / ".managed.setforge-remove"
            staged.rename(tmp_path / "detached")
            staged.mkdir()
            staged.chmod(0o711)

    monkeypatch.setattr(os, "mkdir", swap_after_mkdir)
    with pytest.raises(SetforgeError, match="changed since transition"):
        operations.apply_filesystem_deltas_reverse_anchored((delta,), guards)

    assert not parent.exists()
    assert (tmp_path / ".managed.setforge-remove").stat().st_mode & 0o777 == 0o711


def test_filesystem_delta_collision_cleanup_preserves_swapped_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "managed"
    parent.mkdir()
    delta = transitions.filesystem_deletion_deltas((parent,))[0]
    parent.rmdir()
    guards = orphan_scan.capture_parent_path_guards((parent,))
    parent.mkdir()
    original_rename = operations._rename_noreplace_at

    def swap_then_collide(parent_fd: int, source: str, destination: str) -> None:
        staged = tmp_path / source
        staged.rename(tmp_path / "detached")
        staged.mkdir()
        staged.chmod(0o711)
        original_rename(parent_fd, source, destination)

    monkeypatch.setattr(operations, "_rename_noreplace_at", swap_then_collide)
    with pytest.raises(SetforgeError, match="changed since transition"):
        operations.apply_filesystem_deltas_reverse_anchored((delta,), guards)

    assert (tmp_path / ".managed.setforge-remove").stat().st_mode & 0o777 == 0o711
    assert (tmp_path / "detached").is_dir()


def test_filesystem_delta_metadata_refuses_replaced_restored_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "managed"
    parent.mkdir()
    delta = transitions.filesystem_deletion_deltas((parent,))[0]
    parent.rmdir()
    guards = orphan_scan.capture_parent_path_guards((parent,))
    original = operations._restore_directory_metadata_anchored

    def swap_then_restore(
        item: transitions.FilesystemDelta,
        identities: dict[Path, tuple[int, int, int] | None],
    ) -> None:
        item.path.rename(tmp_path / "detached")
        item.path.mkdir()
        item.path.chmod(0o711)
        original(item, identities)

    monkeypatch.setattr(
        operations, "_restore_directory_metadata_anchored", swap_then_restore
    )

    with pytest.raises(SetforgeError, match="changed since transition"):
        operations.apply_filesystem_deltas_reverse_anchored((delta,), guards)

    assert parent.stat().st_mode & 0o777 == 0o711


def test_filesystem_delta_directory_restore_keeps_validated_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "managed"
    parent.mkdir()
    parent.chmod(0o700)
    pre = transitions.snapshot_filesystem_image(parent)
    parent.chmod(0o755)
    post = transitions.snapshot_filesystem_image(parent)
    delta = transitions.FilesystemDelta(parent, pre, post)
    guards = orphan_scan.capture_parent_path_guards((parent,))
    original_fchmod = os.fchmod
    swapped = False

    def swap_before_chmod(descriptor: int, mode: int) -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            parent.rename(tmp_path / "detached")
            parent.mkdir()
            parent.chmod(0o755)
        original_fchmod(descriptor, mode)

    monkeypatch.setattr(os, "fchmod", swap_before_chmod)

    with pytest.raises(SetforgeError, match="changed since transition"):
        operations.apply_filesystem_deltas_reverse_anchored((delta,), guards)

    assert parent.stat().st_mode & 0o777 == 0o755
    assert (tmp_path / "detached").stat().st_mode & 0o777 == 0o700


def test_filesystem_delta_writer_canonicalizes_and_rejects_double_slash_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "candidate"
    path.write_bytes(b"payload")
    double_slash = Path(f"/{path}")

    constructed = transitions.filesystem_deletion_deltas((double_slash,))
    assert constructed[0].path == path
    transition = _write_transition(monkeypatch, tmp_path, constructed)
    assert transitions.load_filesystem_deltas(transition)[0].path == path

    image = transitions.snapshot_filesystem_image(path)
    aliases = (
        transitions.FilesystemDelta(
            double_slash,
            image,
            transitions.FilesystemImage(transitions.FilesystemKind.ABSENT),
        ),
        transitions.FilesystemDelta(
            path,
            image,
            transitions.FilesystemImage(transitions.FilesystemKind.ABSENT),
        ),
    )
    with pytest.raises(SetforgeError, match="paths must be unique"):
        _write_transition(monkeypatch, tmp_path, aliases)


def test_filesystem_delta_refuses_post_transition_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "candidate"
    path.write_text("before", encoding="utf-8")
    transition = _write_transition(
        monkeypatch,
        tmp_path,
        transitions.filesystem_deletion_deltas((path,)),
    )
    path.unlink()
    path.write_text("user replacement", encoding="utf-8")

    with pytest.raises(RevertFailed, match="changed since transition"):
        transitions.validate_filesystem_deltas_reverse(
            transitions.load_filesystem_deltas(transition)
        )

    assert path.read_text(encoding="utf-8") == "user replacement"


def test_filesystem_delta_round_trips_empty_file_and_old_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "empty"
    path.touch()
    transition = _write_transition(
        monkeypatch,
        tmp_path,
        transitions.filesystem_deletion_deltas((path,)),
    )
    guards = orphan_scan.capture_parent_path_guards((path,))
    path.unlink()

    operations.apply_filesystem_deltas_reverse_anchored(
        transitions.load_filesystem_deltas(transition),
        guards,
    )
    assert path.read_bytes() == b""

    old = transitions.write_transition(
        transitions.make_meta(transitions.TransitionCommand.INSTALL, "p"),
        {},
        {},
        None,
    )
    assert transitions.load_filesystem_deltas(old) == ()


def test_filesystem_delta_rejects_corrupt_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "candidate"
    path.write_text("before", encoding="utf-8")
    transition = _write_transition(
        monkeypatch,
        tmp_path,
        transitions.filesystem_deletion_deltas((path,)),
    )
    payload_path = transition / "filesystem_deltas.json"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    payload["entries"][0]["pre"]["payload_b64"] = "%%%"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(InvalidTransitionRecord, match=r"filesystem_deltas\.json"):
        transitions.load_filesystem_deltas(transition)


def test_filesystem_delta_rejects_lexical_alias_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "candidate"
    path.write_text("before", encoding="utf-8")
    transition = _write_transition(
        monkeypatch,
        tmp_path,
        transitions.filesystem_deletion_deltas((path,)),
    )
    payload_path = transition / "filesystem_deltas.json"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    alias = dict(payload["entries"][0])
    alias["path"] = str(tmp_path / "alias" / ".." / path.name)
    payload["entries"].append(alias)
    payload_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(InvalidTransitionRecord, match="lexically normalized"):
        transitions.load_filesystem_deltas(transition)


def test_filesystem_delta_rejects_double_leading_slash_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "candidate"
    path.write_text("before", encoding="utf-8")
    transition = _write_transition(
        monkeypatch,
        tmp_path,
        transitions.filesystem_deletion_deltas((path,)),
    )
    payload_path = transition / "filesystem_deltas.json"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    payload["entries"][0]["path"] = f"/{path}"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(InvalidTransitionRecord, match="lexically normalized"):
        transitions.load_filesystem_deltas(transition)


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_anchored_reverse_refuses_parent_swap_between_validation_and_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    parent = tmp_path / "managed"
    parent.mkdir()
    candidate = parent / "candidate"
    target = tmp_path / "target"
    target.write_text("target", encoding="utf-8")
    if kind == "file":
        candidate.write_bytes(b"original")
    else:
        candidate.symlink_to(target)
    deltas = transitions.filesystem_deletion_deltas((candidate,))
    guards = orphan_scan.capture_parent_path_guards((candidate,))
    candidate.unlink()
    moved = tmp_path / "managed-moved"
    attacker = tmp_path / "attacker"
    real_snapshot = operations._snapshot_path_at
    swapped = False

    def _swap_after_validation(parent_fd: int, path: Path):
        nonlocal swapped
        snapshot = real_snapshot(parent_fd, path)
        if not swapped:
            swapped = True
            parent.rename(moved)
            attacker.mkdir()
            (attacker / "candidate").write_bytes(b"external")
            parent.symlink_to(attacker, target_is_directory=True)
        return snapshot

    monkeypatch.setattr(operations, "_snapshot_path_at", _swap_after_validation)

    with pytest.raises(SetforgeError, match="parent changed before write"):
        operations.apply_filesystem_deltas_reverse_anchored(deltas, guards)

    assert (attacker / "candidate").read_bytes() == b"external"
    assert not (moved / "candidate").exists()
    assert target.read_text(encoding="utf-8") == "target"
