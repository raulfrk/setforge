"""Tests for the ordered-segment view of the markdown 3-way merge engine.

The segment view re-walks the same ``merge_groups()`` the byte-exact
:func:`merge_markdown` walks, but preserves the interleaved ordering of clean
blocks and conflict hunks so a resolved file can be rebuilt after a caller
chooses a side per conflict. These tests pin that the clean path is
byte-identical to ``merge_markdown`` and that conflicts carry the same
sides and rebuild correctly.
"""

import pytest

from setforge.markdown_merge import (
    CleanSegment,
    LineConflict,
    _split_strip_final,
    merge_markdown,
    merge_markdown_segments,
    resolve_segments,
)


def _ours_term(ours: str) -> str:
    """The trailing terminator merge_markdown restores for ``ours``."""
    _lines, term = _split_strip_final(ours)
    return term


def _choose_ours(conflict: LineConflict) -> list[str]:
    return conflict.ours


def _choose_theirs(conflict: LineConflict) -> list[str]:
    return conflict.theirs


@pytest.mark.parametrize(
    ("base", "ours", "theirs"),
    [
        # Non-overlapping edits on separate paragraphs -> clean.
        (
            "para A\n\npara B\n\npara C\n",
            "para A edited by live\n\npara B\n\npara C\n",
            "para A\n\npara B\n\npara C edited by upstream\n",
        ),
        # Pure idempotent self-merge with trailing newline.
        ("a\nb\nc\n", "a\nb\nc\n", "a\nb\nc\n"),
        # No trailing newline.
        ("a\nb\nc", "a\nb\nc", "a\nb\nc"),
        # CRLF line endings.
        ("a\r\nb\r\nc\r\n", "a\r\nb\r\nc\r\n", "a\r\nb\r\nc\r\n"),
        # Trailing-newline disagreement (not a conflict).
        ("x\ny", "x\ny\n", "x\ny"),
        # Empty document.
        ("", "", ""),
        # Sole terminator.
        ("\n", "\n", "\n"),
    ],
)
def test_clean_resolve_is_byte_exact(base: str, ours: str, theirs: str) -> None:
    """resolve_segments over a clean merge equals merge_markdown byte-for-byte."""
    result = merge_markdown(base, ours, theirs)
    assert result.clean is True
    assert result.merged_text is not None

    segments = merge_markdown_segments(base, ours, theirs)
    assert all(isinstance(seg, CleanSegment) for seg in segments)

    rebuilt = resolve_segments(segments, _choose_ours, _ours_term(ours))
    assert rebuilt == result.merged_text


def test_same_tag_edit_carried_once() -> None:
    """Both sides make the identical edit -> one clean segment, no conflict."""
    base = "alpha\nbeta\ngamma\n"
    ours = "alpha\nBETA\ngamma\n"
    theirs = "alpha\nBETA\ngamma\n"

    segments = merge_markdown_segments(base, ours, theirs)

    assert all(isinstance(seg, CleanSegment) for seg in segments)
    rebuilt = resolve_segments(segments, _choose_ours, _ours_term(ours))
    assert rebuilt == "alpha\nBETA\ngamma\n"
    assert rebuilt.count("BETA") == 1


def test_conflict_at_file_start_adjacent_block_once() -> None:
    """A conflict at index 0 -> the trailing clean block appears exactly once."""
    base = "shared\ntail one\ntail two\n"
    ours = "live change\ntail one\ntail two\n"
    theirs = "upstream change\ntail one\ntail two\n"

    segments = merge_markdown_segments(base, ours, theirs)

    conflicts = [s for s in segments if isinstance(s, LineConflict)]
    assert len(conflicts) == 1

    ours_text = resolve_segments(segments, _choose_ours, _ours_term(ours))
    assert ours_text == "live change\ntail one\ntail two\n"
    assert ours_text.count("tail one") == 1
    assert ours_text.count("tail two") == 1


def test_conflict_at_file_end_adjacent_block_once() -> None:
    """A conflict at the final line -> the leading clean block appears once."""
    base = "head one\nhead two\nshared"
    ours = "head one\nhead two\nlive change"
    theirs = "head one\nhead two\nupstream change"

    segments = merge_markdown_segments(base, ours, theirs)

    conflicts = [s for s in segments if isinstance(s, LineConflict)]
    assert len(conflicts) == 1

    ours_text = resolve_segments(segments, _choose_ours, _ours_term(ours))
    assert ours_text == "head one\nhead two\nlive change"
    assert ours_text.count("head one") == 1
    assert ours_text.count("head two") == 1


def test_genuine_conflict_sides_match_merge_markdown() -> None:
    """The conflict hunk carries the same sides merge_markdown reports, and
    choosing each side rebuilds that side's lines at the conflict."""
    base = "line one\nshared\nline three\n"
    ours = "line one\nlive change\nline three\n"
    theirs = "line one\nupstream change\nline three\n"

    result = merge_markdown(base, ours, theirs)
    assert result.clean is False
    assert len(result.conflicts) == 1
    md_conflict = result.conflicts[0]

    segments = merge_markdown_segments(base, ours, theirs)
    seg_conflicts = [s for s in segments if isinstance(s, LineConflict)]
    assert len(seg_conflicts) == 1
    seg_conflict = seg_conflicts[0]

    assert seg_conflict.base == md_conflict.base
    assert seg_conflict.ours == md_conflict.ours
    assert seg_conflict.theirs == md_conflict.theirs

    ours_text = resolve_segments(segments, _choose_ours, _ours_term(ours))
    assert ours_text == "line one\nlive change\nline three\n"

    theirs_text = resolve_segments(segments, _choose_theirs, _ours_term(ours))
    assert theirs_text == "line one\nupstream change\nline three\n"


def test_clean_segment_is_frozen() -> None:
    """CleanSegment is an immutable value object."""
    segment = CleanSegment(lines=["a\n"])
    with pytest.raises(AttributeError):
        segment.lines = ["b\n"]  # type: ignore[misc]
