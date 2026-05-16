"""Tests for user-section marker parsing and merging."""

import hashlib
import logging

import pytest

from my_setup.errors import MarkerError
from my_setup.sections import (
    SectionSemantics,
    extract_marker_hashes,
    extract_sections,
    hash_sections,
    merge_sections,
    section_semantics,
    set_marker_hashes,
)


def test_no_markers_passthrough() -> None:
    text = "line 1\nline 2\nline 3\n"
    assert extract_sections(text) == {}
    assert merge_sections(text, {}) == text


def test_single_unnamed_section_extract() -> None:
    text = (
        "before\n"
        "<!-- my-setup:user-section start host-local -->\n"
        "preserved 1\npreserved 2\n"
        "<!-- my-setup:user-section end host-local -->\n"
        "after\n"
    )
    assert extract_sections(text) == {"0": "preserved 1\npreserved 2\n"}


def test_single_unnamed_section_merge_round_trip() -> None:
    tracked = (
        "before\n"
        "<!-- my-setup:user-section start host-local -->\n"
        "<!-- my-setup:user-section end host-local -->\n"
        "after\n"
    )
    live_text = (
        "before\n"
        "<!-- my-setup:user-section start host-local -->\n"
        "user content\n"
        "<!-- my-setup:user-section end host-local -->\n"
        "after\n"
    )
    live_sections = extract_sections(live_text)
    merged = merge_sections(tracked, live_sections)
    assert "user content\n" in merged
    assert merged.startswith("before\n")
    assert merged.endswith("after\n")


def test_two_named_sections_independent() -> None:
    tracked = (
        "<!-- my-setup:user-section start shared workflow -->\n"
        "<!-- my-setup:user-section end shared workflow -->\n"
        "between\n"
        "<!-- my-setup:user-section start shared commits -->\n"
        "<!-- my-setup:user-section end shared commits -->\n"
    )
    live_sections = {"workflow": "wf content\n", "commits": "cm content\n"}
    merged = merge_sections(tracked, live_sections)
    assert "wf content" in merged
    assert "cm content" in merged
    assert merged.index("wf content") < merged.index("between")
    assert merged.index("between") < merged.index("cm content")


def test_extract_named_sections_keyed_by_name() -> None:
    text = (
        "<!-- my-setup:user-section start shared workflow -->\n"
        "wf\n"
        "<!-- my-setup:user-section end shared workflow -->\n"
        "<!-- my-setup:user-section start shared commits -->\n"
        "cm\n"
        "<!-- my-setup:user-section end shared commits -->\n"
    )
    assert extract_sections(text) == {"workflow": "wf\n", "commits": "cm\n"}


def test_mismatched_missing_end_raises() -> None:
    text = "<!-- my-setup:user-section start host-local -->\ncontent\n"
    with pytest.raises(MarkerError, match="unclosed"):
        extract_sections(text)


def test_end_without_start_raises() -> None:
    text = "<!-- my-setup:user-section end host-local -->\n"
    with pytest.raises(MarkerError, match="without matching start"):
        extract_sections(text)


def test_name_mismatch_raises() -> None:
    text = (
        "<!-- my-setup:user-section start shared workflow -->\n"
        "<!-- my-setup:user-section end shared commits -->\n"
    )
    with pytest.raises(MarkerError, match="does not match"):
        extract_sections(text)


def test_nested_section_raises() -> None:
    text = (
        "<!-- my-setup:user-section start shared outer -->\n"
        "<!-- my-setup:user-section start shared inner -->\n"
        "<!-- my-setup:user-section end shared inner -->\n"
        "<!-- my-setup:user-section end shared outer -->\n"
    )
    with pytest.raises(MarkerError, match="nested"):
        extract_sections(text)


