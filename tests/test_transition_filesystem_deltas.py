"""Arbitrary-byte and symlink transition-delta regressions."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from setforge import operations, orphan_scan, transitions
from setforge.errors import InvalidTransitionRecord, RevertFailed, SetforgeError


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
        transitions.load_filesystem_deltas(transition), guards
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
