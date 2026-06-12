"""Tests for the transition state-snapshot payload (store rollback support).

The ``state_snapshots/`` payload records the pre-install state of the
per-host stores (byte base, spans sidecar, scalar-base manifest) so
``setforge revert`` can roll them back in lockstep with the live files.
Covers:

- staging order: snapshots land inside the ``.pending-`` dir; ``meta.json``
  (the commit marker) is still written strictly last
- back-compat sentinel: a transition without the dir loads as ``None``
- absent (``payload=None``) vs empty (``payload=b""``) stay distinct
  through a write + load round-trip
- restore: absent entries DELETE the store file, present entries rewrite
  it byte-exact; idempotent on re-run
- corrupt manifest shapes raise :class:`InvalidTransitionRecord`
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from setforge.errors import InvalidTransitionRecord
from setforge.transitions import (
    SnapshotStore,
    StateSnapshotEntry,
    TransitionCommand,
    TransitionDir,
    TransitionMeta,
    load_latest,
    load_state_snapshots,
    restore_state_snapshots,
    snapshot_store_state,
    transitions_root,
    write_transition,
)

_PROFILE = "vmh"


@pytest.fixture(autouse=True)
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    return state


def _make_meta() -> TransitionMeta:
    return TransitionMeta(
        command=TransitionCommand.INSTALL,
        profile=_PROFILE,
        timestamp=datetime(2026, 6, 12, 12, 0, 0, tzinfo=UTC),
        host="h",
        version="0.1.0",
    )


def _entries() -> tuple[StateSnapshotEntry, ...]:
    """One present + one absent + one empty entry across all three stores."""
    return (
        StateSnapshotEntry(
            store=SnapshotStore.BASE,
            profile=_PROFILE,
            key="claude/CLAUDE.md",
            payload=b"base bytes v1\n",
        ),
        StateSnapshotEntry(
            store=SnapshotStore.SPANS,
            profile=_PROFILE,
            key="claude/CLAUDE.md",
            payload=None,
        ),
        StateSnapshotEntry(
            store=SnapshotStore.SCALAR_BASE,
            profile=_PROFILE,
            key="claude/CLAUDE.md",
            payload=b"",
        ),
    )


def _write(snapshots: tuple[StateSnapshotEntry, ...]) -> TransitionDir:
    return write_transition(_make_meta(), {}, {}, None, state_snapshots=snapshots)


# ---------------------------------------------------------------------------
# staging order — snapshots inside .pending-, meta.json strictly last
# ---------------------------------------------------------------------------


def test_snapshots_staged_before_meta_commit_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The snapshot dir + payloads must be on disk BEFORE meta.json lands.

    meta.json is the commit marker: anything written after it would be
    invisible to a crash that lands between the two writes. The wrapper
    asserts the manifest and its payload file already exist in the
    committed target dir at the moment write_meta is invoked.
    """
    import setforge.transitions as transitions_mod

    real_write_meta = transitions_mod.write_meta
    seen: dict[str, bool] = {}

    def checking_write_meta(
        transition_dir: TransitionDir,
        meta: TransitionMeta,
        paths: list[Path] | None = None,
    ) -> None:
        snap_dir = transition_dir / "state_snapshots"
        seen["manifest"] = (snap_dir / "manifest.json").exists()
        seen["payload"] = (snap_dir / "0.payload").exists()
        real_write_meta(transition_dir, meta, paths)

    monkeypatch.setattr(transitions_mod, "write_meta", checking_write_meta)

    out = _write(_entries())

    assert seen == {"manifest": True, "payload": True}
    assert (out / "meta.json").exists()


