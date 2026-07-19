"""Unit tests for :mod:`setforge.ui.box` — the theme-aware ``┌─┐`` frame helper.

``frame`` is pure: it takes already-rendered body lines plus a width and
returns a list of framed plain-text lines. No prompt_toolkit Application is
involved, so the assertions can inspect the exact glyph layout.
"""

from __future__ import annotations

from wcwidth import wcswidth

from setforge.ui.box import frame


def _display_width(line: str) -> int:
    w = wcswidth(line)
    return len(line) if w < 0 else w


def test_frame_basic_corners() -> None:
    lines = frame(["hello"], width=20)
    assert lines[0].startswith("┌")
    assert lines[0].endswith("┐")
    assert lines[-1].startswith("└")
    assert lines[-1].endswith("┘")
    # The body row is wrapped in vertical bars.
    body_rows = [ln for ln in lines if ln.startswith("│")]
    assert body_rows
    assert all(ln.endswith("│") for ln in body_rows)
    assert any("hello" in ln for ln in body_rows)


def test_frame_width_clamps_to_100() -> None:
    lines = frame(["x"], width=200)
    assert all(_display_width(ln) <= 100 for ln in lines)
    # Top rule is exactly the clamped width.
    assert _display_width(lines[0]) == 100


def test_frame_width_46_exact() -> None:
    lines = frame(["x"], width=46)
    assert _display_width(lines[0]) == 46
    assert _display_width(lines[-1]) == 46


def test_frame_lines_balanced() -> None:
    lines = frame(["short", "a longer body line here"], title="Title", width=46)
    widths = {_display_width(ln) for ln in lines}
    assert widths == {46}, f"unbalanced line widths: {sorted(widths)}"


def test_frame_wraps_title() -> None:
    lines = frame(["body"], title="Conflict 1/1", width=46)
    assert "Conflict 1/1" in lines[0]
    # The title sits on the top rule between the corners.
    assert lines[0].startswith("┌")
    assert lines[0].endswith("┐")


def test_frame_truncates_overlong_title() -> None:
    # A title longer than the available rule must not overflow the width.
    lines = frame(["body"], title="X" * 200, width=40)
    assert _display_width(lines[0]) == 40
    assert lines[0].startswith("┌")
    assert lines[0].endswith("┐")


def test_frame_truncates_overlong_body() -> None:
    # Body wider than the inner width is truncated, never overflows.
    lines = frame(["Y" * 200], width=30)
    assert all(_display_width(ln) == 30 for ln in lines)


def test_frame_wide_body_balanced() -> None:
    # Wide (CJK) glyphs are 2 display columns each; the body row must still
    # measure exactly the frame width, not overflow it.
    lines = frame(["世界"], width=20)
    assert all(_display_width(ln) == 20 for ln in lines), (
        f"unbalanced widths: {[_display_width(ln) for ln in lines]}"
    )


def test_frame_wide_title_balanced() -> None:
    # A wide-glyph title on the top rule must not push the ``┐`` corner past
    # the bottom rule's display width.
    lines = frame([], title="クリーンアップ", width=30)
    widths = {_display_width(ln) for ln in lines}
    assert widths == {30}, f"unbalanced widths: {sorted(widths)}"


def test_frame_tiny_width_does_not_crash() -> None:
    # Degenerate small widths must clamp to a sane floor without raising.
    for w in (0, 1, 2, 3, 4):
        lines = frame(["z"], width=w)
        assert lines
        assert lines[0].startswith("┌")
