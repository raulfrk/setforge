"""Tests for the transitions module."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from my_setup.transitions import (
    TransitionCommand,
    TransitionMeta,
    compute_patch,
    make_meta,
    now_utc,
    snapshot_paths,
    state_root,
    transition_dirname,
    transitions_root,
    write_meta,
)


def test_state_root_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MY_SETUP_STATE_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/test")))
    assert state_root() == Path("/home/test/.local/state/my-setup")


def test_state_root_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MY_SETUP_STATE_DIR", str(tmp_path))
    assert state_root() == tmp_path
    assert transitions_root() == tmp_path / "transitions"


def test_transition_dirname_format() -> None:
    ts = datetime(2026, 5, 7, 12, 30, 45, tzinfo=timezone.utc)
    assert transition_dirname(ts, "install", "vm-headless") == (
        "20260507T123045Z-install-vm-headless"
    )


def test_transition_dirname_sort_matches_time() -> None:
    """Lexicographic sort across dirnames must match chronological sort."""
    earlier = datetime(2026, 5, 7, 9, 0, 0, tzinfo=timezone.utc)
    later = datetime(2026, 5, 7, 17, 0, 0, tzinfo=timezone.utc)
    a = transition_dirname(earlier, "install", "vm-headless")
    b = transition_dirname(later, "install", "vm-headless")
    assert sorted([b, a]) == [a, b]


def test_now_utc_is_aware() -> None:
    ts = now_utc()
    assert ts.tzinfo is timezone.utc


def test_transition_command_values() -> None:
    """Closed set must round-trip through json as the bare string value."""
    assert TransitionCommand.INSTALL.value == "install"
    assert TransitionCommand.SYNC.value == "sync"
    assert TransitionCommand.REVERT.value == "revert"


def test_transition_meta_to_dict_iso_timestamp() -> None:
    ts = datetime(2026, 5, 7, 12, 30, 45, tzinfo=timezone.utc)
    meta = TransitionMeta(
        command=TransitionCommand.INSTALL,
        profile="vm-headless",
        timestamp=ts,
        host="example",
        version="0.1.0",
    )
    payload = meta.to_dict()
    assert payload == {
        "command": "install",
        "profile": "vm-headless",
        "timestamp": "2026-05-07T12:30:45+00:00",
        "host": "example",
        "version": "0.1.0",
    }


def test_make_meta_uses_current_host_and_version() -> None:
    meta = make_meta(TransitionCommand.INSTALL, "vm-headless")
    assert meta.command is TransitionCommand.INSTALL
    assert meta.profile == "vm-headless"
    assert meta.host  # not empty
    assert meta.version  # not empty


def test_write_meta_creates_dir_and_file(tmp_path: Path) -> None:
    target = tmp_path / "20260507T120000Z-install-vmh"
    meta = TransitionMeta(
        command=TransitionCommand.INSTALL,
        profile="vmh",
        timestamp=datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc),
        host="h",
        version="0.1.0",
    )
    write_meta(target, meta)
    payload = json.loads((target / "meta.json").read_text())
    assert payload["command"] == "install"
    assert payload["profile"] == "vmh"


def test_snapshot_paths_records_existing_and_missing(tmp_path: Path) -> None:
    a = tmp_path / "a.txt"
    a.write_text("hello\n")
    b = tmp_path / "missing.txt"
    snap = snapshot_paths([a, b])
    assert snap == {a: "hello\n", b: None}


def test_compute_patch_empty_when_unchanged(tmp_path: Path) -> None:
    a = tmp_path / "a.txt"
    snap = {a: "x\n"}
    assert compute_patch(snap, snap) == ""


def test_compute_patch_modified_file(tmp_path: Path) -> None:
    a = tmp_path / "a.txt"
    pre = {a: "before\n"}
    post = {a: "after\n"}
    patch = compute_patch(pre, post)
    assert f"--- {a}" in patch
    assert f"+++ {a}" in patch
    assert "-before" in patch
    assert "+after" in patch


def test_compute_patch_new_file_uses_dev_null(tmp_path: Path) -> None:
    a = tmp_path / "new.txt"
    patch = compute_patch({a: None}, {a: "fresh\n"})
    assert "--- /dev/null" in patch
    assert f"+++ {a}" in patch
    assert "+fresh" in patch


def test_compute_patch_deleted_file_uses_dev_null(tmp_path: Path) -> None:
    a = tmp_path / "gone.txt"
    patch = compute_patch({a: "old\n"}, {a: None})
    assert f"--- {a}" in patch
    assert "+++ /dev/null" in patch
    assert "-old" in patch


def test_compute_patch_combines_multiple_files(tmp_path: Path) -> None:
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    pre = {a: "1\n", b: "2\n"}
    post = {a: "1\n", b: "X\n"}  # only b changed
    patch = compute_patch(pre, post)
    assert f"+++ {b}" in patch
    assert f"+++ {a}" not in patch  # unchanged file omitted