def test_crash_before_rename_stages_snapshots_in_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Snapshots are staged in the .pending- dir, so a crash before the
    rename leaves no committed transition but a fully staged pending dir."""

    def _raise_on_rename(src: str | Path, dst: str | Path) -> None:
        raise SystemExit("simulated crash before rename")

    monkeypatch.setattr("setforge.transitions.os.rename", _raise_on_rename)

    with pytest.raises(SystemExit):
        _write(_entries())

    assert load_latest(_PROFILE) is None
    pending = [
        d for d in transitions_root().iterdir() if d.name.startswith(".pending-")
    ]
    assert len(pending) == 1
    snap_dir = pending[0] / "state_snapshots"
    assert (snap_dir / "manifest.json").exists()
    assert (snap_dir / "0.payload").exists()


def test_empty_snapshot_tuple_writes_no_dir() -> None:
    """The default empty tuple stays byte-identical to the pre-bump shape."""
    out = _write(())
    assert not (out / "state_snapshots").exists()


# ---------------------------------------------------------------------------
# load — back-compat sentinel + absent-vs-empty round-trip
# ---------------------------------------------------------------------------


def test_load_returns_none_when_dir_missing() -> None:
    """A transition recorded before the snapshot bump loads as ``None`` —
    the sentinel that tells revert to skip store work entirely."""
    out = _write(())
    assert load_state_snapshots(out) is None


def test_absent_vs_empty_round_trip() -> None:
    """payload=None (absent) and payload=b"" (empty) must survive the
    write + load round-trip as DISTINCT states — no truthiness collapse."""
    out = _write(_entries())

    loaded = load_state_snapshots(out)
    assert loaded is not None
    by_store = {e.store: e for e in loaded}

    assert by_store[SnapshotStore.BASE].payload == b"base bytes v1\n"
    assert by_store[SnapshotStore.SPANS].payload is None
    empty = by_store[SnapshotStore.SCALAR_BASE].payload
    assert empty is not None
    assert empty == b""


def test_round_trip_preserves_entry_identity_and_order() -> None:
    out = _write(_entries())
    loaded = load_state_snapshots(out)
    assert loaded == _entries()


# ---------------------------------------------------------------------------
# restore — delete absent, rewrite present, idempotent
# ---------------------------------------------------------------------------


def _store_paths(state: Path) -> dict[SnapshotStore, Path]:
    return {
        SnapshotStore.BASE: state / "base" / _PROFILE / "claude" / "CLAUDE.md",
        SnapshotStore.SPANS: state / "spans" / _PROFILE / "claude" / "CLAUDE.md.json",
        SnapshotStore.SCALAR_BASE: (
            state / "scalar-base" / _PROFILE / "claude" / "CLAUDE.md.json"
        ),
    }


def test_restore_deletes_absent_and_rewrites_present(state_dir: Path) -> None:
    paths = _store_paths(state_dir)
    # Seed every store file with post-install junk the restore must replace.
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"post-install junk")

    restore_state_snapshots(_entries())

    assert paths[SnapshotStore.BASE].read_bytes() == b"base bytes v1\n"
    # Absent pre-install -> the file is DELETED, not truncated.
    assert not paths[SnapshotStore.SPANS].exists()
    # Empty pre-install -> the file is REWRITTEN to zero bytes, not deleted.
    assert paths[SnapshotStore.SCALAR_BASE].read_bytes() == b""


def test_restore_is_idempotent(state_dir: Path) -> None:
    """A re-run after an interrupted revert reproduces the same end state."""
    restore_state_snapshots(_entries())
    restore_state_snapshots(_entries())

    paths = _store_paths(state_dir)
    assert paths[SnapshotStore.BASE].read_bytes() == b"base bytes v1\n"
    assert not paths[SnapshotStore.SPANS].exists()
    assert paths[SnapshotStore.SCALAR_BASE].read_bytes() == b""


def test_snapshot_store_state_reads_current_bytes(state_dir: Path) -> None:
    """snapshot_store_state captures present bytes and absent-as-None."""
    base = _store_paths(state_dir)[SnapshotStore.BASE]
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_bytes(b"current\n")

    present = snapshot_store_state(SnapshotStore.BASE, _PROFILE, "claude/CLAUDE.md")
    assert present.payload == b"current\n"

    absent = snapshot_store_state(SnapshotStore.SPANS, _PROFILE, "claude/CLAUDE.md")
    assert absent.payload is None


# ---------------------------------------------------------------------------
# corrupt manifest shapes
# ---------------------------------------------------------------------------


def _corrupt_manifest(out: TransitionDir, body: str) -> None:
    (out / "state_snapshots" / "manifest.json").write_text(body, encoding="utf-8")


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        '["top-level list"]',
        '{"entries": "not a list"}',
        '{"entries": ["not a dict"]}',
        '{"entries": [{"store": "bogus", "profile": "p", "key": "k", '
        '"payload_file": null}]}',
        '{"entries": [{"store": "base", "profile": 7, "key": "k", '
        '"payload_file": null}]}',
        '{"entries": [{"store": "base", "profile": "p", "key": "k", '
        '"payload_file": "missing.payload"}]}',
        '{"entries": [{"store": "base", "profile": "p", "key": "k", '
        '"payload_file": "../escape.payload"}]}',
    ],
    ids=[
        "not-json",
        "top-level-list",
        "entries-not-list",
        "entry-not-dict",
        "unknown-store",
        "non-str-profile",
        "missing-payload-file",
        "traversal-payload-file",
    ],
)
def test_load_raises_on_corrupt_manifest(body: str) -> None:
    out = _write(_entries())
    _corrupt_manifest(out, body)
    with pytest.raises(InvalidTransitionRecord):
        load_state_snapshots(out)


def test_load_raises_when_manifest_missing_but_dir_exists() -> None:
    """A snapshot dir without its manifest is corruption, not back-compat."""
    out = _write(_entries())
    (out / "state_snapshots" / "manifest.json").unlink()
    with pytest.raises(InvalidTransitionRecord):
        load_state_snapshots(out)


def test_payload_files_are_fsynced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every staged snapshot payload fsyncs its own fd before the rename
    (same durability bar as the other staged payload files)."""
    import builtins
    import contextlib

    import setforge.transitions as transitions_mod

    events: list[str] = []
    open_fds: dict[int, str] = {}
    real_open = builtins.open
    real_fsync = os.fsync
    real_rename = os.rename

    def recording_open(file: object, *args: object, **kwargs: object) -> object:
        fh = real_open(file, *args, **kwargs)  # type: ignore[call-overload]
        with contextlib.suppress(OSError):
            open_fds[fh.fileno()] = Path(str(file)).name
        return fh

    def recording_fsync(fd: int) -> None:
        events.append(f"fsync:{open_fds.get(fd, '?')}")
        real_fsync(fd)

    def recording_rename(src: object, dst: object) -> None:
        events.append("rename")
        real_rename(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "open", recording_open)
    monkeypatch.setattr(transitions_mod.os, "fsync", recording_fsync)
    monkeypatch.setattr(transitions_mod.os, "rename", recording_rename)

    _write(_entries())

    rename_idx = events.index("rename")
    before = set(events[:rename_idx])
    assert "fsync:manifest.json" in before
    assert "fsync:0.payload" in before


def test_manifest_payload_file_shape() -> None:
    """The on-disk manifest references payloads by numbered file name and
    records absence as an explicit null (no truthiness-prone encodings)."""
    out = _write(_entries())
    raw = json.loads(
        (out / "state_snapshots" / "manifest.json").read_text(encoding="utf-8")
    )
    files = [e["payload_file"] for e in raw["entries"]]
    assert files == ["0.payload", None, "1.payload"]
