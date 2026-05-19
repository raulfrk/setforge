"""Unit tests for :mod:`setforge.snapshots` (setforge-of3a).

Covers atomic create (partial → final rename), mode preservation,
symlink fidelity, setuid masking, additive-overlay restore,
prune-on-create retention, and the various edge cases the spec calls
out (`--keep 0`, `--keep -1`, missing `_meta.json`, etc.).

The tests use ``tmp_path`` + a monkeypatched ``Path.home`` so the
snapshot root lands in a per-test directory; that lets the assertions
exercise the real filesystem operations (symlinks, mode bits) instead
of mocking them out.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from setforge import snapshots as snap_mod
from setforge.config import (
    Config,
    Profile,
    ResolvedProfile,
    TrackedFile,
)
from setforge.errors import SetforgeError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``Path.home()`` AND ``LOCAL_CONFIG_PATH`` at a fresh tmp directory.

    Snapshots resolve through ``Path.home()``. ``binaries.LOCAL_CONFIG_PATH``
    is captured at import time as a module-level ``Final`` constant, so a
    bare ``Path.home`` monkeypatch leaves it pointing at the real
    ``~/.config/setforge/local.yaml`` — re-bind it explicitly so the
    test surface stays sandboxed.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    fake_local = tmp_path / ".config" / "setforge" / "local.yaml"
    monkeypatch.setattr(snap_mod, "LOCAL_CONFIG_PATH", fake_local)
    return tmp_path


@dataclass(frozen=True, slots=True)
class _Ctx:
    """Test-local bundle mirroring the four args ``create_snapshot`` needs."""

    cfg: Config
    resolved: ResolvedProfile
    repo_root: Path
    profile: str


def _build_ctx(
    home: Path,
    *,
    profile: str = "test",
    tracked_file_name: str = "minimal_text",
    src_relative: str = "minimal/text.txt",
    dst_template: str | None = None,
) -> tuple[_Ctx, Path, Path]:
    """Build a test context whose single tracked_file lives under ``home``.

    Returns ``(ctx, src_path, dst_path)`` — tests write content to
    ``src_path`` (the tracked source) and assert against ``dst_path``
    (the live destination).
    """
    repo_root = home / "config-repo"
    src_path = repo_root / "tracked" / src_relative
    src_path.parent.mkdir(parents=True, exist_ok=True)
    if dst_template is None:
        dst_template = str(home / "live" / "text.txt")
    tracked = TrackedFile(src=src_relative, dst=dst_template, template=False)
    cfg = Config(
        version=1,
        schema_version="1.0",
        tracked_files={tracked_file_name: tracked},
        profiles={profile: Profile(tracked_files=[tracked_file_name])},
    )
    resolved = ResolvedProfile(tracked_files=[tracked_file_name])
    ctx = _Ctx(cfg=cfg, resolved=resolved, repo_root=repo_root, profile=profile)
    return ctx, src_path, Path(dst_template)


def _create(
    ctx: _Ctx,
    label: str,
    *,
    keep: int = snap_mod.DEFAULT_KEEP,
) -> snap_mod.SnapshotMeta:
    return snap_mod.create_snapshot(
        ctx.cfg, ctx.resolved, ctx.repo_root, ctx.profile, label, keep=keep
    )


def _pre_ctx(ctx: _Ctx) -> tuple[Config, ResolvedProfile, Path, str]:
    return (ctx.cfg, ctx.resolved, ctx.repo_root, ctx.profile)


# ---------------------------------------------------------------------------
# create_snapshot
# ---------------------------------------------------------------------------


def test_create_snapshot_writes_meta_last_for_atomicity(
    fake_home: Path,
) -> None:
    """The final dir contains ``_meta.json`` — written LAST as commit marker."""
    ctx, _, dst = _build_ctx(fake_home)
    dst.parent.mkdir(parents=True)
    dst.write_text("body\n")
    meta = _create(ctx, "label-x")
    final_dir = snap_mod.snapshots_root() / meta.snapshot_id
    assert final_dir.is_dir()
    assert (final_dir / "_meta.json").is_file()
    loaded = json.loads((final_dir / "_meta.json").read_text())
    assert loaded["label"] == "label-x"
    assert loaded["profile"] == "test"
    assert loaded["snapshot_id"] == meta.snapshot_id


def test_create_snapshot_partial_dir_removed_on_failure(
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If write_meta raises, the ``.partial`` dir is cleaned up."""
    ctx, _, dst = _build_ctx(fake_home)
    dst.parent.mkdir(parents=True)
    dst.write_text("body\n")

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(snap_mod, "_write_meta", boom)
    with pytest.raises(OSError, match="simulated fsync failure"):
        _create(ctx, "fail")
    # No partial dir left behind, no final dir created.
    root = snap_mod.snapshots_root()
    if root.exists():
        partials = [p for p in root.iterdir() if p.name.endswith(".partial")]
        assert partials == []


