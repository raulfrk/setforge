"""Tests for the disposition (stored-base 3-way merge) branch of copy_atomic.

These exercise :func:`setforge.deploy.copy_atomic` with a ``disposition`` set,
asserting it routes content through :func:`setforge.disposition_merge.resolve_file`
and reports back ``new_base`` / ``merge_conflicts`` so the install loop can
re-baseline the stored base. The no-disposition regression case confirms the
legacy preserve path is byte-for-byte unchanged.
"""

from pathlib import Path

from setforge.config import Disposition
from setforge.deploy import DeployAction, copy_atomic
from setforge.section_wizard import ReconcileAuto


def test_disposition_shared_clean_merge_writes_merged_and_advances(
    tmp_path: Path,
) -> None:
    base = "line1\nline2\nline3\n"
    # live edits line1; tracked edits line3 — non-overlapping regions.
    live = "LIVE1\nline2\nline3\n"
    tracked = "line1\nline2\nTRACKED3\n"
    src = tmp_path / "src.md"
    src.write_text(tracked)
    dst = tmp_path / "dst.md"
    dst.write_text(live)

    result = copy_atomic(src, dst, disposition=Disposition.SHARED, base_text=base)

    merged = "LIVE1\nline2\nTRACKED3\n"
    assert dst.read_text() == merged
    assert result.action is DeployAction.UPDATED
    assert result.new_base == merged
    assert result.merge_conflicts == []


def test_disposition_shared_first_run_no_base_seeds_tracked(
    tmp_path: Path,
) -> None:
    tracked = "tracked content\n"
    src = tmp_path / "src.md"
    src.write_text(tracked)
    dst = tmp_path / "dst.md"
    dst.write_text("different live content\n")

    result = copy_atomic(src, dst, disposition=Disposition.SHARED, base_text=None)

    assert dst.read_text() == tracked
    assert result.new_base == tracked
    assert result.merge_conflicts == []


def test_disposition_shared_conflict_bare_keeps_live_defers_base(
    tmp_path: Path,
) -> None:
    base = "shared\n"
    live = "LIVE-edit\n"
    tracked = "TRACKED-edit\n"
    src = tmp_path / "src.md"
    src.write_text(tracked)
    dst = tmp_path / "dst.md"
    dst.write_text(live)

    result = copy_atomic(
        src, dst, disposition=Disposition.SHARED, base_text=base, merge_auto=None
    )

    assert dst.read_text() == live
    assert result.new_base is None
    assert result.merge_conflicts != []


def test_disposition_shared_conflict_use_tracked_takes_tracked_advances(
    tmp_path: Path,
) -> None:
    base = "shared\n"
    live = "LIVE-edit\n"
    tracked = "TRACKED-edit\n"
    src = tmp_path / "src.md"
    src.write_text(tracked)
    dst = tmp_path / "dst.md"
    dst.write_text(live)

    result = copy_atomic(
        src,
        dst,
        disposition=Disposition.SHARED,
        base_text=base,
        merge_auto=ReconcileAuto.USE_TRACKED,
    )

    assert dst.read_text() == tracked
    assert result.new_base is not None
    assert result.merge_conflicts != []


def test_disposition_pinned_keeps_live_noop_no_advance(tmp_path: Path) -> None:
    base = "base\n"
    live = "live content\n"
    tracked = "tracked content\n"
    src = tmp_path / "src.md"
    src.write_text(tracked)
    dst = tmp_path / "dst.md"
    dst.write_text(live)

    result = copy_atomic(src, dst, disposition=Disposition.PINNED, base_text=base)

    assert dst.read_text() == live
    assert result.action is DeployAction.NOOP
    assert result.new_base is None


def test_disposition_clean_merge_equal_to_live_noop_but_advances(
    tmp_path: Path,
) -> None:
    # tracked == base: only live diverged. Clean merge yields live verbatim,
    # so the write is a NOOP — but the base still re-baselines to the merged
    # text (the load-bearing NOOP+advance case).
    base = "line1\nline2\n"
    tracked = "line1\nline2\n"
    live = "LIVE1\nline2\n"
    src = tmp_path / "src.md"
    src.write_text(tracked)
    dst = tmp_path / "dst.md"
    dst.write_text(live)

    result = copy_atomic(src, dst, disposition=Disposition.SHARED, base_text=base)

    assert dst.read_text() == live
    assert result.action is DeployAction.NOOP
    assert result.new_base == live
    assert result.merge_conflicts == []


def test_no_disposition_plain_deploy_no_base(tmp_path: Path) -> None:
    # A disposition=None file deploys tracked verbatim; new_base /
    # merge_conflicts stay inert (the merge driver never runs).
    src = tmp_path / "src.yaml"
    src.write_text("a: 1\nb: 2\nc: 3\n")
    dst = tmp_path / "dst.yaml"
    dst.write_text("a: 10\nb: 20\nc: 30\n")

    result = copy_atomic(src, dst)

    assert dst.read_text() == "a: 1\nb: 2\nc: 3\n"
    assert result.new_base is None
    assert result.merge_conflicts == []
