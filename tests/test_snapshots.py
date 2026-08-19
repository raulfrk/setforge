"""Unit tests for :mod:`setforge.snapshots`.

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
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

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
    tracked = TrackedFile.model_validate(
        {"src": src_relative, "dst": dst_template, "template": False}
    )
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


def _pre_ctx(ctx: _Ctx) -> snap_mod.PreSnapshotCtx:
    return snap_mod.PreSnapshotCtx(
        cfg=ctx.cfg,
        resolved=ctx.resolved,
        repo_root=ctx.repo_root,
        profile=ctx.profile,
    )


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


def test_create_snapshot_fsyncs_commit_marker_directory_before_publication(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _, dst = _build_ctx(fake_home)
    dst.parent.mkdir(parents=True)
    dst.write_text("body\n")
    synced: list[Path] = []
    real_fsync_dir = snap_mod.atomicio.fsync_dir

    def recording_fsync_dir(path: Path) -> None:
        synced.append(path)
        real_fsync_dir(path)

    monkeypatch.setattr(snap_mod.atomicio, "fsync_dir", recording_fsync_dir)

    meta = _create(ctx, "durable-marker")
    partial = snap_mod.snapshots_root() / f"{meta.snapshot_id}.partial"
    root = snap_mod.snapshots_root()

    assert partial in synced
    assert root in synced
    assert synced.index(partial) < synced.index(root)


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
    dst.symlink_to(target)
    meta = _create(ctx, "symlink-test")
    mirror = snap_mod.snapshots_root() / meta.snapshot_id / dst.relative_to("/")
    assert mirror.is_symlink()
    assert str(mirror.readlink()) == str(target)


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


def test_create_snapshot_writes_only_the_frozen_capture_plan(
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, _, first = _build_ctx(fake_home)
    second_name = "second_text"
    second = fake_home / "live" / "second.txt"
    ctx.cfg.tracked_files[second_name] = TrackedFile.model_validate(
        {
            "src": "minimal/second.txt",
            "dst": str(second),
            "template": False,
        }
    )
    ctx.resolved.tracked_files.append(second_name)
    first.parent.mkdir(parents=True)
    first.write_text("first frozen\n")
    second.write_text("second frozen\n")
    real_write = snap_mod._write_frozen_file
    writes = 0

    def mutate_second_after_first_write(
        source: snap_mod._FrozenSnapshotFile, destination: Path
    ) -> None:
        nonlocal writes
        real_write(source, destination)
        writes += 1
        if writes == 1:
            second.write_text("second changed after planning\n")

    monkeypatch.setattr(
        snap_mod,
        "_write_frozen_file",
        mutate_second_after_first_write,
    )

    meta = _create(ctx, "frozen-create")

    second_mirror = (
        snap_mod.snapshots_root() / meta.snapshot_id / second.relative_to("/")
    )
    assert second_mirror.read_text() == "second frozen\n"


def test_create_snapshot_rejects_empty_label(fake_home: Path) -> None:
    ctx, _, _ = _build_ctx(fake_home)
    with pytest.raises(SetforgeError, match="non-empty"):
        _create(ctx, "")


@pytest.mark.parametrize(
    "label",
    ["../escape", "nested/name", ".", "..", "hidden.partial", "line\nbreak"],
)
def test_create_snapshot_rejects_unsafe_label(fake_home: Path, label: str) -> None:
    ctx, _, _ = _build_ctx(fake_home)
    with pytest.raises(SetforgeError, match="safe non-empty name"):
        _create(ctx, label)


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


def test_list_snapshots_ignores_partial_dirs(
    fake_home: Path,
) -> None:
    """``.partial`` dirs do NOT surface in list."""
    root = snap_mod.snapshots_root()
    root.mkdir(parents=True)
    (root / "20260102T000000Z-partial.partial").mkdir()
    assert snap_mod.list_snapshots() == []


def test_list_snapshots_ignores_meta_missing_dirs(
    fake_home: Path,
) -> None:
    """Dirs without ``_meta.json`` (incomplete / corrupt) are skipped."""
    root = snap_mod.snapshots_root()
    root.mkdir(parents=True)
    (root / "20260101T000000Z-broken").mkdir()
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


def test_resolve_snapshot_scopes_same_label_to_requested_profile(
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_ctx, _, first_dst = _build_ctx(fake_home, profile="first")
    second_ctx, _, second_dst = _build_ctx(
        fake_home,
        profile="second",
        tracked_file_name="second_text",
        dst_template=str(fake_home / "live" / "second.txt"),
    )
    first_dst.parent.mkdir(parents=True)
    first_dst.write_text("first\n")
    second_dst.write_text("second\n")
    times = iter(
        (
            datetime(2026, 5, 18, 21, 0, 0, tzinfo=UTC),
            datetime(2026, 5, 18, 21, 0, 1, tzinfo=UTC),
        )
    )
    monkeypatch.setattr(snap_mod, "now_utc", lambda: next(times))
    first = _create(first_ctx, "shared")
    _create(second_ctx, "shared")

    resolved = snap_mod.resolve_snapshot("shared", profile="first")

    assert resolved.snapshot_id == first.snapshot_id


def test_load_meta_rejects_noncanonical_destination(fake_home: Path) -> None:
    snapshot_id = "20260518T210000Z-unsafe"
    snapshot_dir = snap_mod.snapshots_root() / snapshot_id
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / "_meta.json").write_text(
        json.dumps(
            {
                "snapshot_id": snapshot_id,
                "label": "unsafe",
                "created_at": "2026-05-18T21:00:00+00:00",
                "profile": "test",
                "files": ["/tmp/../outside"],
            }
        )
    )

    with pytest.raises(SetforgeError, match="canonical absolute"):
        snap_mod._load_meta(snapshot_dir)


def test_load_meta_rejects_symlinked_snapshot_directory(fake_home: Path) -> None:
    root = snap_mod.snapshots_root()
    root.mkdir(parents=True)
    outside = fake_home / "outside-snapshot"
    outside.mkdir()
    linked = root / "20260518T210000Z-linked"
    linked.symlink_to(outside, target_is_directory=True)

    with pytest.raises(SetforgeError, match="unsafe snapshot directory"):
        snap_mod._load_meta(linked)


def test_load_meta_rejects_metadata_id_mismatch(fake_home: Path) -> None:
    snapshot_dir = snap_mod.snapshots_root() / "20260518T210000Z-directory"
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / "_meta.json").write_text(
        json.dumps(
            {
                "snapshot_id": "20260518T210000Z-other",
                "label": "other",
                "created_at": "2026-05-18T21:00:00+00:00",
                "profile": "test",
                "files": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SetforgeError, match="does not match its directory"):
        snap_mod._load_meta(snapshot_dir)


@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_load_meta_rejects_unsafe_metadata_file(fake_home: Path, kind: str) -> None:
    snapshot_dir = snap_mod.snapshots_root() / f"20260518T210000Z-{kind}"
    snapshot_dir.mkdir(parents=True)
    meta_path = snapshot_dir / "_meta.json"
    if kind == "directory":
        meta_path.mkdir()
    else:
        target = fake_home / "outside-meta.json"
        target.write_text("{}", encoding="utf-8")
        meta_path.symlink_to(target)

    with pytest.raises(SetforgeError, match=r"corrupt _meta\.json"):
        snap_mod._load_meta(snapshot_dir)


def test_load_meta_normalizes_invalid_utf8(fake_home: Path) -> None:
    snapshot_dir = snap_mod.snapshots_root() / "20260518T210000Z-invalid-utf8"
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / "_meta.json").write_bytes(b"\xff")

    with pytest.raises(SetforgeError, match=r"corrupt _meta\.json"):
        snap_mod._load_meta(snapshot_dir)


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("snapshot_id", "", "non-empty text"),
        ("created_at", "not-a-timestamp", "invalid 'created_at'"),
        ("created_at", "2026-05-18T21:00:00", "include a timezone"),
        ("files", "not-a-list", "must be a list"),
        ("files", [42], "canonical absolute text"),
    ],
)
def test_snapshot_meta_rejects_invalid_field_shapes(
    field: str, value: object, match: str
) -> None:
    raw: dict[str, object] = {
        "snapshot_id": "20260518T210000Z-property",
        "label": "property",
        "created_at": "2026-05-18T21:00:00+00:00",
        "profile": "test",
        "files": ["/managed/file"],
    }
    raw[field] = value

    with pytest.raises(SetforgeError, match=match):
        snap_mod.SnapshotMeta.from_dict(raw)


def test_snapshot_meta_rejects_unknown_fields() -> None:
    raw: dict[str, object] = {
        "snapshot_id": "20260518T210000Z-property",
        "label": "property",
        "created_at": "2026-05-18T21:00:00+00:00",
        "profile": "test",
        "files": ["/managed/file"],
        "unexpected": True,
    }

    with pytest.raises(SetforgeError, match="expected exactly"):
        snap_mod.SnapshotMeta.from_dict(raw)


@pytest.mark.parametrize(
    ("profile", "created_at", "match"),
    [
        ("", datetime(2026, 5, 18, 21, 0, tzinfo=UTC), "profile"),
        ("test", datetime(2026, 5, 18, 21, 0), "timezone"),
    ],
)
def test_snapshot_meta_direct_construction_enforces_persisted_schema(
    profile: str, created_at: datetime, match: str
) -> None:
    with pytest.raises(SetforgeError, match=match):
        snap_mod.SnapshotMeta(
            snapshot_id="20260518T210000Z-direct",
            label="direct",
            created_at=created_at,
            profile=profile,
            files=(Path("/managed/file"),),
        )


def test_snapshot_meta_rejects_tzinfo_without_utc_offset() -> None:
    class NoOffset(tzinfo):
        def utcoffset(self, _dt: datetime | None):
            return None

        def dst(self, _dt: datetime | None):
            return None

        def tzname(self, _dt: datetime | None):
            return "no-offset"

    with pytest.raises(SetforgeError, match="timezone"):
        snap_mod.SnapshotMeta(
            snapshot_id="20260518T210000Z-direct",
            label="direct",
            created_at=datetime(2026, 5, 18, 21, 0, tzinfo=NoOffset()),
            profile="test",
            files=(Path("/managed/file"),),
        )


def test_snapshot_meta_rejects_nul_destination() -> None:
    raw: dict[str, object] = {
        "snapshot_id": "20260518T210000Z-property",
        "label": "property",
        "created_at": "2026-05-18T21:00:00+00:00",
        "profile": "test",
        "files": ["/managed/bad\x00path"],
    }

    with pytest.raises(SetforgeError, match="canonical absolute"):
        snap_mod.SnapshotMeta.from_dict(raw)


@given(
    st.lists(
        st.text(
            alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-",
            min_size=1,
            max_size=16,
        ),
        unique=True,
        max_size=8,
    )
)
def test_snapshot_meta_round_trips_canonical_paths(parts: list[str]) -> None:
    meta = snap_mod.SnapshotMeta(
        snapshot_id="20260518T210000Z-property",
        label="property",
        created_at=datetime(2026, 5, 18, 21, 0, 0, tzinfo=UTC),
        profile="test",
        files=tuple(Path("/managed") / part for part in parts),
    )

    assert snap_mod.SnapshotMeta.from_dict(meta.to_dict()) == meta


@given(
    st.text(
        alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-",
        min_size=1,
        max_size=16,
    )
)
def test_snapshot_meta_rejects_duplicate_destination(component: str) -> None:
    path = f"/managed/{component}"
    raw: dict[str, object] = {
        "snapshot_id": "20260518T210000Z-property",
        "label": "property",
        "created_at": "2026-05-18T21:00:00+00:00",
        "profile": "test",
        "files": [path, path],
    }

    with pytest.raises(SetforgeError, match="duplicate file path"):
        snap_mod.SnapshotMeta.from_dict(raw)


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


def test_restore_plan_does_not_reread_changed_mirror(fake_home: Path) -> None:
    ctx, _, dst = _build_ctx(fake_home)
    dst.parent.mkdir(parents=True)
    dst.write_text("planned body\n")
    meta = _create(ctx, "freeze-restore")
    plan = snap_mod._plan_restore_snapshot(
        meta.snapshot_id,
        cfg=ctx.cfg,
        resolved=ctx.resolved,
        repo_root=ctx.repo_root,
        profile=ctx.profile,
    )
    mirror = snap_mod.snapshots_root() / meta.snapshot_id / dst.relative_to("/")
    mirror.write_text("changed mirror body\n")
    dst.write_text("live drift\n")

    snap_mod._apply_restore_plan(plan)

    assert dst.read_text() == "planned body\n"


def test_restore_plan_refuses_destination_parent_retarget_before_write(
    fake_home: Path,
) -> None:
    ctx, _, dst = _build_ctx(fake_home)
    dst.parent.mkdir(parents=True)
    dst.write_text("snapshot body\n")
    meta = _create(ctx, "parent-retarget")
    dst.write_text("pre-restore body\n")
    plan = snap_mod._plan_restore_snapshot(
        meta.snapshot_id,
        cfg=ctx.cfg,
        resolved=ctx.resolved,
        repo_root=ctx.repo_root,
        profile=ctx.profile,
    )
    original_parent = fake_home / "original-live"
    dst.parent.rename(original_parent)
    external_parent = fake_home / "external-live"
    external_parent.mkdir()
    dst.parent.symlink_to(external_parent, target_is_directory=True)

    with pytest.raises(SetforgeError, match="symlinked destination parent"):
        snap_mod._apply_restore_plan(plan)

    assert not (external_parent / dst.name).exists()
    assert (original_parent / dst.name).read_text() == "pre-restore body\n"


def test_restore_plan_refuses_replaced_destination_directory_before_write(
    fake_home: Path,
) -> None:
    ctx, _, dst = _build_ctx(fake_home)
    dst.parent.mkdir(parents=True)
    dst.write_text("snapshot body\n")
    meta = _create(ctx, "parent-replaced")
    dst.write_text("pre-restore body\n")
    plan = snap_mod._plan_restore_snapshot(
        meta.snapshot_id,
        cfg=ctx.cfg,
        resolved=ctx.resolved,
        repo_root=ctx.repo_root,
        profile=ctx.profile,
    )
    original_parent = fake_home / "original-live"
    dst.parent.rename(original_parent)
    dst.parent.mkdir()
    (dst.parent / dst.name).write_text("unrelated replacement\n")

    with pytest.raises(SetforgeError, match="topology changed"):
        snap_mod._apply_restore_plan(plan)

    assert (dst.parent / dst.name).read_text() == "unrelated replacement\n"
    assert (original_parent / dst.name).read_text() == "pre-restore body\n"


@pytest.mark.parametrize("as_symlink", [False, True])
def test_restore_write_refuses_parent_swap_after_preflight(
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    as_symlink: bool,
) -> None:
    ctx, _, dst = _build_ctx(fake_home)
    dst.parent.mkdir(parents=True)
    if as_symlink:
        (dst.parent / "target").write_text("snapshot target\n")
        dst.symlink_to("target")
    else:
        dst.write_text("snapshot body\n")
    meta = _create(ctx, "mid-write-parent-swap")
    plan = snap_mod._plan_restore_snapshot(
        meta.snapshot_id,
        cfg=ctx.cfg,
        resolved=ctx.resolved,
        repo_root=ctx.repo_root,
        profile=ctx.profile,
    )
    real_write = snap_mod._write_restored_file
    swapped = False

    def swap_then_write(
        frozen: snap_mod._FrozenSnapshotFile,
        guard_identities: dict[Path, tuple[int, int, int] | None],
    ) -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            dst.parent.rename(fake_home / "original-live")
            dst.parent.mkdir()
            (dst.parent / dst.name).write_text("unrelated replacement\n")
        real_write(frozen, guard_identities)

    monkeypatch.setattr(snap_mod, "_write_restored_file", swap_then_write)

    with pytest.raises(SetforgeError, match="parent changed before write"):
        snap_mod._apply_restore_plan(plan)

    assert (dst.parent / dst.name).read_text() == "unrelated replacement\n"


def test_restore_plan_allows_unrelated_sibling_change(fake_home: Path) -> None:
    ctx, _, dst = _build_ctx(fake_home)
    dst.parent.mkdir(parents=True)
    dst.write_text("snapshot body\n")
    meta = _create(ctx, "sibling-change")
    dst.write_text("pre-restore body\n")
    plan = snap_mod._plan_restore_snapshot(
        meta.snapshot_id,
        cfg=ctx.cfg,
        resolved=ctx.resolved,
        repo_root=ctx.repo_root,
        profile=ctx.profile,
    )
    (fake_home / "unrelated-sibling").write_text("unrelated\n")

    snap_mod._apply_restore_plan(plan)

    assert dst.read_text() == "snapshot body\n"


def test_restore_plan_recreates_deleted_destination_tree(fake_home: Path) -> None:
    ctx, _, dst = _build_ctx(fake_home)
    dst.parent.mkdir(parents=True)
    dst.write_text("snapshot body\n")
    meta = _create(ctx, "deleted-tree")
    shutil.rmtree(dst.parent)

    plan = snap_mod._plan_restore_snapshot(
        meta.snapshot_id,
        cfg=ctx.cfg,
        resolved=ctx.resolved,
        repo_root=ctx.repo_root,
        profile=ctx.profile,
    )
    snap_mod._apply_restore_plan(plan)

    assert dst.read_text() == "snapshot body\n"


@pytest.mark.parametrize(
    "destination",
    [
        lambda: snap_mod.operations.journals_root(),
        lambda: snap_mod.operations.journals_root() / "journal.json",
        lambda: snap_mod.operations.journals_root().parent,
        lambda: (
            snap_mod.operations.journals_root().parent / "locks" / "mutation-gate.lock"
        ),
        lambda: snap_mod.snapshots_root(),
        lambda: snap_mod.transitions.transitions_root() / "record" / "meta.json",
    ],
)
def test_restore_rejects_control_path_overlap(
    fake_home: Path, destination: Callable[[], Path]
) -> None:
    meta = snap_mod.SnapshotMeta(
        snapshot_id="20260518T210000Z-control",
        label="control",
        created_at=datetime(2026, 5, 18, 21, 0, tzinfo=UTC),
        profile="test",
        files=(destination(),),
    )

    with pytest.raises(SetforgeError, match="overlaps SetForge control path"):
        snap_mod._require_safe_restore_destinations(meta)


def test_restore_rejects_resolved_control_path_alias(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = fake_home / "state"
    state_root.mkdir()
    monkeypatch.setattr(snap_mod.transitions, "state_root", lambda: state_root)
    alias = fake_home / "state-alias"
    alias.symlink_to(state_root, target_is_directory=True)
    meta = snap_mod.SnapshotMeta(
        snapshot_id="20260518T210000Z-control-alias",
        label="control-alias",
        created_at=datetime(2026, 5, 18, 21, 0, tzinfo=UTC),
        profile="test",
        files=(alias / "transitions" / "record" / "meta.json",),
    )

    with pytest.raises(SetforgeError, match="overlaps SetForge control path"):
        snap_mod._require_safe_restore_destinations(meta)


def test_restore_plan_rejects_live_symlink_parent(fake_home: Path) -> None:
    target_parent = fake_home / "target-parent"
    target_parent.mkdir()
    linked_parent = fake_home / "linked-parent"
    linked_parent.symlink_to(target_parent, target_is_directory=True)
    ctx, _, dst = _build_ctx(fake_home, dst_template=str(linked_parent / "text.txt"))
    dst.write_text("snapshot body\n")
    meta = _create(ctx, "linked-live-parent")

    with pytest.raises(SetforgeError, match="symlinked destination parent"):
        snap_mod._plan_restore_snapshot(
            meta.snapshot_id,
            cfg=ctx.cfg,
            resolved=ctx.resolved,
            repo_root=ctx.repo_root,
            profile=ctx.profile,
        )


def test_freeze_file_refuses_metadata_change_during_capture(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = fake_home / "changing.txt"
    path.write_text("stable bytes\n")
    real_snapshot_path = snap_mod.operations.snapshot_path

    def capture_then_touch(candidate: Path) -> snap_mod.operations.PathSnapshot:
        captured = real_snapshot_path(candidate)
        current = candidate.stat().st_mtime_ns
        os.utime(candidate, ns=(current + 1_000_000, current + 1_000_000))
        return captured

    monkeypatch.setattr(snap_mod.operations, "snapshot_path", capture_then_touch)

    with pytest.raises(SetforgeError, match="changed while planning"):
        snap_mod._freeze_file(path)


def test_freeze_file_refuses_disappearance_during_capture(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = fake_home / "disappearing.txt"
    path.write_text("present before capture\n")

    def disappear(candidate: Path) -> snap_mod.operations.PathSnapshot:
        candidate.unlink()
        return snap_mod.operations.PathSnapshot(
            path=candidate,
            kind=snap_mod.operations.SnapshotKind.ABSENT,
        )

    monkeypatch.setattr(snap_mod.operations, "snapshot_path", disappear)

    with pytest.raises(SetforgeError, match="changed while planning"):
        snap_mod._freeze_file(path)


def test_freeze_file_refuses_directory(fake_home: Path) -> None:
    directory = fake_home / "directory"
    directory.mkdir()

    with pytest.raises(SetforgeError, match="non-regular non-symlink"):
        snap_mod._freeze_file(directory)


def test_restore_plan_requires_complete_effective_context(fake_home: Path) -> None:
    with pytest.raises(SetforgeError, match="incomplete effective-profile context"):
        snap_mod._plan_restore_snapshot("missing", profile="test")


@pytest.mark.parametrize("parent_kind", ["symlink", "file"])
def test_restore_plan_rejects_unsafe_mirror_parent(
    fake_home: Path, parent_kind: str
) -> None:
    ctx, _, dst = _build_ctx(fake_home)
    dst.parent.mkdir(parents=True)
    dst.write_text("captured\n")
    meta = _create(ctx, "unsafe-parent")
    snapshot_dir = snap_mod.snapshots_root() / meta.snapshot_id
    first_component = snapshot_dir / dst.relative_to("/").parts[0]
    if parent_kind == "symlink":
        relocated = snapshot_dir / "relocated"
        first_component.rename(relocated)
        first_component.symlink_to(relocated, target_is_directory=True)
    else:
        shutil.rmtree(first_component)
        first_component.write_text("not a directory", encoding="utf-8")

    with pytest.raises(SetforgeError, match="unsafe mirror parent"):
        snap_mod._plan_restore_snapshot(
            meta.snapshot_id,
            cfg=ctx.cfg,
            resolved=ctx.resolved,
            repo_root=ctx.repo_root,
            profile=ctx.profile,
        )


@pytest.mark.parametrize("symlink_first", [True, False])
def test_capture_rejects_overlapping_symlink_ancestor_without_escape(
    fake_home: Path, symlink_first: bool
) -> None:
    external = fake_home / "external"
    external.mkdir()
    escaped = external / "escaped.txt"
    escaped.write_text("outside stays unchanged\n", encoding="utf-8")
    link = fake_home / "live-link"
    link.symlink_to(external, target_is_directory=True)
    partial = fake_home / "partial"
    partial.mkdir()
    paths: tuple[Path, ...] = (link, link / "escaped.txt")
    if not symlink_first:
        paths = tuple(reversed(paths))

    with pytest.raises(SetforgeError, match=r"unsafe mirror parent|mirror directory"):
        snap_mod._capture_files(partial, paths)

    assert escaped.read_text(encoding="utf-8") == "outside stays unchanged\n"


@pytest.mark.parametrize(
    "kind",
    [snap_mod.operations.SnapshotKind.FILE, snap_mod.operations.SnapshotKind.SYMLINK],
)
def test_capture_rejects_commit_marker_mirror_identity(
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: snap_mod.operations.SnapshotKind,
) -> None:
    partial = fake_home / "partial"
    partial.mkdir()
    live_path = Path("/_meta.json")
    frozen = snap_mod._FrozenSnapshotFile(
        path=live_path,
        kind=kind,
        mode=0o600,
        payload=b"body" if kind is snap_mod.operations.SnapshotKind.FILE else None,
        link_target=(
            "target" if kind is snap_mod.operations.SnapshotKind.SYMLINK else None
        ),
        mtime_ns=1,
    )
    monkeypatch.setattr(snap_mod, "_freeze_file", lambda _path: frozen)

    with pytest.raises(SetforgeError, match="unsafe live path"):
        snap_mod._capture_files(partial, (live_path,))


def test_restore_plan_preserves_frozen_mode_and_mtime(fake_home: Path) -> None:
    ctx, _, dst = _build_ctx(fake_home)
    dst.parent.mkdir(parents=True)
    dst.write_text("mode body\n")
    dst.chmod(0o640)
    expected_mtime_ns = 1_700_000_000_123_456_789
    os.utime(dst, ns=(expected_mtime_ns, expected_mtime_ns))
    meta = _create(ctx, "mode-restore")
    dst.write_text("drift\n")
    dst.chmod(0o600)

    snap_mod.restore_snapshot(meta.snapshot_id, pre_snapshot=False)

    assert stat.S_IMODE(dst.stat().st_mode) == 0o640
    assert dst.stat().st_mtime_ns == expected_mtime_ns


def test_restore_plan_recreates_frozen_symlink(fake_home: Path) -> None:
    ctx, _, dst = _build_ctx(fake_home)
    dst.parent.mkdir(parents=True)
    target = fake_home / "target.txt"
    target.write_text("target\n")
    dst.symlink_to(target)
    expected_mtime_ns = 1_700_000_000_444_444_444
    os.utime(dst, ns=(expected_mtime_ns, expected_mtime_ns), follow_symlinks=False)
    meta = _create(ctx, "symlink-restore")
    dst.unlink()
    dst.write_text("regular drift\n")

    snap_mod.restore_snapshot(meta.snapshot_id, pre_snapshot=False)

    assert dst.is_symlink()
    assert dst.readlink() == target
    assert dst.lstat().st_mtime_ns == expected_mtime_ns


def test_resolve_snapshot_skips_meta_missing_dirs(
    fake_home: Path,
) -> None:
    """``resolve_snapshot`` does not match a hand-corrupted dir without ``_meta.json``.

    ``list_snapshots`` filters meta-less dirs out, so ``resolve_snapshot``
    (which iterates over its result) never sees them and raises
    ``not found`` rather than returning a half-built ``SnapshotMeta``.
    """
    root = snap_mod.snapshots_root()
    root.mkdir(parents=True)
    bad = root / "20260101T000000Z-broken"
    bad.mkdir()
    with pytest.raises(SetforgeError, match="not found"):
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
    dst.symlink_to(other)
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


def test_create_reports_success_when_post_commit_pruning_fails(
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctx, _, dst = _build_ctx(fake_home)
    dst.parent.mkdir(parents=True)
    dst.write_text("committed body\n")

    def fail_prune(_keep: int) -> int:
        raise OSError("retention filesystem unavailable")

    monkeypatch.setattr(snap_mod, "prune_snapshots", fail_prune)

    meta = _create(ctx, "committed")

    assert snap_mod.resolve_snapshot(meta.snapshot_id) == meta
    assert "was created, but retention pruning failed" in caplog.text


def test_create_success_survives_diagnostic_logger_failure(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _, dst = _build_ctx(fake_home)
    dst.parent.mkdir(parents=True)
    dst.write_text("committed body\n")
    monkeypatch.setattr(
        snap_mod,
        "prune_snapshots",
        lambda _keep: (_ for _ in ()).throw(OSError("prune failed")),
    )
    monkeypatch.setattr(
        snap_mod._LOGGER,
        "warning",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("log failed")),
    )

    meta = _create(ctx, "committed")

    assert snap_mod.resolve_snapshot(meta.snapshot_id) == meta


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