def test_create_snapshot_preserves_mode_for_executable_file(
    fake_home: Path,
) -> None:
    """An executable live file lands in the snapshot with executable bits."""
    ctx, _, dst = _build_ctx(fake_home)
    dst.parent.mkdir(parents=True)
    dst.write_text("#!/bin/sh\necho hi\n")
    dst.chmod(0o755)
    meta = _create(ctx, "exec-test")
    mirror = snap_mod.snapshots_root() / meta.snapshot_id / dst.relative_to("/")
    assert mirror.is_file()
    assert stat.S_IMODE(mirror.stat().st_mode) == 0o755


def test_create_snapshot_masks_setuid_setgid_bits(
    fake_home: Path,
) -> None:
    """A setuid live file lands in the snapshot WITHOUT the setuid bit set."""
    ctx, _, dst = _build_ctx(fake_home)
    dst.parent.mkdir(parents=True)
    dst.write_text("body\n")
    dst.chmod(0o6755)  # setuid + setgid + 0755
    meta = _create(ctx, "setuid-test")
    mirror = snap_mod.snapshots_root() / meta.snapshot_id / dst.relative_to("/")
    masked = stat.S_IMODE(mirror.stat().st_mode)
    assert masked & 0o4000 == 0, "setuid bit must be stripped"
    assert masked & 0o2000 == 0, "setgid bit must be stripped"
    assert masked & 0o0777 == 0o755, "low bits preserved"


def test_create_snapshot_preserves_symlinks_as_symlinks(
    fake_home: Path,
) -> None:
    """A symlinked live path is captured AS a symlink, not its target body."""
    ctx, _, dst = _build_ctx(fake_home)
    dst.parent.mkdir(parents=True)
    target = fake_home / "elsewhere" / "real.txt"
    target.parent.mkdir(parents=True)
    target.write_text("target body\n")
    os.symlink(target, dst)
    meta = _create(ctx, "symlink-test")
    mirror = snap_mod.snapshots_root() / meta.snapshot_id / dst.relative_to("/")
    assert mirror.is_symlink()
    assert os.readlink(mirror) == str(target)


def test_create_snapshot_skips_missing_live_files(fake_home: Path) -> None:
    """Snapshot fidelity is files-that-exist-now; missing dsts skip silently."""
    ctx, _, dst = _build_ctx(fake_home)
    # dst not created — simulates first-install profile with no live file.
    assert not dst.exists()
    meta = _create(ctx, "empty")
    assert meta.files == ()
    final_dir = snap_mod.snapshots_root() / meta.snapshot_id
    assert (final_dir / "_meta.json").is_file()


def test_create_snapshot_captures_local_yaml_when_present(
    fake_home: Path,
) -> None:
    """``~/.config/setforge/local.yaml`` is captured alongside tracked dsts."""
    ctx, _, dst = _build_ctx(fake_home)
    dst.parent.mkdir(parents=True)
    dst.write_text("body\n")
    local_yaml = fake_home / ".config" / "setforge" / "local.yaml"
    local_yaml.parent.mkdir(parents=True)
    local_yaml.write_text("binaries: {}\n")
    meta = _create(ctx, "with-local")
    assert local_yaml in meta.files
    mirror = snap_mod.snapshots_root() / meta.snapshot_id / local_yaml.relative_to("/")
    assert mirror.read_text() == "binaries: {}\n"


