"""Tests for user-section marker parsing and merging."""

import logging

import pytest

from my_setup.errors import MarkerError
from my_setup.sections import extract_sections, merge_sections


def test_no_markers_passthrough() -> None:
    text = "line 1\nline 2\nline 3\n"
    assert extract_sections(text) == {}
    assert merge_sections(text, {}) == text


def test_single_unnamed_section_extract() -> None:
    text = (
        "before\n"
        "<!-- my-setup:user-section start -->\n"
        "preserved 1\npreserved 2\n"
        "<!-- my-setup:user-section end -->\n"
        "after\n"
    )
    assert extract_sections(text) == {"0": "preserved 1\npreserved 2\n"}


def test_single_unnamed_section_merge_round_trip() -> None:
    tracked = (
        "before\n"
        "<!-- my-setup:user-section start -->\n"
        "<!-- my-setup:user-section end -->\n"
        "after\n"
    )
    live_text = (
        "before\n"
        "<!-- my-setup:user-section start -->\n"
        "user content\n"
        "<!-- my-setup:user-section end -->\n"
        "after\n"
    )
    live_sections = extract_sections(live_text)
    merged = merge_sections(tracked, live_sections)
    assert "user content\n" in merged
    assert merged.startswith("before\n")
    assert merged.endswith("after\n")


def test_two_named_sections_independent() -> None:
    tracked = (
        "<!-- my-setup:user-section start workflow -->\n"
        "<!-- my-setup:user-section end workflow -->\n"
        "between\n"
        "<!-- my-setup:user-section start commits -->\n"
        "<!-- my-setup:user-section end commits -->\n"
    )
    live_sections = {"workflow": "wf content\n", "commits": "cm content\n"}
    merged = merge_sections(tracked, live_sections)
    assert "wf content" in merged
    assert "cm content" in merged
    assert merged.index("wf content") < merged.index("between")
    assert merged.index("between") < merged.index("cm content")


def test_extract_named_sections_keyed_by_name() -> None:
    text = (
        "<!-- my-setup:user-section start workflow -->\n"
        "wf\n"
        "<!-- my-setup:user-section end workflow -->\n"
        "<!-- my-setup:user-section start commits -->\n"
        "cm\n"
        "<!-- my-setup:user-section end commits -->\n"
    )
    assert extract_sections(text) == {"workflow": "wf\n", "commits": "cm\n"}


def test_mismatched_missing_end_raises() -> None:
    text = "<!-- my-setup:user-section start -->\ncontent\n"
    with pytest.raises(MarkerError, match="unclosed"):
        extract_sections(text)


def test_end_without_start_raises() -> None:
    text = "<!-- my-setup:user-section end -->\n"
    with pytest.raises(MarkerError, match="without matching start"):
        extract_sections(text)


def test_name_mismatch_raises() -> None:
    text = (
        "<!-- my-setup:user-section start workflow -->\n"
        "<!-- my-setup:user-section end commits -->\n"
    )
    with pytest.raises(MarkerError, match="does not match"):
        extract_sections(text)


def test_nested_section_raises() -> None:
    text = (
        "<!-- my-setup:user-section start outer -->\n"
        "<!-- my-setup:user-section start inner -->\n"
        "<!-- my-setup:user-section end inner -->\n"
        "<!-- my-setup:user-section end outer -->\n"
    )
    with pytest.raises(MarkerError, match="nested"):
        extract_sections(text)


def test_live_extra_section_warns_and_drops(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tracked = (
        "<!-- my-setup:user-section start workflow -->\n"
        "<!-- my-setup:user-section end workflow -->\n"
    )
    live = {"workflow": "wf\n", "extra": "extra\n"}
    with caplog.at_level(logging.WARNING):
        merged = merge_sections(tracked, live)
    assert "extra" not in merged
    assert any("extra" in rec.getMessage() for rec in caplog.records)


def test_tracked_section_absent_from_live_keeps_placeholder() -> None:
    tracked = (
        "<!-- my-setup:user-section start workflow -->\n"
        "placeholder text\n"
        "<!-- my-setup:user-section end workflow -->\n"
    )
    merged = merge_sections(tracked, {})
    assert "placeholder text" in merged


def test_extract_unnamed_indices_in_order() -> None:
    text = (
        "<!-- my-setup:user-section start -->\n"
        "first\n"
        "<!-- my-setup:user-section end -->\n"
        "<!-- my-setup:user-section start -->\n"
        "second\n"
        "<!-- my-setup:user-section end -->\n"
    )
    assert extract_sections(text) == {"0": "first\n", "1": "second\n"}


# ---------------------------------------------------------------------------
# dotfiles-xyw — marker regex extension: optional hash= segment on end markers
# ---------------------------------------------------------------------------

_HASH_HEX_64 = "a" * 64


def test_extract_sections_parses_end_marker_with_hash() -> None:
    """End marker with hash= segment parses identically to one without —
    name and body unchanged."""
    text = (
        "<!-- my-setup:user-section start a -->\n"
        "body\n"
        f"<!-- my-setup:user-section end a hash={_HASH_HEX_64} -->\n"
    )
    assert extract_sections(text) == {"a": "body\n"}


def test_extract_sections_parses_unnamed_end_marker_with_hash() -> None:
    """An unnamed end marker carrying only a hash= segment still parses."""
    text = (
        "<!-- my-setup:user-section start -->\n"
        "body\n"
        f"<!-- my-setup:user-section end hash={_HASH_HEX_64} -->\n"
    )
    assert extract_sections(text) == {"0": "body\n"}


def test_extract_sections_legacy_hashless_end_marker_still_parses() -> None:
    """Backward-compat: pre-xyw end markers (no hash=) remain valid."""
    text = (
        "<!-- my-setup:user-section start a -->\n"
        "body\n"
        "<!-- my-setup:user-section end a -->\n"
    )
    assert extract_sections(text) == {"a": "body\n"}
