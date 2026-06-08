"""Tests for the per-host stored-base bytes store."""

import os
from pathlib import Path

import pytest

from setforge import base_store, base_store_format
from setforge.errors import BaseStoreError, BaseStoreSchemaError


@pytest.fixture(autouse=True)
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    return tmp_path


def test_read_base_missing_returns_none() -> None:
    assert base_store.read_base("vm", "claude/CLAUDE.md") is None


def test_read_base_empty_file_returns_empty_bytes() -> None:
    base_store.write_base("vm", "claude/empty", b"")
    result = base_store.read_base("vm", "claude/empty")
    assert result == b""
    assert result is not None


def test_round_trip_exact_bytes(state_dir: Path) -> None:
    payload = b"line one\r\nline two\n"
    base_store.write_base("vm", "claude/CLAUDE.md", payload)
    assert base_store.read_base("vm", "claude/CLAUDE.md") == payload


def test_keyed_by_plain_profile_under_base_root(state_dir: Path) -> None:
    base_store.write_base("debian-vm", "claude/CLAUDE.md", b"x")
    expected = state_dir / "base" / "debian-vm" / "claude" / "CLAUDE.md"
    assert expected.read_bytes() == b"x"
    assert base_store.base_root() == state_dir / "base"


def test_write_base_rejects_traversal() -> None:
    with pytest.raises(BaseStoreError):
        base_store.write_base("vm", "../escape", b"x")


def test_write_base_rejects_absolute() -> None:
    with pytest.raises(BaseStoreError):
        base_store.write_base("vm", "/etc/passwd", b"x")


def test_read_base_rejects_traversal() -> None:
    with pytest.raises(BaseStoreError):
        base_store.read_base("vm", "../escape")


def test_concurrent_forked_writers_no_torn_bytes(state_dir: Path) -> None:
    if not hasattr(os, "fork"):
        pytest.skip("os.fork unavailable on this platform")

    payload_a = b"A" * (256 * 1024)
    payload_b = b"B" * (256 * 1024)

    pids: list[int] = []
    for payload in (payload_a, payload_b):
        pid = os.fork()
        if pid == 0:
            try:
                base_store.write_base("vm", "claude/CLAUDE.md", payload)
            finally:
                os._exit(0)
        pids.append(pid)

    for pid in pids:
        os.waitpid(pid, 0)

    result = base_store.read_base("vm", "claude/CLAUDE.md")
    assert result in (payload_a, payload_b)


def test_prune_keeps_only_live_ids(state_dir: Path) -> None:
    base_store.write_base("vm", "a", b"1")
    base_store.write_base("vm", "b", b"2")
    base_store.write_base("vm", "nested/c", b"3")
    # A different profile's base must survive an unrelated prune.
    base_store.write_base("other", "a", b"keep")

    base_store.prune("vm", {"a", "nested/c"})

    assert base_store.read_base("vm", "a") == b"1"
    assert base_store.read_base("vm", "nested/c") == b"3"
    assert base_store.read_base("vm", "b") is None
    assert base_store.read_base("other", "a") == b"keep"


def test_prune_missing_profile_is_noop() -> None:
    base_store.prune("never-written", {"a"})


# --- format-version sidecar wiring ---------------------------------------


def _sidecar(state_dir: Path, profile: str) -> Path:
    return state_dir / "base" / profile / base_store_format.SIDECAR_NAME


def test_write_base_stamps_format_version(state_dir: Path) -> None:
    # First-run and legacy-data both stamp v1 lazily on write.
    base_store.write_base("vm", "claude/CLAUDE.md", b"x")
    sidecar = _sidecar(state_dir, "vm")
    assert (
        sidecar.read_text(encoding="utf-8").strip()
        == base_store_format.BASE_STORE_FORMAT_VERSION
    )


def test_read_base_refuses_future_format_before_read(state_dir: Path) -> None:
    base_store.write_base("vm", "claude/CLAUDE.md", b"payload")
    _sidecar(state_dir, "vm").write_text("2.0\n", encoding="utf-8")
    with pytest.raises(BaseStoreSchemaError):
        base_store.read_base("vm", "claude/CLAUDE.md")


def test_read_base_grandfathers_legacy_root(state_dir: Path) -> None:
    # A pre-versioning root: payload present, no sidecar -> read succeeds.
    target = state_dir / "base" / "vm" / "claude" / "CLAUDE.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"legacy")
    assert not _sidecar(state_dir, "vm").exists()
    assert base_store.read_base("vm", "claude/CLAUDE.md") == b"legacy"


def test_prune_preserves_format_version_sidecar(state_dir: Path) -> None:
    base_store.write_base("vm", "a", b"1")
    base_store.write_base("vm", "b", b"2")
    # Prune away every live id; the sidecar is metadata, not a base.
    base_store.prune("vm", set())
    assert base_store.read_base("vm", "a") is None
    assert base_store.read_base("vm", "b") is None
    assert _sidecar(state_dir, "vm").exists()


def test_grandfather_then_stamp_on_next_write(state_dir: Path) -> None:
    target = state_dir / "base" / "vm" / "claude" / "CLAUDE.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"legacy")
    # Read grandfathers (no sidecar yet); the next write stamps it.
    assert base_store.read_base("vm", "claude/CLAUDE.md") == b"legacy"
    base_store.write_base("vm", "claude/CLAUDE.md", b"updated")
    assert (
        _sidecar(state_dir, "vm").read_text(encoding="utf-8").strip()
        == base_store_format.BASE_STORE_FORMAT_VERSION
    )