def test_create_snapshot_rejects_empty_label(fake_home: Path) -> None:
    ctx, _, _ = _build_ctx(fake_home)
    with pytest.raises(SetforgeError, match="non-empty"):
        _create(ctx, "")


def test_create_snapshot_rejects_negative_keep(fake_home: Path) -> None:
    ctx, _, _ = _build_ctx(fake_home)
    with pytest.raises(SetforgeError, match="non-negative"):
        _create(ctx, "label", keep=-1)


def test_create_snapshot_rejects_existing_id_collision(
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same-second create with the same label refuses rather than overwrites."""
    ctx, _, dst = _build_ctx(fake_home)
    dst.parent.mkdir(parents=True)
    dst.write_text("body\n")
    pinned = datetime(2026, 5, 18, 21, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(snap_mod, "now_utc", lambda: pinned)
    _create(ctx, "twin")
    with pytest.raises(SetforgeError, match="already exists"):
        _create(ctx, "twin")


# ---------------------------------------------------------------------------
# list_snapshots
# ---------------------------------------------------------------------------


def test_list_snapshots_empty_root_returns_empty(fake_home: Path) -> None:
    assert snap_mod.list_snapshots() == []


def test_list_snapshots_newest_first(
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Snapshots are returned newest-first regardless of creation order."""
    ctx, _, dst = _build_ctx(fake_home)
    dst.parent.mkdir(parents=True)
    dst.write_text("body\n")
    times = iter(
        [
            datetime(2026, 5, 17, 9, 30, 15, tzinfo=UTC),
            datetime(2026, 5, 18, 21, 0, 0, tzinfo=UTC),
            datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC),
        ]
    )
    monkeypatch.setattr(snap_mod, "now_utc", lambda: next(times))
    _create(ctx, "first")
    _create(ctx, "second")
    _create(ctx, "third")
    listed = snap_mod.list_snapshots()
    assert [s.label for s in listed] == ["third", "second", "first"]


def test_list_snapshots_ignores_partial_and_corrupt(
    fake_home: Path,
) -> None:
    """``.partial`` dirs and meta-less dirs do NOT surface in list."""
    root = snap_mod.snapshots_root()
    root.mkdir(parents=True)
    (root / "20260101T000000Z-broken").mkdir()  # no _meta.json
    (root / "20260102T000000Z-partial.partial").mkdir()  # partial suffix
    assert snap_mod.list_snapshots() == []


# ---------------------------------------------------------------------------
# resolve_snapshot
# ---------------------------------------------------------------------------


def test_resolve_snapshot_matches_by_label(fake_home: Path) -> None:
    ctx, _, dst = _build_ctx(fake_home)
    dst.parent.mkdir(parents=True)
    dst.write_text("body\n")
    _create(ctx, "alpha")
    resolved = snap_mod.resolve_snapshot("alpha")
    assert resolved.label == "alpha"


def test_resolve_snapshot_matches_by_id(fake_home: Path) -> None:
    ctx, _, dst = _build_ctx(fake_home)
    dst.parent.mkdir(parents=True)
    dst.write_text("body\n")
    meta = _create(ctx, "beta")
    resolved = snap_mod.resolve_snapshot(meta.snapshot_id)
    assert resolved.snapshot_id == meta.snapshot_id


def test_resolve_snapshot_missing_raises(fake_home: Path) -> None:
    with pytest.raises(SetforgeError, match="not found"):
        snap_mod.resolve_snapshot("does-not-exist")


# ---------------------------------------------------------------------------
# restore_snapshot
# ---------------------------------------------------------------------------


def test_restore_snapshot_overlays_only_files_in_snapshot(
    fake_home: Path,
) -> None:
    """Additive overlay: live-only files added after snapshot are untouched."""
    ctx, _, dst = _build_ctx(fake_home)
    dst.parent.mkdir(parents=True)
    dst.write_text("original\n")
    meta = _create(ctx, "snap1")
    # Drift live AND add a sibling that was NOT in the snapshot.
    dst.write_text("drifted\n")
    sibling = dst.parent / "live-only.txt"
    sibling.write_text("untouched live-only body\n")

    snap_mod.restore_snapshot(meta.snapshot_id, pre_snapshot=False)
    assert dst.read_text() == "original\n"
    assert sibling.read_text() == "untouched live-only body\n"


