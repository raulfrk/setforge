"""Tests for the transitions module."""

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from my_setup.errors import RevertFailed
from my_setup.transitions import (
    ExtensionDelta,
    TransitionCommand,
    TransitionMeta,
    apply_patch_reverse,
    compute_patch,
    load_latest,
    make_meta,
    now_utc,
    snapshot_paths,
    state_root,
    transition_dirname,
    transitions_root,
    write_meta,
    write_transition,
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


def _root_relative(p: Path) -> str:
    """Mirror transitions._diff_path: leading ``/`` stripped for assertions."""
    s = str(p)
    return s.lstrip("/") if s.startswith("/") else s


def test_compute_patch_modified_file(tmp_path: Path) -> None:
    a = tmp_path / "a.txt"
    pre = {a: "before\n"}
    post = {a: "after\n"}
    patch = compute_patch(pre, post)
    assert f"--- {_root_relative(a)}" in patch
    assert f"+++ {_root_relative(a)}" in patch
    assert "-before" in patch
    assert "+after" in patch


def test_compute_patch_new_file_uses_dev_null(tmp_path: Path) -> None:
    a = tmp_path / "new.txt"
    patch = compute_patch({a: None}, {a: "fresh\n"})
    assert "--- /dev/null" in patch
    assert f"+++ {_root_relative(a)}" in patch
    assert "+fresh" in patch


def test_compute_patch_deleted_file_uses_dev_null(tmp_path: Path) -> None:
    a = tmp_path / "gone.txt"
    patch = compute_patch({a: "old\n"}, {a: None})
    assert f"--- {_root_relative(a)}" in patch
    assert "+++ /dev/null" in patch
    assert "-old" in patch


def test_compute_patch_combines_multiple_files(tmp_path: Path) -> None:
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    pre = {a: "1\n", b: "2\n"}
    post = {a: "1\n", b: "X\n"}  # only b changed
    patch = compute_patch(pre, post)
    assert f"+++ {_root_relative(b)}" in patch
    assert f"+++ {_root_relative(a)}" not in patch  # unchanged file omitted


def test_compute_patch_paths_are_root_relative_for_patch_safety(
    tmp_path: Path,
) -> None:
    """GNU patch rejects absolute paths as dangerous; we strip the
    leading slash and pair with `patch -d /` on apply."""
    a = tmp_path / "a.txt"
    patch = compute_patch({a: "x\n"}, {a: "y\n"})
    assert f"--- {str(a)[0]}" not in patch.split("\n")[0] or not patch.startswith(
        "--- /"
    )
    assert "--- /tmp" not in patch  # no leading slash on real paths


def _make_meta(command: TransitionCommand = TransitionCommand.INSTALL) -> TransitionMeta:
    return TransitionMeta(
        command=command,
        profile="vmh",
        timestamp=datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc),
        host="h",
        version="0.1.0",
    )


def test_extension_delta_is_empty() -> None:
    assert ExtensionDelta(added=[], removed=[]).is_empty() is True
    assert ExtensionDelta(added=["a.x"], removed=[]).is_empty() is False
    assert ExtensionDelta(added=[], removed=["b.y"]).is_empty() is False


def test_write_transition_full_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MY_SETUP_STATE_DIR", str(tmp_path))
    target_file = tmp_path / "live.txt"
    pre = {target_file: "before\n"}
    post = {target_file: "after\n"}
    delta = ExtensionDelta(added=["a.x"], removed=["b.y"])

    out = write_transition(_make_meta(), pre, post, delta)

    assert out.exists()
    assert (out / "meta.json").exists()
    assert (out / "changes.patch").exists()
    assert (out / "extensions.json").exists()
    assert "before" in (out / "changes.patch").read_text()
    payload = json.loads((out / "extensions.json").read_text())
    assert payload == {"added": ["a.x"], "removed": ["b.y"]}


def test_write_transition_omits_empty_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MY_SETUP_STATE_DIR", str(tmp_path))
    same = {tmp_path / "x": "same\n"}
    out = write_transition(
        _make_meta(), same, same, ExtensionDelta(added=["a.x"], removed=[])
    )
    assert (out / "meta.json").exists()
    assert not (out / "changes.patch").exists()
    assert (out / "extensions.json").exists()


def test_write_transition_omits_empty_extension_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MY_SETUP_STATE_DIR", str(tmp_path))
    target_file = tmp_path / "live.txt"
    out = write_transition(
        _make_meta(),
        {target_file: "a\n"},
        {target_file: "b\n"},
        ExtensionDelta(added=[], removed=[]),
    )
    assert (out / "changes.patch").exists()
    assert not (out / "extensions.json").exists()


def test_write_transition_omits_extension_delta_when_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MY_SETUP_STATE_DIR", str(tmp_path))
    out = write_transition(
        _make_meta(),
        {tmp_path / "x": "a\n"},
        {tmp_path / "x": "b\n"},
        None,
    )
    assert not (out / "extensions.json").exists()


def test_load_latest_returns_none_when_root_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MY_SETUP_STATE_DIR", str(tmp_path / "ghost"))
    assert load_latest("vmh") is None


def test_load_latest_returns_none_when_no_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MY_SETUP_STATE_DIR", str(tmp_path))
    (tmp_path / "transitions").mkdir()
    (tmp_path / "transitions" / "20260507T120000Z-install-other").mkdir()
    assert load_latest("vmh") is None


def test_load_latest_picks_most_recent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MY_SETUP_STATE_DIR", str(tmp_path))
    root = tmp_path / "transitions"
    root.mkdir()
    older = root / "20260507T090000Z-install-vmh"
    newer = root / "20260507T170000Z-install-vmh"
    older.mkdir()
    newer.mkdir()
    assert load_latest("vmh") == newer


def test_apply_patch_reverse_no_patch_is_noop(tmp_path: Path) -> None:
    apply_patch_reverse(tmp_path)  # no changes.patch → silent no-op


@pytest.mark.skipif(
    shutil.which("patch") is None, reason="GNU patch not on PATH"
)
def test_apply_patch_reverse_round_trips(tmp_path: Path) -> None:
    """Forward content edit, then apply_patch_reverse restores original."""
    target = tmp_path / "live.txt"
    target.write_text("after\n", encoding="utf-8")
    transition = tmp_path / "transition"
    transition.mkdir()
    (transition / "changes.patch").write_text(
        compute_patch({target: "before\n"}, {target: "after\n"}),
        encoding="utf-8",
    )

    apply_patch_reverse(transition)

    assert target.read_text() == "before\n"


@pytest.mark.skipif(
    shutil.which("patch") is None, reason="GNU patch not on PATH"
)
def test_apply_patch_reverse_raises_on_drift(tmp_path: Path) -> None:
    target = tmp_path / "live.txt"
    target.write_text("drifted-content\n", encoding="utf-8")
    transition = tmp_path / "transition"
    transition.mkdir()
    (transition / "changes.patch").write_text(
        compute_patch({target: "before\n"}, {target: "after\n"}),
        encoding="utf-8",
    )

    with pytest.raises(RevertFailed):
        apply_patch_reverse(transition)
