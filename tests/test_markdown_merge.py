"""Tests for the pure line-based stored-base 3-way markdown merge engine."""

import pytest

from setforge.markdown_merge import (
    LineConflict,
    MarkdownMergeResult,
    merge_markdown,
)


def test_non_overlapping_edits_auto_merge() -> None:
    """Live edits one paragraph, upstream another -> clean merge with both."""
    base = "para A\n\npara B\n\npara C\n"
    ours = "para A edited by live\n\npara B\n\npara C\n"
    theirs = "para A\n\npara B\n\npara C edited by upstream\n"

    result = merge_markdown(base, ours, theirs)

    assert result.clean is True
    assert result.conflicts == []
    assert result.merged_text is not None
    assert "para A edited by live" in result.merged_text
    assert "para C edited by upstream" in result.merged_text


def test_same_region_edits_conflict() -> None:
    """Both sides edit the same line -> one conflict carrying all three sides."""
    base = "line one\nshared\nline three\n"
    ours = "line one\nlive change\nline three\n"
    theirs = "line one\nupstream change\nline three\n"

    result = merge_markdown(base, ours, theirs)

    assert result.clean is False
    assert result.merged_text is None
    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict.base == ["shared\n"]
    assert conflict.ours == ["live change\n"]
    assert conflict.theirs == ["upstream change\n"]


def test_convergent_identical_edit_taken_once() -> None:
    """Both sides make the same change -> clean, changed line present once."""
    base = "alpha\nbeta\ngamma\n"
    ours = "alpha\nBETA\ngamma\n"
    theirs = "alpha\nBETA\ngamma\n"

    result = merge_markdown(base, ours, theirs)

    assert result.clean is True
    assert result.conflicts == []
    assert result.merged_text == "alpha\nBETA\ngamma\n"
    assert result.merged_text.count("BETA") == 1


@pytest.mark.parametrize(
    "text",
    [
        "a\nb\nc\n",  # trailing newline present
        "a\nb\nc",  # trailing newline absent
        "a\r\nb\r\nc\r\n",  # CRLF line endings
        "",  # empty
    ],
)
def test_idempotency_byte_exact(text: str) -> None:
    """merge_markdown(x, x, x) reproduces x byte-for-byte."""
    result = merge_markdown(text, text, text)

    assert result.clean is True
    assert result.conflicts == []
    assert result.merged_text == text


def test_disagree_on_trailing_newline_is_not_a_conflict() -> None:
    """A final-newline-only disagreement must not produce a spurious conflict."""
    base = "x\ny"
    ours = "x\ny\n"
    theirs = "x\ny"

    result = merge_markdown(base, ours, theirs)

    assert result.clean is True
    assert result.conflicts == []
    assert result.merged_text is not None


def test_both_add_different_lines_same_insertion_point() -> None:
    """Both sides insert a distinct line at the same point.

    Empirically, merge3 driven by PatienceSequenceMatcher treats this as a
    CONFLICT (the base side of the conflict group is empty, ours/theirs carry
    the two divergent insertions). This test pins that observed behavior.
    """
    base = "x\ny\n"
    ours = "x\nAAA\ny\n"
    theirs = "x\nBBB\ny\n"

    result = merge_markdown(base, ours, theirs)

    assert result.clean is False
    assert result.merged_text is None
    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict.base == []
    assert conflict.ours == ["AAA\n"]
    assert conflict.theirs == ["BBB\n"]


def test_patience_anchored_adjacent_edits_no_false_conflict() -> None:
    """Adjacent non-overlapping edits anchored by unique lines merge cleanly.

    The repeated ``\\n`` blank lines plus the unique heading lines give the
    patience matcher stable anchors, so a live edit immediately above an
    upstream edit does not collapse into a single false conflict.
    """
    base = "# Heading\n\nintro line\n\nmiddle line\n\noutro line\n"
    ours = "# Heading\n\nintro line edited live\n\nmiddle line\n\noutro line\n"
    theirs = "# Heading\n\nintro line\n\nmiddle line edited upstream\n\noutro line\n"

    result = merge_markdown(base, ours, theirs)

    assert result.clean is True
    assert result.conflicts == []
    assert result.merged_text is not None
    assert "intro line edited live" in result.merged_text
    assert "middle line edited upstream" in result.merged_text


def test_result_dataclasses_are_frozen() -> None:
    """The result value objects are immutable."""
    conflict = LineConflict(base=[], ours=["a\n"], theirs=["b\n"])
    result = MarkdownMergeResult(clean=False, merged_text=None, conflicts=[conflict])

    with pytest.raises(AttributeError):
        conflict.base = ["x\n"]  # type: ignore[misc]
    with pytest.raises(AttributeError):
        result.clean = True  # type: ignore[misc]
