"""Shared themed diff renderer (setforge/ui/diffview.py)."""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from setforge.reconcile.merge_model import Clean, Conflict, MergeResult
from setforge.ui import diffview
from setforge.ui.diffview import DiffModel, RichLayout, RowKind


def _kinds(m: DiffModel) -> list[str]:
    return [row.kind for row in m.rows]


def test_two_way_clean_no_change() -> None:
    m = diffview.two_way_lines(b"a\nb\n", b"a\nb\n")
    assert all(r.kind is RowKind.CTX for r in m.rows)
    assert m.added == 0
    assert m.removed == 0


def test_two_way_all_add_from_empty() -> None:
    m = diffview.two_way_lines(b"", b"x\ny\nz\n")
    assert [r.text for r in m.rows if r.kind is RowKind.ADD] == ["x\n", "y\n", "z\n"]
    assert m.added == 3
    assert m.removed == 0


def test_two_way_del_and_add() -> None:
    m = diffview.two_way_lines(b"old\n", b"new\n")
    kinds = _kinds(m)
    assert RowKind.DEL in kinds
    assert RowKind.ADD in kinds
    assert m.added == 1
    assert m.removed == 1


def test_two_way_empty_both() -> None:
    m = diffview.two_way_lines(b"", b"")
    assert m.rows == []
    assert m.added == 0
    assert m.removed == 0
    assert m.hunks == 0


def test_two_way_context_window() -> None:
    live = b"\n".join(f"L{i}".encode() for i in range(20)) + b"\n"
    upstream = live.replace(b"L10", b"CHANGED")
    full = diffview.two_way_lines(live, upstream, context=None)
    windowed = diffview.two_way_lines(live, upstream, context=1)
    assert len(windowed.rows) < len(full.rows)


def test_three_way_clean_segments() -> None:
    result = MergeResult((Clean(b"a\n"), Clean(b"b\n")))
    m = diffview.three_way_segments(result)
    assert all(r.kind is RowKind.CTX for r in m.rows)
    assert m.hunks == 0


def test_three_way_conflict_segment() -> None:
    result = MergeResult((Clean(b"pre\n"), Conflict(b"base\n", b"ours\n", b"theirs\n")))
    m = diffview.three_way_segments(result)
    kinds = _kinds(m)
    assert RowKind.CONFLICT in kinds
    assert m.hunks == 1
    texts = "".join(r.text for r in m.rows)
    assert "ours" in texts
    assert "theirs" in texts


def test_binary_input_is_stat_line_not_crash() -> None:
    m = diffview.two_way_lines(b"a\x00b", b"c\x00d")
    assert m.binary is True
    assert len(m.rows) == 1
    assert "binary" in m.rows[0].text.lower()
    assert "cannot diff" in m.rows[0].text.lower()


def test_three_way_binary_conflict_side() -> None:
    result = MergeResult((Conflict(b"\x00", b"\x00a", b"\x00b"),))
    m = diffview.three_way_segments(result)
    assert m.binary is True


def test_non_utf8_bytes_do_not_crash() -> None:
    m = diffview.two_way_lines(b"", b"\xff\xfe bad\n")
    assert m.binary is False
    joined = "".join(r.text for r in m.rows)
    assert "�" in joined


def test_control_chars_are_sanitized() -> None:
    m = diffview.two_way_lines(b"", b"tab\there\x07bell\n")
    joined = "".join(r.text for r in m.rows)
    assert "^G" in joined


def test_to_fragments_uses_theme_roles() -> None:
    m = diffview.two_way_lines(b"old\n", b"new\n")
    frags = diffview.to_fragments(m)
    classes = {style for style, _ in frags}
    assert "class:success" in classes
    assert "class:warning" in classes
    assert all(cls.startswith("class:") for cls, _ in frags)


def test_to_fragments_cap_limits_rows() -> None:
    m = diffview.two_way_lines(b"", b"a\nb\nc\nd\ne\n")
    capped = diffview.to_fragments(m, cap=2)
    assert len(capped) < len(diffview.to_fragments(m))


def test_to_rich_renders_without_raw_hex() -> None:
    m = diffview.two_way_lines(b"old\n", b"new\n")
    renderable = diffview.to_rich(m, layout=RichLayout.STACKED)
    sink = io.StringIO()
    console = Console(file=sink, force_terminal=True, color_system="truecolor")
    console.print(renderable)
    assert sink.getvalue()


def test_to_rich_side_by_side_layout() -> None:
    result = MergeResult((Conflict(b"b\n", b"ours\n", b"theirs\n"),))
    m = diffview.three_way_segments(result)
    renderable = diffview.to_rich(m, layout=RichLayout.SIDE_BY_SIDE)
    sink = io.StringIO()
    width = 80
    console = Console(
        file=sink, force_terminal=True, color_system="truecolor", width=width
    )
    console.print(renderable)
    lines = sink.getvalue().splitlines()
    ours_line = next(line for line in lines if "-ours" in line)
    theirs_line = next(line for line in lines if "+theirs" in line)
    # LIVE rows (e.g. "ours") render in the left column, UPSTREAM rows
    # (e.g. "theirs") in the right column.
    assert ours_line.index("ours") < width // 2
    assert theirs_line.index("theirs") >= width // 2


def test_binary_stat_line_reports_byte_count() -> None:
    m = diffview.two_way_lines(b"x" * 10, b"y\x00" * 5)
    assert "10 bytes" in m.rows[0].text or "bytes" in m.rows[0].text


@pytest.mark.parametrize("layout", list(RichLayout))
def test_to_rich_every_layout(layout: RichLayout) -> None:
    m = diffview.two_way_lines(b"a\n", b"b\n")
    console = Console(file=io.StringIO(), force_terminal=True)
    console.print(diffview.to_rich(m, layout=layout))
