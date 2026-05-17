"""Tests for user-section marker parsing and merging."""

import hashlib
import logging
from pathlib import Path

import pytest

from setforge.errors import MarkerError
from setforge.sections import (
    SectionSemantics,
    detect_legacy_markers,
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
        "<!-- setforge:user-section start host-local -->\n"
        "preserved 1\npreserved 2\n"
        "<!-- setforge:user-section end host-local -->\n"
        "after\n"
    )
    assert extract_sections(text, allow_legacy=True) == {
        "0": "preserved 1\npreserved 2\n"
    }


def test_single_unnamed_section_merge_round_trip() -> None:
    tracked = (
        "before\n"
        "<!-- setforge:user-section start host-local -->\n"
        f"<!-- setforge:user-section end host-local hash={'a' * 64} -->\n"
        "after\n"
    )
    live_text = (
        "before\n"
        "<!-- setforge:user-section start host-local -->\n"
        "user content\n"
        "<!-- setforge:user-section end host-local -->\n"
        "after\n"
    )
    live_sections = extract_sections(live_text, allow_legacy=True)
    merged = merge_sections(tracked, live_sections)
    assert "user content\n" in merged
    assert merged.startswith("before\n")
    assert merged.endswith("after\n")


def test_two_named_sections_independent() -> None:
    tracked = (
        "<!-- setforge:user-section start shared workflow -->\n"
        f"<!-- setforge:user-section end shared workflow hash={'a' * 64} -->\n"
        "between\n"
        "<!-- setforge:user-section start shared commits -->\n"
        f"<!-- setforge:user-section end shared commits hash={'b' * 64} -->\n"
    )
    live_sections = {"workflow": "wf content\n", "commits": "cm content\n"}
    merged = merge_sections(tracked, live_sections)
    assert "wf content" in merged
    assert "cm content" in merged
    assert merged.index("wf content") < merged.index("between")
    assert merged.index("between") < merged.index("cm content")


def test_extract_named_sections_keyed_by_name() -> None:
    text = (
        "<!-- setforge:user-section start shared workflow -->\n"
        "wf\n"
        "<!-- setforge:user-section end shared workflow -->\n"
        "<!-- setforge:user-section start shared commits -->\n"
        "cm\n"
        "<!-- setforge:user-section end shared commits -->\n"
    )
    assert extract_sections(text, allow_legacy=True) == {
        "workflow": "wf\n",
        "commits": "cm\n",
    }


def test_mismatched_missing_end_raises() -> None:
    text = "<!-- setforge:user-section start host-local -->\ncontent\n"
    with pytest.raises(MarkerError, match="unclosed"):
        extract_sections(text)


def test_end_without_start_raises() -> None:
    text = "<!-- setforge:user-section end host-local -->\n"
    with pytest.raises(MarkerError, match="without matching start"):
        extract_sections(text)


def test_name_mismatch_raises() -> None:
    text = (
        "<!-- setforge:user-section start shared workflow -->\n"
        "<!-- setforge:user-section end shared commits -->\n"
    )
    with pytest.raises(MarkerError, match="does not match"):
        extract_sections(text)


def test_nested_section_raises() -> None:
    text = (
        "<!-- setforge:user-section start shared outer -->\n"
        "<!-- setforge:user-section start shared inner -->\n"
        "<!-- setforge:user-section end shared inner -->\n"
        "<!-- setforge:user-section end shared outer -->\n"
    )
    with pytest.raises(MarkerError, match="nested"):
        extract_sections(text)