def test_restore_snapshot_refuses_when_meta_missing(
    fake_home: Path,
) -> None:
    """Hand-corrupted snapshot dir without ``_meta.json`` is unreadable."""
    root = snap_mod.snapshots_root()
    root.mkdir(parents=True)
    bad = root / "20260101T000000Z-broken"
    bad.mkdir()
    with pytest.raises(SetforgeError, match="not found"):
        # ``list_snapshots`` filters this out, so resolve fails first.
        snap_mod.resolve_snapshot("broken")


def test_restore_snapshot_with_pre_snapshot_captures_current_state(
    fake_home: Path,
) -> None:
    """``pre_snapshot=True`` captures live BEFORE applying the restore."""
    ctx, _, dst = _build_ctx(fake_home)
    dst.parent.mkdir(parents=True)
    dst.write_text("v1\n")
    _create(ctx, "v1")
    dst.write_text("v2\n")
    snap_mod.restore_snapshot("v1", pre_snapshot=True, pre_snapshot_ctx=_pre_ctx(ctx))
    # Live now == v1; the pre-restore snapshot captured v2.
    assert dst.read_text() == "v1\n"
    labels = [s.label for s in snap_mod.list_snapshots()]
    assert any(label.startswith("pre-restore-") for label in labels)


def test_restore_snapshot_pre_snapshot_requires_ctx(
    fake_home: Path,
) -> None:
    ctx, _, dst = _build_ctx(fake_home)
    dst.parent.mkdir(parents=True)
    dst.write_text("body\n")
    _create(ctx, "alpha")
    with pytest.raises(SetforgeError, match="requires a profile context"):
        snap_mod.restore_snapshot("alpha", pre_snapshot=True, pre_snapshot_ctx=None)


def test_restore_snapshot_unlinks_live_symlink_before_write(
    fake_home: Path,
) -> None:
    """If live is a symlink, restore unlinks it first (does NOT follow it)."""
    ctx, _, dst = _build_ctx(fake_home)
    dst.parent.mkdir(parents=True)
    dst.write_text("body in snap\n")
    _create(ctx, "regular")
    # Swap live to a symlink pointing somewhere else.
    other = fake_home / "other.txt"
    other.write_text("symlink target body\n")
    dst.unlink()
    os.symlink(other, dst)
    snap_mod.restore_snapshot("regular", pre_snapshot=False)
    # dst is now a regular file, not a symlink, and the original
    # symlink target was NOT overwritten.
    assert not dst.is_symlink()
    assert dst.read_text() == "body in snap\n"
    assert other.read_text() == "symlink target body\n"


# ---------------------------------------------------------------------------
# prune_snapshots
# ---------------------------------------------------------------------------


