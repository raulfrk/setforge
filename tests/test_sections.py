"""Tests for user-section marker parsing and merging."""

import hashlib
import logging

import pytest

from my_setup.errors import MarkerError
from my_setup.sections import (
    extract_marker_hashes,
    extract_sections,
    hash_sections,
    merge_sections,
)


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


# ---------------------------------------------------------------------------
# dotfiles-xyw — hash_sections primitive
# ---------------------------------------------------------------------------


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def test_hash_sections_coverage_parity_with_extract_sections() -> None:
    text = (
        "<!-- my-setup:user-section start a -->\n"
        "alpha\n"
        "<!-- my-setup:user-section end a -->\n"
        "<!-- my-setup:user-section start b -->\n"
        "beta\n"
        "<!-- my-setup:user-section end b -->\n"
    )
    assert hash_sections(text).keys() == extract_sections(text).keys()


def test_hash_sections_identical_content_identical_hash() -> None:
    body = "shared body\n"
    t1 = (
        "<!-- my-setup:user-section start a -->\n"
        f"{body}"
        "<!-- my-setup:user-section end a -->\n"
    )
    t2 = (
        "different surrounding text\n"
        "<!-- my-setup:user-section start a -->\n"
        f"{body}"
        "<!-- my-setup:user-section end a -->\n"
        "more surrounding\n"
    )
    assert hash_sections(t1)["a"] == hash_sections(t2)["a"]


def test_hash_sections_differing_content_differing_hash() -> None:
    t1 = (
        "<!-- my-setup:user-section start a -->\n"
        "v1\n"
        "<!-- my-setup:user-section end a -->\n"
    )
    t2 = (
        "<!-- my-setup:user-section start a -->\n"
        "v2\n"
        "<!-- my-setup:user-section end a -->\n"
    )
    assert hash_sections(t1)["a"] != hash_sections(t2)["a"]


def test_hash_sections_hex_digest_shape_64_lowercase() -> None:
    text = (
        "<!-- my-setup:user-section start a -->\n"
        "body\n"
        "<!-- my-setup:user-section end a -->\n"
    )
    digest = hash_sections(text)["a"]
    assert len(digest) == 64
    assert digest == digest.lower()
    assert all(c in "0123456789abcdef" for c in digest)


def test_hash_sections_composition_invariant() -> None:
    """hash_sections(t)[n] == sha256(extract_sections(t)[n]).hexdigest()."""
    text = (
        "<!-- my-setup:user-section start a -->\n"
        "alpha\n"
        "<!-- my-setup:user-section end a -->\n"
    )
    body = extract_sections(text)["a"]
    assert hash_sections(text)["a"] == _sha256_hex(body)


def test_hash_sections_unnamed_keying_mirrors_extract_sections() -> None:
    text = (
        "<!-- my-setup:user-section start -->\n"
        "first\n"
        "<!-- my-setup:user-section end -->\n"
        "<!-- my-setup:user-section start -->\n"
        "second\n"
        "<!-- my-setup:user-section end -->\n"
    )
    assert set(hash_sections(text).keys()) == {"0", "1"}


def test_hash_sections_propagates_marker_error() -> None:
    text = "<!-- my-setup:user-section end a -->\n"
    with pytest.raises(MarkerError, match="without matching start"):
        hash_sections(text)


# ---------------------------------------------------------------------------
# dotfiles-xyw — extract_marker_hashes
# ---------------------------------------------------------------------------


def test_extract_marker_hashes_returns_hash_when_present() -> None:
    text = (
        "<!-- my-setup:user-section start a -->\n"
        "body\n"
        f"<!-- my-setup:user-section end a hash={_HASH_HEX_64} -->\n"
    )
    assert extract_marker_hashes(text) == {"a": _HASH_HEX_64}


def test_extract_marker_hashes_returns_none_for_legacy_hashless() -> None:
    text = (
        "<!-- my-setup:user-section start a -->\n"
        "body\n"
        "<!-- my-setup:user-section end a -->\n"
    )
    assert extract_marker_hashes(text) == {"a": None}


def test_extract_marker_hashes_mixed_file() -> None:
    text = (
        "<!-- my-setup:user-section start a -->\n"
        "alpha\n"
        f"<!-- my-setup:user-section end a hash={_HASH_HEX_64} -->\n"
        "<!-- my-setup:user-section start b -->\n"
        "beta\n"
        "<!-- my-setup:user-section end b -->\n"
    )
    assert extract_marker_hashes(text) == {"a": _HASH_HEX_64, "b": None}


def test_extract_marker_hashes_coverage_parity_with_extract_sections() -> None:
    text = (
        "<!-- my-setup:user-section start a -->\n"
        "alpha\n"
        "<!-- my-setup:user-section end a -->\n"
        "<!-- my-setup:user-section start b -->\n"
        "beta\n"
        f"<!-- my-setup:user-section end b hash={_HASH_HEX_64} -->\n"
    )
    assert extract_marker_hashes(text).keys() == extract_sections(text).keys()


def test_extract_marker_hashes_propagates_marker_error() -> None:
    text = "<!-- my-setup:user-section start a -->\nbody\n"
    with pytest.raises(MarkerError, match="unclosed"):
        extract_marker_hashes(text)