def test_live_extra_section_warns_and_drops(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tracked = (
        "<!-- my-setup:user-section start shared workflow -->\n"
        "<!-- my-setup:user-section end shared workflow -->\n"
    )
    live = {"workflow": "wf\n", "extra": "extra\n"}
    with caplog.at_level(logging.WARNING):
        merged = merge_sections(tracked, live)
    assert "extra" not in merged
    assert any("extra" in rec.getMessage() for rec in caplog.records)


def test_tracked_section_absent_from_live_keeps_placeholder() -> None:
    tracked = (
        "<!-- my-setup:user-section start shared workflow -->\n"
        "placeholder text\n"
        "<!-- my-setup:user-section end shared workflow -->\n"
    )
    merged = merge_sections(tracked, {})
    assert "placeholder text" in merged


def test_extract_unnamed_indices_in_order() -> None:
    text = (
        "<!-- my-setup:user-section start host-local -->\n"
        "first\n"
        "<!-- my-setup:user-section end host-local -->\n"
        "<!-- my-setup:user-section start host-local -->\n"
        "second\n"
        "<!-- my-setup:user-section end host-local -->\n"
    )
    assert extract_sections(text) == {"0": "first\n", "1": "second\n"}


# ---------------------------------------------------------------------------
# dotfiles-xyw — marker regex extension: optional hash= segment on end markers
# ---------------------------------------------------------------------------

_HASH_HEX_64: str = "a" * 64


def test_extract_sections_parses_end_marker_with_hash() -> None:
    """End marker with hash= segment parses identically to one without —
    name and body unchanged."""
    text = (
        "<!-- my-setup:user-section start shared a -->\n"
        "body\n"
        f"<!-- my-setup:user-section end shared a hash={_HASH_HEX_64} -->\n"
    )
    assert extract_sections(text) == {"a": "body\n"}


def test_extract_sections_parses_unnamed_end_marker_with_hash() -> None:
    """An unnamed end marker carrying only a hash= segment still parses."""
    text = (
        "<!-- my-setup:user-section start host-local -->\n"
        "body\n"
        f"<!-- my-setup:user-section end host-local hash={_HASH_HEX_64} -->\n"
    )
    assert extract_sections(text) == {"0": "body\n"}


def test_extract_sections_hashless_end_marker_still_parses() -> None:
    """Backward-compat: end markers without hash= remain valid (xyw)."""
    text = (
        "<!-- my-setup:user-section start shared a -->\n"
        "body\n"
        "<!-- my-setup:user-section end shared a -->\n"
    )
    assert extract_sections(text) == {"a": "body\n"}


# ---------------------------------------------------------------------------
# dotfiles-xyw — hash_sections primitive
# ---------------------------------------------------------------------------


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def test_hash_sections_coverage_parity_with_extract_sections() -> None:
    text = (
        "<!-- my-setup:user-section start shared a -->\n"
        "alpha\n"
        "<!-- my-setup:user-section end shared a -->\n"
        "<!-- my-setup:user-section start shared b -->\n"
        "beta\n"
        "<!-- my-setup:user-section end shared b -->\n"
    )
    assert hash_sections(text).keys() == extract_sections(text).keys()


def test_hash_sections_identical_content_identical_hash() -> None:
    body = "shared body\n"
    t1 = (
        "<!-- my-setup:user-section start shared a -->\n"
        f"{body}"
        "<!-- my-setup:user-section end shared a -->\n"
    )
    t2 = (
        "different surrounding text\n"
        "<!-- my-setup:user-section start shared a -->\n"
        f"{body}"
        "<!-- my-setup:user-section end shared a -->\n"
        "more surrounding\n"
    )
    assert hash_sections(t1)["a"] == hash_sections(t2)["a"]


def test_hash_sections_differing_content_differing_hash() -> None:
    t1 = (
        "<!-- my-setup:user-section start shared a -->\n"
        "v1\n"
        "<!-- my-setup:user-section end shared a -->\n"
    )
    t2 = (
        "<!-- my-setup:user-section start shared a -->\n"
        "v2\n"
        "<!-- my-setup:user-section end shared a -->\n"
    )
    assert hash_sections(t1)["a"] != hash_sections(t2)["a"]


def test_hash_sections_hex_digest_shape_64_lowercase() -> None:
    text = (
        "<!-- my-setup:user-section start shared a -->\n"
        "body\n"
        "<!-- my-setup:user-section end shared a -->\n"
    )
    digest = hash_sections(text)["a"]
    assert len(digest) == 64
    assert digest == digest.lower()
    assert all(c in "0123456789abcdef" for c in digest)


def test_hash_sections_composition_invariant() -> None:
    """hash_sections(t)[n] == sha256(extract_sections(t)[n]).hexdigest()."""
    text = (
        "<!-- my-setup:user-section start shared a -->\n"
        "alpha\n"
        "<!-- my-setup:user-section end shared a -->\n"
    )
    body = extract_sections(text)["a"]
    assert hash_sections(text)["a"] == _sha256_hex(body)


def test_hash_sections_unnamed_keying_mirrors_extract_sections() -> None:
    text = (
        "<!-- my-setup:user-section start host-local -->\n"
        "first\n"
        "<!-- my-setup:user-section end host-local -->\n"
        "<!-- my-setup:user-section start host-local -->\n"
        "second\n"
        "<!-- my-setup:user-section end host-local -->\n"
    )
    assert set(hash_sections(text).keys()) == {"0", "1"}


def test_hash_sections_propagates_marker_error() -> None:
    text = "<!-- my-setup:user-section end shared a -->\n"
    with pytest.raises(MarkerError, match="without matching start"):
        hash_sections(text)


# ---------------------------------------------------------------------------
# dotfiles-xyw — extract_marker_hashes
# ---------------------------------------------------------------------------


def test_extract_marker_hashes_returns_hash_when_present() -> None:
    text = (
        "<!-- my-setup:user-section start shared a -->\n"
        "body\n"
        f"<!-- my-setup:user-section end shared a hash={_HASH_HEX_64} -->\n"
    )
    assert extract_marker_hashes(text) == {"a": _HASH_HEX_64}


def test_extract_marker_hashes_returns_none_for_legacy_hashless() -> None:
    text = (
        "<!-- my-setup:user-section start shared a -->\n"
        "body\n"
        "<!-- my-setup:user-section end shared a -->\n"
    )
    assert extract_marker_hashes(text) == {"a": None}


def test_extract_marker_hashes_mixed_file() -> None:
    text = (
        "<!-- my-setup:user-section start shared a -->\n"
        "alpha\n"
        f"<!-- my-setup:user-section end shared a hash={_HASH_HEX_64} -->\n"
        "<!-- my-setup:user-section start shared b -->\n"
        "beta\n"
        "<!-- my-setup:user-section end shared b -->\n"
    )
    assert extract_marker_hashes(text) == {"a": _HASH_HEX_64, "b": None}


def test_extract_marker_hashes_coverage_parity_with_extract_sections() -> None:
    text = (
        "<!-- my-setup:user-section start shared a -->\n"
        "alpha\n"
        "<!-- my-setup:user-section end shared a -->\n"
        "<!-- my-setup:user-section start shared b -->\n"
        "beta\n"
        f"<!-- my-setup:user-section end shared b hash={_HASH_HEX_64} -->\n"
    )
    assert extract_marker_hashes(text).keys() == extract_sections(text).keys()


def test_extract_marker_hashes_propagates_marker_error() -> None:
    text = "<!-- my-setup:user-section start shared a -->\nbody\n"
    with pytest.raises(MarkerError, match="unclosed"):
        extract_marker_hashes(text)


# ---------------------------------------------------------------------------
# dotfiles-xyw — set_marker_hashes
# ---------------------------------------------------------------------------


def test_set_marker_hashes_adds_hash_to_hashless_end_marker() -> None:
    text = (
        "<!-- my-setup:user-section start shared a -->\n"
        "body\n"
        "<!-- my-setup:user-section end shared a -->\n"
    )
    digest = _sha256_hex("body\n")
    result = set_marker_hashes(text, {"a": digest})
    assert f"hash={digest}" in result
    assert extract_marker_hashes(result) == {"a": digest}


def test_set_marker_hashes_replaces_existing_hash() -> None:
    old = _HASH_HEX_64
    new = "b" * 64
    text = (
        "<!-- my-setup:user-section start shared a -->\n"
        "body\n"
        f"<!-- my-setup:user-section end shared a hash={old} -->\n"
    )
    result = set_marker_hashes(text, {"a": new})
    assert f"hash={new}" in result
    assert f"hash={old}" not in result
    assert extract_marker_hashes(result) == {"a": new}


def test_set_marker_hashes_strips_when_absent_from_dict() -> None:
    """Sections present in text but absent from hashes dict have their
    hash= segment removed."""
    text = (
        "<!-- my-setup:user-section start shared a -->\n"
        "body\n"
        f"<!-- my-setup:user-section end shared a hash={_HASH_HEX_64} -->\n"
    )
    result = set_marker_hashes(text, {})
    assert "hash=" not in result
    assert extract_marker_hashes(result) == {"a": None}


def test_set_marker_hashes_byte_preserving_outside_markers() -> None:
    body = "user body line 1\nuser body line 2\n"
    text = (
        "preamble line 1\n"
        "preamble line 2\n"
        "<!-- my-setup:user-section start shared a -->\n"
        f"{body}"
        "<!-- my-setup:user-section end shared a -->\n"
        "epilogue\n"
    )
    result = set_marker_hashes(text, {"a": _sha256_hex(body)})
    assert result.startswith("preamble line 1\npreamble line 2\n")
    assert body in result
    assert result.endswith("epilogue\n")


def test_set_marker_hashes_round_trip_with_extract() -> None:
    text = (
        "<!-- my-setup:user-section start shared a -->\n"
        "alpha\n"
        "<!-- my-setup:user-section end shared a -->\n"
        "<!-- my-setup:user-section start shared b -->\n"
        "beta\n"
        "<!-- my-setup:user-section end shared b -->\n"
    )
    hashes = {"a": _HASH_HEX_64, "b": "f" * 64}
    assert extract_marker_hashes(set_marker_hashes(text, hashes)) == hashes


def test_set_marker_hashes_round_trip_with_hash_sections() -> None:
    text = (
        "<!-- my-setup:user-section start shared a -->\n"
        "alpha\n"
        "<!-- my-setup:user-section end shared a -->\n"
        "<!-- my-setup:user-section start shared b -->\n"
        "beta\n"
        "<!-- my-setup:user-section end shared b -->\n"
    )
    hashes = hash_sections(text)
    assert extract_marker_hashes(set_marker_hashes(text, hashes)) == hashes


def test_set_marker_hashes_bad_key_raises_value_error() -> None:
    text = (
        "<!-- my-setup:user-section start shared a -->\n"
        "body\n"
        "<!-- my-setup:user-section end shared a -->\n"
    )
    with pytest.raises(ValueError, match="nonexistent"):
        set_marker_hashes(text, {"nonexistent": _HASH_HEX_64})


def test_set_marker_hashes_unnamed_section_by_index() -> None:
    """Unnamed sections key by '0', '1', ... — set_marker_hashes accepts those."""
    text = (
        "<!-- my-setup:user-section start host-local -->\n"
        "body\n"
        "<!-- my-setup:user-section end host-local -->\n"
    )
    digest = _sha256_hex("body\n")
    result = set_marker_hashes(text, {"0": digest})
    assert f"hash={digest}" in result
    assert extract_marker_hashes(result) == {"0": digest}


def test_set_marker_hashes_propagates_marker_error() -> None:
    text = "<!-- my-setup:user-section end shared a -->\n"
    with pytest.raises(MarkerError, match="without matching start"):
        set_marker_hashes(text, {})


def test_extract_marker_hashes_extracted_form_matches_writer() -> None:
    """The hash extract_marker_hashes returns is exactly what
    set_marker_hashes wrote."""
    base = (
        "<!-- my-setup:user-section start shared a -->\n"
        "body\n"
        "<!-- my-setup:user-section end shared a -->\n"
    )
    hashes = hash_sections(base)
    written = set_marker_hashes(base, hashes)
    assert extract_marker_hashes(written) == hashes


# ---------------------------------------------------------------------------
# dotfiles-9by — required host-local|shared keyword
# ---------------------------------------------------------------------------


def test_untagged_start_marker_raises_marker_error() -> None:
    text = (
        "<!-- my-setup:user-section start workflow -->\n"
        "body\n"
        "<!-- my-setup:user-section end shared workflow -->\n"
    )
    with pytest.raises(MarkerError, match="missing required"):
        extract_sections(text)


def test_untagged_end_marker_raises_marker_error() -> None:
    text = (
        "<!-- my-setup:user-section start shared workflow -->\n"
        "body\n"
        "<!-- my-setup:user-section end workflow -->\n"
    )
    with pytest.raises(MarkerError, match="missing required"):
        extract_sections(text)


def test_untagged_unnamed_start_raises_marker_error() -> None:
    text = (
        "<!-- my-setup:user-section start -->\n"
        "body\n"
        "<!-- my-setup:user-section end -->\n"
    )
    with pytest.raises(MarkerError, match="missing required"):
        extract_sections(text)


def test_semantics_mismatch_raises_marker_error() -> None:
    text = (
        "<!-- my-setup:user-section start shared workflow -->\n"
        "body\n"
        "<!-- my-setup:user-section end host-local workflow -->\n"
    )
    with pytest.raises(MarkerError, match="end semantics"):
        extract_sections(text)


def test_unknown_semantics_keyword_is_not_recognised_as_marker() -> None:
    """A token that is neither 'host-local' nor 'shared' makes the start
    marker fail to match the regex entirely (extra tokens before the
    closing ``-->``); the start line is then treated as outside-section
    text, so the subsequent end raises end-without-start."""
    text = (
        "<!-- my-setup:user-section start unknown workflow -->\n"
        "body\n"
        "<!-- my-setup:user-section end shared workflow -->\n"
    )
    with pytest.raises(MarkerError, match="without matching start"):
        extract_sections(text)


def test_extract_sections_accepts_host_local_keyword() -> None:
    text = (
        "<!-- my-setup:user-section start host-local notes -->\n"
        "host-local body\n"
        "<!-- my-setup:user-section end host-local notes -->\n"
    )
    assert extract_sections(text) == {"notes": "host-local body\n"}


def test_extract_sections_accepts_shared_keyword() -> None:
    text = (
        "<!-- my-setup:user-section start shared workflow -->\n"
        "shared body\n"
        "<!-- my-setup:user-section end shared workflow -->\n"
    )
    assert extract_sections(text) == {"workflow": "shared body\n"}


def test_end_marker_with_keyword_and_hash_parses() -> None:
    text = (
        "<!-- my-setup:user-section start shared workflow -->\n"
        "body\n"
        f"<!-- my-setup:user-section end shared workflow hash={_HASH_HEX_64} -->\n"
    )
    assert extract_sections(text) == {"workflow": "body\n"}
    assert extract_marker_hashes(text) == {"workflow": _HASH_HEX_64}


def test_section_semantics_returns_keyword_per_section() -> None:
    text = (
        "<!-- my-setup:user-section start shared workflow -->\n"
        "wf\n"
        "<!-- my-setup:user-section end shared workflow -->\n"
        "<!-- my-setup:user-section start host-local notes -->\n"
        "notes\n"
        "<!-- my-setup:user-section end host-local notes -->\n"
    )
    assert section_semantics(text) == {
        "workflow": "shared",
        "notes": "host-local",
    }


def test_section_semantics_coverage_parity_with_extract_sections() -> None:
    text = (
        "<!-- my-setup:user-section start shared a -->\n"
        "alpha\n"
        "<!-- my-setup:user-section end shared a -->\n"
        "<!-- my-setup:user-section start host-local b -->\n"
        "beta\n"
        "<!-- my-setup:user-section end host-local b -->\n"
    )
    assert section_semantics(text).keys() == extract_sections(text).keys()


def test_section_semantics_unnamed_keying_mirrors_extract_sections() -> None:
    text = (
        "<!-- my-setup:user-section start host-local -->\n"
        "first\n"
        "<!-- my-setup:user-section end host-local -->\n"
        "<!-- my-setup:user-section start shared -->\n"
        "second\n"
        "<!-- my-setup:user-section end shared -->\n"
    )
    assert section_semantics(text) == {"0": "host-local", "1": "shared"}


def test_section_semantics_propagates_marker_error() -> None:
    text = "<!-- my-setup:user-section start workflow -->\nbody\n"
    with pytest.raises(MarkerError):
        section_semantics(text)


def test_set_marker_hashes_preserves_semantics_keyword() -> None:
    """Rewriting an end-marker hash keeps the host-local|shared keyword."""
    text = (
        "<!-- my-setup:user-section start host-local notes -->\n"
        "body\n"
        "<!-- my-setup:user-section end host-local notes -->\n"
    )
    digest = _sha256_hex("body\n")
    result = set_marker_hashes(text, {"notes": digest})
    assert "end host-local notes" in result
    assert f"hash={digest}" in result


def test_set_marker_hashes_preserves_shared_keyword() -> None:
    text = (
        "<!-- my-setup:user-section start shared workflow -->\n"
        "body\n"
        "<!-- my-setup:user-section end shared workflow -->\n"
    )
    digest = _sha256_hex("body\n")
    result = set_marker_hashes(text, {"workflow": digest})
    assert "end shared workflow" in result
    assert f"hash={digest}" in result


def test_section_semantics_value_is_canonical_string() -> None:
    """Values are :class:`SectionSemantics` members; since it is a StrEnum,
    they compare equal to and are instances of ``str``."""
    text = (
        "<!-- my-setup:user-section start shared workflow -->\n"
        "wf\n"
        "<!-- my-setup:user-section end shared workflow -->\n"
    )
    value = section_semantics(text)["workflow"]
    assert value is SectionSemantics.SHARED
    assert isinstance(value, str)  # StrEnum is-a str
    assert value == "shared"
