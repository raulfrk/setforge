"""Tests for the shared atomic-write primitive."""

from pathlib import Path

import pytest

from setforge import atomicio


def test_atomic_write_bytes_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "file.bin"
    payload = b"\x00\x01binary\xffbytes\n"
    atomicio.atomic_write_bytes(target, payload)
    assert target.read_bytes() == payload


def test_atomic_write_text_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    text = "héllo\nwörld\r\n"
    atomicio.atomic_write_text(target, text)
    # Read as bytes so universal-newline translation does not mask the
    # exact-bytes guarantee.
    assert target.read_bytes() == text.encode("utf-8")


def test_atomic_write_bytes_no_fsync_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "file.bin"
    atomicio.atomic_write_bytes(target, b"data", fsync=False)
    assert target.read_bytes() == b"data"


def test_atomic_write_tempfile_in_target_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "nested" / "file.bin"
    real_mkstemp = atomicio.tempfile.mkstemp
    seen_dirs: list[str] = []

    def spy_mkstemp(*, dir: str, prefix: str, suffix: str) -> tuple[int, str]:
        seen_dirs.append(dir)
        return real_mkstemp(dir=dir, prefix=prefix, suffix=suffix)

    monkeypatch.setattr(atomicio.tempfile, "mkstemp", spy_mkstemp)
    atomicio.atomic_write_bytes(target, b"x")
    assert seen_dirs == [str(target.parent)]


def test_atomic_write_cleans_temp_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "file.bin"

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("replace blew up")

    # Fail after the temp file is fully written but before the rename
    # lands, exercising the except-cleanup path.
    monkeypatch.setattr(atomicio.os, "replace", boom)
    with pytest.raises(RuntimeError, match="replace blew up"):
        atomicio.atomic_write_bytes(target, b"payload")

    assert not target.exists()
    leftovers = list(tmp_path.glob(".*.tmp"))
    assert leftovers == []