def test_live_extra_section_warns_and_drops(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tracked = (
        "<!-- setforge:user-section start shared workflow -->\n"
        f"<!-- setforge:user-section end shared workflow hash={'a' * 64} -->\n"
    )
    live = {"workflow": "wf\n", "extra": "extra\n"}
    with caplog.at_level(logging.WARNING):
        merged = merge_sections(tracked, live)
    assert "extra" not in merged
    assert any("extra" in rec.getMessage() for rec in caplog.records)


def test_tracked_section_absent_from_live_keeps_placeholder() -> None:
    tracked = (
        "<!-- setforge:user-section start shared workflow -->\n"
        "placeholder text\n"
        f"<!-- setforge:user-section end shared workflow hash={'a' * 64} -->\n"
    )
    merged = merge_sections(tracked, {})
    assert "placeholder text" in merged


def test_extract_unnamed_indices_in_order() -> None:
    text = (
        "<!-- setforge:user-section start host-local -->\n"
        "first\n"
        "<!-- setforge:user-section end host-local -->\n"
        "<!-- setforge:user-section start host-local -->\n"
        "second\n"
        "<!-- setforge:user-section end host-local -->\n"
    )
    assert extract_sections(text, allow_legacy=True) == {
        "0": "first\n",
        "1": "second\n",
    }


# ---------------------------------------------------------------------------
# setforge-xyw — marker regex extension: optional hash= segment on end markers
# ---------------------------------------------------------------------------

_HASH_HEX_64: str = "a" * 64


def test_extract_sections_parses_end_marker_with_hash() -> None:
    """End marker with hash= segment parses identically to one without —
    name and body unchanged."""
    text = (
        "<!-- setforge:user-section start shared a -->\n"
        "body\n"
        f"<!-- setforge:user-section end shared a hash={_HASH_HEX_64} -->\n"
    )
    assert extract_sections(text) == {"a": "body\n"}


def test_extract_sections_parses_unnamed_end_marker_with_hash() -> None:
    """An unnamed end marker carrying only a hash= segment still parses."""
    text = (
        "<!-- setforge:user-section start host-local -->\n"
        "body\n"
        f"<!-- setforge:user-section end host-local hash={_HASH_HEX_64} -->\n"
    )
    assert extract_sections(text) == {"0": "body\n"}


def test_extract_sections_hashless_end_marker_under_allow_legacy_parses() -> None:
    """Hashless end markers parse under the migration-only ``allow_legacy``
    escape hatch (9ln); strict default rejects them."""
    text = (
        "<!-- setforge:user-section start shared a -->\n"
        "body\n"
        "<!-- setforge:user-section end shared a -->\n"
    )
    assert extract_sections(text, allow_legacy=True) == {"a": "body\n"}


# ---------------------------------------------------------------------------
# setforge-xyw — hash_sections primitive
# ---------------------------------------------------------------------------


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def test_hash_sections_coverage_parity_with_extract_sections() -> None:
    text = (
        "<!-- setforge:user-section start shared a -->\n"
        "alpha\n"
        "<!-- setforge:user-section end shared a -->\n"
        "<!-- setforge:user-section start shared b -->\n"
        "beta\n"
        "<!-- setforge:user-section end shared b -->\n"
    )
    assert (
        hash_sections(text, allow_legacy=True).keys()
        == extract_sections(text, allow_legacy=True).keys()
    )


def test_hash_sections_identical_content_identical_hash() -> None:
    body = "shared body\n"
    t1 = (
        "<!-- setforge:user-section start shared a -->\n"
        f"{body}"
        "<!-- setforge:user-section end shared a -->\n"
    )
    t2 = (
        "different surrounding text\n"
        "<!-- setforge:user-section start shared a -->\n"
        f"{body}"
        "<!-- setforge:user-section end shared a -->\n"
        "more surrounding\n"
    )
    assert (
        hash_sections(t1, allow_legacy=True)["a"]
        == hash_sections(t2, allow_legacy=True)["a"]
    )


def test_hash_sections_differing_content_differing_hash() -> None:
    t1 = (
        "<!-- setforge:user-section start shared a -->\n"
        "v1\n"
        "<!-- setforge:user-section end shared a -->\n"
    )
    t2 = (
        "<!-- setforge:user-section start shared a -->\n"
        "v2\n"
        "<!-- setforge:user-section end shared a -->\n"
    )
    assert (
        hash_sections(t1, allow_legacy=True)["a"]
        != hash_sections(t2, allow_legacy=True)["a"]
    )


def test_hash_sections_hex_digest_shape_64_lowercase() -> None:
    text = (
        "<!-- setforge:user-section start shared a -->\n"
        "body\n"
        "<!-- setforge:user-section end shared a -->\n"
    )
    digest = hash_sections(text, allow_legacy=True)["a"]
    assert len(digest) == 64
    assert digest == digest.lower()
    assert all(c in "0123456789abcdef" for c in digest)


def test_hash_sections_composition_invariant() -> None:
    """hash_sections(t)[n] == sha256(extract_sections(t)[n]).hexdigest()."""
    text = (
        "<!-- setforge:user-section start shared a -->\n"
        "alpha\n"
        "<!-- setforge:user-section end shared a -->\n"
    )
    body = extract_sections(text, allow_legacy=True)["a"]
    assert hash_sections(text, allow_legacy=True)["a"] == _sha256_hex(body)


def test_hash_sections_unnamed_keying_mirrors_extract_sections() -> None:
    text = (
        "<!-- setforge:user-section start host-local -->\n"
        "first\n"
        "<!-- setforge:user-section end host-local -->\n"
        "<!-- setforge:user-section start host-local -->\n"
        "second\n"
        "<!-- setforge:user-section end host-local -->\n"
    )
    assert set(hash_sections(text, allow_legacy=True).keys()) == {"0", "1"}


def test_hash_sections_propagates_marker_error() -> None:
    text = "<!-- setforge:user-section end shared a -->\n"
    with pytest.raises(MarkerError, match="without matching start"):
        hash_sections(text)


# ---------------------------------------------------------------------------
# setforge-xyw — extract_marker_hashes
# ---------------------------------------------------------------------------


def test_extract_marker_hashes_returns_hash_when_present() -> None:
    text = (
        "<!-- setforge:user-section start shared a -->\n"
        "body\n"
        f"<!-- setforge:user-section end shared a hash={_HASH_HEX_64} -->\n"
    )
    assert extract_marker_hashes(text) == {"a": _HASH_HEX_64}


def test_extract_marker_hashes_returns_none_for_legacy_hashless() -> None:
    text = (
        "<!-- setforge:user-section start shared a -->\n"
        "body\n"
        "<!-- setforge:user-section end shared a -->\n"
    )
    assert extract_marker_hashes(text, allow_legacy=True) == {"a": None}


def test_extract_marker_hashes_mixed_file() -> None:
    text = (
        "<!-- setforge:user-section start shared a -->\n"
        "alpha\n"
        f"<!-- setforge:user-section end shared a hash={_HASH_HEX_64} -->\n"
        "<!-- setforge:user-section start shared b -->\n"
        "beta\n"
        "<!-- setforge:user-section end shared b -->\n"
    )
    assert extract_marker_hashes(text, allow_legacy=True) == {
        "a": _HASH_HEX_64,
        "b": None,
    }


def test_extract_marker_hashes_coverage_parity_with_extract_sections() -> None:
    text = (
        "<!-- setforge:user-section start shared a -->\n"
        "alpha\n"
        "<!-- setforge:user-section end shared a -->\n"
        "<!-- setforge:user-section start shared b -->\n"
        "beta\n"
        f"<!-- setforge:user-section end shared b hash={_HASH_HEX_64} -->\n"
    )
    assert (
        extract_marker_hashes(text, allow_legacy=True).keys()
        == extract_sections(text, allow_legacy=True).keys()
    )


def test_extract_marker_hashes_propagates_marker_error() -> None:
    text = "<!-- setforge:user-section start shared a -->\nbody\n"
    with pytest.raises(MarkerError, match="unclosed"):
        extract_marker_hashes(text)


# ---------------------------------------------------------------------------
# setforge-xyw — set_marker_hashes
# ---------------------------------------------------------------------------


def test_set_marker_hashes_adds_hash_to_hashless_end_marker() -> None:
    text = (
        "<!-- setforge:user-section start shared a -->\n"
        "body\n"
        "<!-- setforge:user-section end shared a -->\n"
    )
    digest = _sha256_hex("body\n")
    result = set_marker_hashes(text, {"a": digest}, allow_legacy=True)
    assert f"hash={digest}" in result
    assert extract_marker_hashes(result) == {"a": digest}


def test_set_marker_hashes_replaces_existing_hash() -> None:
    old = _HASH_HEX_64
    new = "b" * 64
    text = (
        "<!-- setforge:user-section start shared a -->\n"
        "body\n"
        f"<!-- setforge:user-section end shared a hash={old} -->\n"
    )
    result = set_marker_hashes(text, {"a": new})
    assert f"hash={new}" in result
    assert f"hash={old}" not in result
    assert extract_marker_hashes(result) == {"a": new}


def test_set_marker_hashes_strips_when_absent_from_dict() -> None:
    """Sections present in text but absent from hashes dict have their
    hash= segment removed."""
    text = (
        "<!-- setforge:user-section start shared a -->\n"
        "body\n"
        f"<!-- setforge:user-section end shared a hash={_HASH_HEX_64} -->\n"
    )
    result = set_marker_hashes(text, {})
    assert "hash=" not in result
    # Stripped output has no hash segment — re-parse needs allow_legacy=True
    # under the post-9ln strict parser.
    assert extract_marker_hashes(result, allow_legacy=True) == {"a": None}


def test_set_marker_hashes_byte_preserving_outside_markers() -> None:
    body = "user body line 1\nuser body line 2\n"
    text = (
        "preamble line 1\n"
        "preamble line 2\n"
        "<!-- setforge:user-section start shared a -->\n"
        f"{body}"
        "<!-- setforge:user-section end shared a -->\n"
        "epilogue\n"
    )
    result = set_marker_hashes(text, {"a": _sha256_hex(body)}, allow_legacy=True)
    assert result.startswith("preamble line 1\npreamble line 2\n")
    assert body in result
    assert result.endswith("epilogue\n")


def test_set_marker_hashes_round_trip_with_extract() -> None:
    text = (
        "<!-- setforge:user-section start shared a -->\n"
        "alpha\n"
        "<!-- setforge:user-section end shared a -->\n"
        "<!-- setforge:user-section start shared b -->\n"
        "beta\n"
        "<!-- setforge:user-section end shared b -->\n"
    )
    hashes = {"a": _HASH_HEX_64, "b": "f" * 64}
    assert (
        extract_marker_hashes(set_marker_hashes(text, hashes, allow_legacy=True))
        == hashes
    )


def test_set_marker_hashes_round_trip_with_hash_sections() -> None:
    text = (
        "<!-- setforge:user-section start shared a -->\n"
        "alpha\n"
        "<!-- setforge:user-section end shared a -->\n"
        "<!-- setforge:user-section start shared b -->\n"
        "beta\n"
        "<!-- setforge:user-section end shared b -->\n"
    )
    hashes = hash_sections(text, allow_legacy=True)
    assert (
        extract_marker_hashes(set_marker_hashes(text, hashes, allow_legacy=True))
        == hashes
    )


def test_set_marker_hashes_bad_key_raises_value_error() -> None:
    text = (
        "<!-- setforge:user-section start shared a -->\n"
        "body\n"
        "<!-- setforge:user-section end shared a -->\n"
    )
    with pytest.raises(ValueError, match="nonexistent"):
        set_marker_hashes(text, {"nonexistent": _HASH_HEX_64}, allow_legacy=True)


def test_set_marker_hashes_unnamed_section_by_index() -> None:
    """Unnamed sections key by '0', '1', ... — set_marker_hashes accepts those."""
    text = (
        "<!-- setforge:user-section start host-local -->\n"
        "body\n"
        "<!-- setforge:user-section end host-local -->\n"
    )
    digest = _sha256_hex("body\n")
    result = set_marker_hashes(text, {"0": digest}, allow_legacy=True)
    assert f"hash={digest}" in result
    assert extract_marker_hashes(result) == {"0": digest}


def test_set_marker_hashes_propagates_marker_error() -> None:
    text = "<!-- setforge:user-section end shared a -->\n"
    with pytest.raises(MarkerError, match="without matching start"):
        set_marker_hashes(text, {})


def test_extract_marker_hashes_extracted_form_matches_writer() -> None:
    """The hash extract_marker_hashes returns is exactly what
    set_marker_hashes wrote."""
    base = (
        "<!-- setforge:user-section start shared a -->\n"
        "body\n"
        "<!-- setforge:user-section end shared a -->\n"
    )
    hashes = hash_sections(base, allow_legacy=True)
    written = set_marker_hashes(base, hashes, allow_legacy=True)
    assert extract_marker_hashes(written) == hashes


# ---------------------------------------------------------------------------
# setforge-9by — required host-local|shared keyword
# ---------------------------------------------------------------------------


def test_untagged_start_marker_raises_marker_error() -> None:
    text = (
        "<!-- setforge:user-section start workflow -->\n"
        "body\n"
        "<!-- setforge:user-section end shared workflow -->\n"
    )
    with pytest.raises(MarkerError, match="missing required"):
        extract_sections(text)


def test_untagged_end_marker_raises_marker_error() -> None:
    text = (
        "<!-- setforge:user-section start shared workflow -->\n"
        "body\n"
        "<!-- setforge:user-section end workflow -->\n"
    )
    with pytest.raises(MarkerError, match="missing required"):
        extract_sections(text)


def test_untagged_unnamed_start_raises_marker_error() -> None:
    text = (
        "<!-- setforge:user-section start -->\n"
        "body\n"
        "<!-- setforge:user-section end -->\n"
    )
    with pytest.raises(MarkerError, match="missing required"):
        extract_sections(text)


def test_semantics_mismatch_raises_marker_error() -> None:
    text = (
        "<!-- setforge:user-section start shared workflow -->\n"
        "body\n"
        "<!-- setforge:user-section end host-local workflow -->\n"
    )
    with pytest.raises(MarkerError, match="end semantics"):
        extract_sections(text)


def test_unknown_semantics_keyword_raises_at_parse_time() -> None:
    """A token that is neither 'host-local' nor 'shared' on a start marker
    surfaces a precise :class:`MarkerError` with line context naming the
    bad keyword — rather than silently falling through and producing an
    opaque downstream 'end-without-start' from the subsequent end marker.
    """
    text = (
        "<!-- setforge:user-section start unknown workflow -->\n"
        "body\n"
        "<!-- setforge:user-section end shared workflow -->\n"
    )
    with pytest.raises(MarkerError, match="unknown semantics keyword 'unknown'"):
        extract_sections(text)


def test_extract_sections_accepts_host_local_keyword() -> None:
    text = (
        "<!-- setforge:user-section start host-local notes -->\n"
        "host-local body\n"
        f"<!-- setforge:user-section end host-local notes hash={_HASH_HEX_64} -->\n"
    )
    assert extract_sections(text) == {"notes": "host-local body\n"}


def test_extract_sections_accepts_shared_keyword() -> None:
    text = (
        "<!-- setforge:user-section start shared workflow -->\n"
        "shared body\n"
        f"<!-- setforge:user-section end shared workflow hash={_HASH_HEX_64} -->\n"
    )
    assert extract_sections(text) == {"workflow": "shared body\n"}


def test_end_marker_with_keyword_and_hash_parses() -> None:
    text = (
        "<!-- setforge:user-section start shared workflow -->\n"
        "body\n"
        f"<!-- setforge:user-section end shared workflow hash={_HASH_HEX_64} -->\n"
    )
    assert extract_sections(text) == {"workflow": "body\n"}
    assert extract_marker_hashes(text) == {"workflow": _HASH_HEX_64}


def test_section_semantics_returns_keyword_per_section() -> None:
    text = (
        "<!-- setforge:user-section start shared workflow -->\n"
        "wf\n"
        "<!-- setforge:user-section end shared workflow -->\n"
        "<!-- setforge:user-section start host-local notes -->\n"
        "notes\n"
        "<!-- setforge:user-section end host-local notes -->\n"
    )
    assert section_semantics(text, allow_legacy=True) == {
        "workflow": "shared",
        "notes": "host-local",
    }


def test_section_semantics_coverage_parity_with_extract_sections() -> None:
    text = (
        "<!-- setforge:user-section start shared a -->\n"
        "alpha\n"
        "<!-- setforge:user-section end shared a -->\n"
        "<!-- setforge:user-section start host-local b -->\n"
        "beta\n"
        "<!-- setforge:user-section end host-local b -->\n"
    )
    assert (
        section_semantics(text, allow_legacy=True).keys()
        == extract_sections(text, allow_legacy=True).keys()
    )


def test_section_semantics_unnamed_keying_mirrors_extract_sections() -> None:
    text = (
        "<!-- setforge:user-section start host-local -->\n"
        "first\n"
        "<!-- setforge:user-section end host-local -->\n"
        "<!-- setforge:user-section start shared -->\n"
        "second\n"
        "<!-- setforge:user-section end shared -->\n"
    )
    assert section_semantics(text, allow_legacy=True) == {
        "0": "host-local",
        "1": "shared",
    }


def test_section_semantics_propagates_marker_error() -> None:
    text = "<!-- setforge:user-section start workflow -->\nbody\n"
    with pytest.raises(MarkerError):
        section_semantics(text)


def test_set_marker_hashes_preserves_semantics_keyword() -> None:
    """Rewriting an end-marker hash keeps the host-local|shared keyword."""
    text = (
        "<!-- setforge:user-section start host-local notes -->\n"
        "body\n"
        "<!-- setforge:user-section end host-local notes -->\n"
    )
    digest = _sha256_hex("body\n")
    result = set_marker_hashes(text, {"notes": digest}, allow_legacy=True)
    assert "end host-local notes" in result
    assert f"hash={digest}" in result


def test_set_marker_hashes_preserves_shared_keyword() -> None:
    text = (
        "<!-- setforge:user-section start shared workflow -->\n"
        "body\n"
        "<!-- setforge:user-section end shared workflow -->\n"
    )
    digest = _sha256_hex("body\n")
    result = set_marker_hashes(text, {"workflow": digest}, allow_legacy=True)
    assert "end shared workflow" in result
    assert f"hash={digest}" in result


def test_section_semantics_value_is_canonical_string() -> None:
    """Values are :class:`SectionSemantics` members; since it is a StrEnum,
    they compare equal to and are instances of ``str``."""
    text = (
        "<!-- setforge:user-section start shared workflow -->\n"
        "wf\n"
        "<!-- setforge:user-section end shared workflow -->\n"
    )
    value = section_semantics(text, allow_legacy=True)["workflow"]
    assert value is SectionSemantics.SHARED
    assert isinstance(value, str)  # StrEnum is-a str
    assert value == "shared"


# ---------------------------------------------------------------------------
# setforge-9ln — strict parser + allow_legacy migration mode
# ---------------------------------------------------------------------------


_LEGACY_UNTAGGED_TEXT: str = (
    "<!-- setforge:user-section start workflow -->\n"
    "rule 1\nrule 2\n"
    "<!-- setforge:user-section end workflow -->\n"
)


_LEGACY_TAGGED_HASHLESS_TEXT: str = (
    "<!-- setforge:user-section start shared workflow -->\n"
    "rule 1\nrule 2\n"
    "<!-- setforge:user-section end shared workflow -->\n"
)


def test_walk_markers_allow_legacy_yields_shared_and_none_hash() -> None:
    """Untagged + hashless markers under ``allow_legacy=True``: semantics
    parses as SHARED, embedded hash yields None."""
    result = extract_sections(_LEGACY_UNTAGGED_TEXT, allow_legacy=True)
    assert result == {"workflow": "rule 1\nrule 2\n"}
    semantics = section_semantics(_LEGACY_UNTAGGED_TEXT, allow_legacy=True)
    assert semantics == {"workflow": SectionSemantics.SHARED}
    hashes = extract_marker_hashes(_LEGACY_UNTAGGED_TEXT, allow_legacy=True)
    assert hashes == {"workflow": None}


def test_walk_markers_strict_default_rejects_missing_semantics() -> None:
    """Untagged markers + strict (default) → MarkerError (preserved 9by
    behavior)."""
    with pytest.raises(MarkerError, match="missing required"):
        extract_sections(_LEGACY_UNTAGGED_TEXT)


def test_walk_markers_strict_default_rejects_missing_hash() -> None:
    """Tagged markers without hash= + strict (default) → MarkerError on
    the end marker. NEW strictness from 9ln."""
    with pytest.raises(MarkerError, match="missing required 'hash="):
        extract_sections(_LEGACY_TAGGED_HASHLESS_TEXT)


def test_walk_markers_allow_legacy_tolerates_missing_hash() -> None:
    """Tagged markers without hash= + ``allow_legacy=True`` parse;
    embedded hash is None."""
    assert extract_sections(_LEGACY_TAGGED_HASHLESS_TEXT, allow_legacy=True) == {
        "workflow": "rule 1\nrule 2\n",
    }
    assert extract_marker_hashes(_LEGACY_TAGGED_HASHLESS_TEXT, allow_legacy=True) == {
        "workflow": None
    }


def test_extract_sections_legacy_path() -> None:
    """``extract_sections`` plumbs ``allow_legacy`` through to the walker."""
    assert extract_sections(_LEGACY_UNTAGGED_TEXT, allow_legacy=True) == {
        "workflow": "rule 1\nrule 2\n",
    }


def test_hash_sections_legacy_path() -> None:
    """``hash_sections`` works under ``allow_legacy=True`` on untagged input."""
    digests = hash_sections(_LEGACY_UNTAGGED_TEXT, allow_legacy=True)
    assert digests == {"workflow": _sha256_hex("rule 1\nrule 2\n")}


def test_extract_marker_hashes_legacy_returns_none() -> None:
    """Every section's embedded hash is None on a legacy file."""
    text = _LEGACY_UNTAGGED_TEXT + (
        "<!-- setforge:user-section start commits -->\n"
        "body\n"
        "<!-- setforge:user-section end commits -->\n"
    )
    hashes = extract_marker_hashes(text, allow_legacy=True)
    assert hashes == {"workflow": None, "commits": None}


def test_walk_markers_strict_rejects_pre_9by_fixture() -> None:
    """The on-disk pre-9by fixture is rejected by the strict parser."""
    fixture_text = (Path(__file__).parent / "fixtures" / "pre_9by_CLAUDE.md").read_text(
        encoding="utf-8"
    )
    with pytest.raises(MarkerError, match="missing required"):
        extract_sections(fixture_text)


def test_walk_markers_allow_legacy_accepts_pre_9by_fixture() -> None:
    """The on-disk pre-9by fixture parses under allow_legacy=True with two
    sections (workflow, commits) and all-None embedded hashes."""
    fixture_text = (Path(__file__).parent / "fixtures" / "pre_9by_CLAUDE.md").read_text(
        encoding="utf-8"
    )
    bodies = extract_sections(fixture_text, allow_legacy=True)
    assert set(bodies) == {"workflow", "commits"}
    assert "Stay focused" in bodies["workflow"]
    assert "imperative mood" in bodies["commits"]
    assert extract_marker_hashes(fixture_text, allow_legacy=True) == {
        "workflow": None,
        "commits": None,
    }


# ---------------------------------------------------------------------------
# setforge-9ln — detect_legacy_markers helper
# ---------------------------------------------------------------------------


def test_detect_legacy_markers_returns_true_for_untagged_start() -> None:
    assert detect_legacy_markers(_LEGACY_UNTAGGED_TEXT) is True


def test_detect_legacy_markers_returns_true_for_missing_hash() -> None:
    assert detect_legacy_markers(_LEGACY_TAGGED_HASHLESS_TEXT) is True


def test_detect_legacy_markers_returns_false_for_strict_clean() -> None:
    text = (
        "<!-- setforge:user-section start shared workflow -->\n"
        "body\n"
        f"<!-- setforge:user-section end shared workflow hash={_HASH_HEX_64} -->\n"
    )
    assert detect_legacy_markers(text) is False


def test_detect_legacy_markers_returns_false_for_no_markers() -> None:
    """A file with no markers at all isn't 'legacy' — there's nothing to migrate."""
    assert detect_legacy_markers("plain text\nno markers here\n") is False


def test_detect_legacy_markers_returns_true_when_any_marker_is_legacy() -> None:
    """One legacy marker among otherwise-clean markers still flags the file."""
    text = (
        "<!-- setforge:user-section start shared a -->\n"
        "body\n"
        f"<!-- setforge:user-section end shared a hash={_HASH_HEX_64} -->\n"
        "<!-- setforge:user-section start workflow -->\n"
        "body 2\n"
        "<!-- setforge:user-section end workflow -->\n"
    )
    assert detect_legacy_markers(text) is True


def test_detect_legacy_markers_flags_malformed_end_hash() -> None:
    """An end marker carrying a non-64-hex ``hash=`` value flags as legacy.

    Without this branch a live file with ``hash=NOTHEX`` slips past the
    CLI's legacy-detector and surfaces a raw ``MarkerError`` mid-flight
    instead of the friendly "run install first" upgrade hint.
    """
    text = (
        "<!-- setforge:user-section start shared a -->\n"
        "body\n"
        "<!-- setforge:user-section end shared a hash=NOTHEX -->\n"
    )
    assert detect_legacy_markers(text) is True


# ---------------------------------------------------------------------------
# setforge-2ba.7 — detect_legacy_namespace_markers helper
# (post-rename detection of pre-rename `my-setup:user-section` markers)
# ---------------------------------------------------------------------------


def test_detect_legacy_namespace_markers_returns_true_for_old_start() -> None:
    text = "<!-- my-setup:user-section start shared workflow -->\n"
    from setforge.sections import detect_legacy_namespace_markers

    assert detect_legacy_namespace_markers(text) is True


def test_detect_legacy_namespace_markers_returns_true_for_old_end() -> None:
    text = f"<!-- my-setup:user-section end shared workflow hash={_HASH_HEX_64} -->\n"
    from setforge.sections import detect_legacy_namespace_markers

    assert detect_legacy_namespace_markers(text) is True


def test_detect_legacy_namespace_markers_returns_false_for_new_namespace() -> None:
    text = (
        "<!-- setforge:user-section start shared workflow -->\n"
        "body\n"
        f"<!-- setforge:user-section end shared workflow hash={_HASH_HEX_64} -->\n"
    )
    from setforge.sections import detect_legacy_namespace_markers

    assert detect_legacy_namespace_markers(text) is False


def test_detect_legacy_namespace_markers_returns_false_for_no_markers() -> None:
    from setforge.sections import detect_legacy_namespace_markers

    assert detect_legacy_namespace_markers("plain text\nno markers\n") is False


def test_detect_legacy_namespace_markers_flags_mixed_namespaces() -> None:
    """A file with BOTH old and new namespace markers still flags as legacy."""
    text = (
        "<!-- setforge:user-section start shared a -->\n"
        "body\n"
        f"<!-- setforge:user-section end shared a hash={_HASH_HEX_64} -->\n"
        "<!-- my-setup:user-section start shared b -->\n"
        "body2\n"
        f"<!-- my-setup:user-section end shared b hash={_HASH_HEX_64} -->\n"
    )
    from setforge.sections import detect_legacy_namespace_markers

    assert detect_legacy_namespace_markers(text) is True


def test_malformed_hash_segment_raises_in_strict_mode() -> None:
    """A non-64-hex ``hash=`` value is rejected with a clear MarkerError."""
    text = (
        "<!-- setforge:user-section start shared FOO -->\n"
        "body\n"
        "<!-- setforge:user-section end shared FOO hash=NOTHEX -->\n"
    )
    with pytest.raises(MarkerError, match="malformed hash"):
        extract_sections(text)


def test_malformed_hash_segment_treated_as_absent_under_allow_legacy() -> None:
    """``allow_legacy=True`` tolerates a malformed hash as if it were absent."""
    text = (
        "<!-- setforge:user-section start shared FOO -->\n"
        "body\n"
        "<!-- setforge:user-section end shared FOO hash=NOTHEX -->\n"
    )
    assert extract_sections(text, allow_legacy=True) == {"FOO": "body\n"}


def test_valid_64_hex_hash_still_parses() -> None:
    """A well-formed 64-hex hash continues to parse cleanly under strict mode."""
    valid = "a" * 64
    text = (
        "<!-- setforge:user-section start shared FOO -->\n"
        "body\n"
        f"<!-- setforge:user-section end shared FOO hash={valid} -->\n"
    )
    extract_sections(text)


def test_unknown_semantics_raises_marker_error_with_line() -> None:
    """Unknown semantics keyword surfaces a MarkerError naming the line and keyword."""
    text = (
        "<!-- setforge:user-section start fish-tacos NAME -->\n"
        "body\n"
        "<!-- setforge:user-section end fish-tacos NAME -->\n"
    )
    with pytest.raises(MarkerError) as excinfo:
        list(extract_sections(text))
    msg = str(excinfo.value)
    assert "line 1" in msg, msg
    assert "unknown semantics" in msg, msg
    assert "fish-tacos" in msg, msg


def test_unknown_semantics_raises_under_allow_legacy() -> None:
    """Unknown (not missing) semantics still raises even under allow_legacy=True.

    Only NULL/missing semantics gets the legacy SHARED fallback; an
    explicit-but-invalid keyword is still a malformed marker.
    """
    text = (
        "<!-- setforge:user-section start fish-tacos NAME -->\n"
        "body\n"
        "<!-- setforge:user-section end fish-tacos NAME -->\n"
    )
    with pytest.raises(MarkerError) as excinfo:
        extract_sections(text, allow_legacy=True)
    assert "unknown semantics" in str(excinfo.value)