def test_prune_keeps_n_newest(
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, _, dst = _build_ctx(fake_home)
    dst.parent.mkdir(parents=True)
    dst.write_text("body\n")
    times = iter(datetime(2026, 5, d, 0, 0, 0, tzinfo=UTC) for d in range(1, 6))
    monkeypatch.setattr(snap_mod, "now_utc", lambda: next(times))
    for i in range(5):
        _create(ctx, f"s{i}", keep=100)  # disable auto-prune
    removed = snap_mod.prune_snapshots(2)
    assert removed == 3
    labels = [s.label for s in snap_mod.list_snapshots()]
    assert labels == ["s4", "s3"]


def test_prune_keep_zero_removes_all(
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--keep 0`` means "no retention"; everything is deleted."""
    ctx, _, dst = _build_ctx(fake_home)
    dst.parent.mkdir(parents=True)
    dst.write_text("body\n")
    times = iter(datetime(2026, 5, d, 0, 0, 0, tzinfo=UTC) for d in range(1, 4))
    monkeypatch.setattr(snap_mod, "now_utc", lambda: next(times))
    for i in range(3):
        _create(ctx, f"s{i}", keep=100)
    removed = snap_mod.prune_snapshots(0)
    assert removed == 3
    assert snap_mod.list_snapshots() == []


def test_prune_keep_negative_raises(fake_home: Path) -> None:
    with pytest.raises(SetforgeError, match="non-negative"):
        snap_mod.prune_snapshots(-1)


def test_create_then_prune_fires_after_successful_create(
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto-prune runs AFTER write_meta — failed create keeps prior snapshot."""
    ctx, _, dst = _build_ctx(fake_home)
    dst.parent.mkdir(parents=True)
    dst.write_text("body\n")
    pinned = iter(
        [
            datetime(2026, 5, 17, 0, 0, 0, tzinfo=UTC),
            datetime(2026, 5, 18, 0, 0, 0, tzinfo=UTC),
        ]
    )
    monkeypatch.setattr(snap_mod, "now_utc", lambda: next(pinned))
    _create(ctx, "good")
    # Force the SECOND create to crash before commit; confirm "good"
    # survives (no premature prune-then-create).
    real_write_meta = snap_mod._write_meta

    def boom(*_a: Any, **_kw: Any) -> None:
        raise OSError("disk-full-simulation")

    monkeypatch.setattr(snap_mod, "_write_meta", boom)
    with pytest.raises(OSError, match="disk-full-simulation"):
        _create(ctx, "bad", keep=1)
    monkeypatch.setattr(snap_mod, "_write_meta", real_write_meta)
    labels = [s.label for s in snap_mod.list_snapshots()]
    assert labels == ["good"], "prior good snapshot must survive a failed create"


def test_auto_prune_on_create_keeps_keep_value(
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """11th create with ``keep=10`` removes the oldest; 10 remain."""
    ctx, _, dst = _build_ctx(fake_home)
    dst.parent.mkdir(parents=True)
    dst.write_text("body\n")
    times = iter(datetime(2026, 1, d, 0, 0, 0, tzinfo=UTC) for d in range(1, 13))
    monkeypatch.setattr(snap_mod, "now_utc", lambda: next(times))
    for i in range(11):
        _create(ctx, f"s{i:02d}", keep=10)
    labels = [s.label for s in snap_mod.list_snapshots()]
    assert len(labels) == 10
    # Newest 10 retained; s00 (the oldest) pruned.
    assert "s00" not in labels
    assert "s10" in labels


# ---------------------------------------------------------------------------
# format helpers
# ---------------------------------------------------------------------------


def test_format_age_buckets() -> None:
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    then_30s = datetime(2026, 1, 1, 11, 59, 30, tzinfo=UTC)
    then_30m = datetime(2026, 1, 1, 11, 30, 0, tzinfo=UTC)
    then_1h = datetime(2026, 1, 1, 11, 0, 0, tzinfo=UTC)
    then_1d = datetime(2025, 12, 31, 12, 0, 0, tzinfo=UTC)
    assert snap_mod.format_age(base, base) == "0s ago"
    assert snap_mod.format_age(base, then_30s) == "30s ago"
    assert snap_mod.format_age(base, then_30m) == "30m ago"
    assert snap_mod.format_age(base, then_1h) == "1h ago"
    assert snap_mod.format_age(base, then_1d) == "1d ago"


def test_format_size_units() -> None:
    assert snap_mod.format_size(512) == "512B"
    assert snap_mod.format_size(2048) == "2.00K"
    assert snap_mod.format_size(5 * 1024 * 1024) == "5.00M"
    assert snap_mod.format_size(200 * 1024 * 1024) == "200M"


def test_directory_size_bytes_walks_with_followlinks_false(
    fake_home: Path,
) -> None:
    """Symlinks inside the snapshot tree are NOT followed when summing size."""
    ctx, _, dst = _build_ctx(fake_home)
    dst.parent.mkdir(parents=True)
    huge_target = fake_home / "huge.bin"
    huge_target.write_bytes(b"x" * 100_000)
    dst.symlink_to(huge_target)
    meta = _create(ctx, "linked")
    size = snap_mod.directory_size_bytes(meta.snapshot_id)
    # The symlink itself is small; if followlinks=True we'd see 100k.
    assert size < 10_000
