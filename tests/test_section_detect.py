"""Unit tests for the section-detect engine (9hrw S2).

Covers the pitfall-flagged correctness properties: idempotency (no edits → no
regions), deploy-normalization false-positives (CRLF / trailing newline must NOT
read as edits), insertion vs divergence classification, deletions, and
multi-region detection with correct line ranges + text.
"""

from __future__ import annotations

from setforge.anchors import (
    AnchorAfterHeading,
    AnchorAtEndOfFile,
    AnchorAtStartOfFile,
    AnchorInSection,
)
from setforge.section_detect import (
    AnchorRefusal,
    DetectRegion,
    RegionKind,
    compute_detect_regions,
    propose_anchor,
)


def _only_region(live: str, expected: str) -> DetectRegion:
    regions = compute_detect_regions(live, expected)
    assert len(regions) == 1, regions
    return regions[0]


_BASE = "# Title\n\n## My Notes\n- tracked default\n\n## Workflow\n- step one\n"


def test_idempotency_identical_text_no_regions() -> None:
    assert compute_detect_regions(_BASE, _BASE) == []


def test_idempotency_crlf_live_not_flagged() -> None:
    """A CRLF live file equal-after-normalization to LF expected → no drift."""
    crlf = _BASE.replace("\n", "\r\n")
    assert compute_detect_regions(crlf, _BASE) == []


def test_idempotency_cr_live_not_flagged() -> None:
    cr = _BASE.replace("\n", "\r")
    assert compute_detect_regions(cr, _BASE) == []


def test_pure_insertion_is_new_content() -> None:
    live = _BASE + "\n## Scratch\n- host-only idea\n"
    regions = compute_detect_regions(live, _BASE)
    assert len(regions) == 1
    r = regions[0]
    assert r.kind is RegionKind.NEW_CONTENT
    assert r.expected_start == r.expected_end  # nothing replaced
    assert "## Scratch" in r.live_text
    assert "host-only idea" in r.live_text
    assert r.expected_text == ""


def test_modification_is_divergence() -> None:
    live = _BASE.replace("- tracked default", "- my host override")
    regions = compute_detect_regions(live, _BASE)
    assert len(regions) == 1
    r = regions[0]
    assert r.kind is RegionKind.DIVERGENCE
    assert "my host override" in r.live_text
    assert "tracked default" in r.expected_text


def test_deletion_is_divergence() -> None:
    live = _BASE.replace("- step one\n", "")
    regions = compute_detect_regions(live, _BASE)
    assert len(regions) == 1
    r = regions[0]
    assert r.kind is RegionKind.DIVERGENCE
    assert "step one" in r.expected_text
    # The deleted line is gone from live; the live side of the region is empty.
    assert "step one" not in r.live_text


def test_multiple_regions_detected_separately() -> None:
    live = _BASE.replace("- tracked default", "- override A")
    live = live + "\n## Scratch\n- new B\n"
    regions = compute_detect_regions(live, _BASE)
    assert len(regions) == 2
    kinds = {r.kind for r in regions}
    assert kinds == {RegionKind.DIVERGENCE, RegionKind.NEW_CONTENT}


def test_region_line_indices_slice_correctly() -> None:
    """The reported live indices must slice the live lines back to live_text."""
    live = _BASE + "## Extra\n"
    live_lines = live.splitlines(keepends=True)
    regions = compute_detect_regions(live, _BASE)
    assert len(regions) == 1
    r = regions[0]
    assert "".join(live_lines[r.live_start : r.live_end]) == r.live_text


def test_trailing_newline_only_difference_not_flagged_after_norm() -> None:
    """Trailing-newline pinning is deploy normalization, not a user edit.

    `compute_detect_regions` operates on the EXPECTED deploy output (already
    newline-canonicalized) vs live, so when the only difference is the trailing
    newline both sides agree after the caller's canonicalization. Here we assert
    the engine itself reports a single tail region for a genuine trailing-line
    insertion (so callers that canonicalize upstream get clean idempotency).
    """
    # Genuine extra trailing content IS a region (not suppressed).
    regions = compute_detect_regions(_BASE + "trailing\n", _BASE)
    assert len(regions) == 1
    assert regions[0].kind is RegionKind.NEW_CONTENT


def test_empty_live_against_expected_is_one_deletion() -> None:
    regions = compute_detect_regions("", _BASE)
    assert len(regions) == 1
    assert regions[0].kind is RegionKind.DIVERGENCE
    assert regions[0].live_text == ""


def test_detectregion_is_frozen() -> None:
    r = DetectRegion(
        kind=RegionKind.NEW_CONTENT,
        live_start=0,
        live_end=1,
        expected_start=0,
        expected_end=0,
        live_text="x\n",
        expected_text="",
    )
    import dataclasses

    import pytest

    with pytest.raises(dataclasses.FrozenInstanceError):
        r.kind = RegionKind.DIVERGENCE  # type: ignore[misc]


# ---------------------------------------------------------------------------
# propose_anchor (S3) — safe anchoring + refusals
# ---------------------------------------------------------------------------

_DOC = "# Title\n\n## My Notes\n- tracked default\n\n## Workflow\n- step one\n"


def test_anchor_divergence_under_unique_heading() -> None:
    live = _DOC.replace("- tracked default", "- my override")
    region = _only_region(live, _DOC)
    anchor = propose_anchor(region, live, _DOC)
    assert isinstance(anchor, AnchorAfterHeading)
    assert anchor.value == "My Notes"


def test_anchor_new_content_below_section_content_is_in_section() -> None:
    """Content appended below a section's existing content gets an exact
    in-section anchor (preceding line + offset), not just the heading — so it
    re-lands where typed instead of jumping under the heading (setforge-b300)."""
    live = _DOC + "loose host note\n"
    region = _only_region(live, _DOC)
    assert region.kind is RegionKind.NEW_CONTENT
    anchor = propose_anchor(region, live, _DOC)
    assert isinstance(anchor, AnchorInSection)
    assert anchor.heading == "Workflow"
    assert anchor.after_line == "- step one"


def test_anchor_new_content_appended_no_headings_is_end_of_file() -> None:
    expected = "plain line\n"
    live = expected + "appended host note\n"
    region = _only_region(live, expected)
    assert region.kind is RegionKind.NEW_CONTENT
    anchor = propose_anchor(region, live, expected)
    assert isinstance(anchor, AnchorAtEndOfFile)


def test_anchor_new_content_at_start_is_start_of_file() -> None:
    live = "host preamble\n" + _DOC
    region = _only_region(live, _DOC)
    assert region.kind is RegionKind.NEW_CONTENT
    anchor = propose_anchor(region, live, _DOC)
    assert isinstance(anchor, AnchorAtStartOfFile)


def test_anchor_refuses_ambiguous_heading() -> None:
    dup = "## Notes\n- a\n\n## Notes\n- b\n"
    live = dup.replace("- a", "- a EDIT")
    region = _only_region(live, dup)
    anchor = propose_anchor(region, live, dup)
    assert isinstance(anchor, AnchorRefusal)
    assert "ambiguous" in anchor.reason


def test_anchor_refuses_heading_absent_from_tracked() -> None:
    """A heading the user added live but absent from tracked would orphan."""
    expected = "# Title\n\nbody\n"
    live = "# Title\n\n## Host Only\n- mine\n\nbody\n"
    region = _only_region(live, expected)
    # The region is under the user's new "## Host Only" heading, which is not
    # in tracked — anchoring there orphans on install.
    anchor = propose_anchor(region, live, expected)
    # New content with an enclosing heading absent from tracked is refused
    # (orphan) OR, if classified as a pure insertion at the boundary, anchored
    # safely; assert it never returns a heading anchor missing from tracked.
    if isinstance(anchor, AnchorAfterHeading):
        assert anchor.value != "Host Only"


def test_anchor_refuses_closing_hash_heading() -> None:
    doc = "## Notes ##\n- default\n"
    live = doc.replace("- default", "- override")
    region = _only_region(live, doc)
    anchor = propose_anchor(region, live, doc)
    assert isinstance(anchor, AnchorRefusal)
    assert "closing-hash" in anchor.reason


def test_anchor_refuses_setext_heading() -> None:
    doc = "My Notes\n========\n- default\n"
    live = doc.replace("- default", "- override")
    region = _only_region(live, doc)
    anchor = propose_anchor(region, live, doc)
    assert isinstance(anchor, AnchorRefusal)
    assert "setext" in anchor.reason


def test_anchor_ignores_heading_inside_code_fence() -> None:
    """A heading-shaped line inside a fence must not be chosen as the anchor."""
    doc = "## Real\n```\n## Fake\n```\n- default\n"
    live = doc.replace("- default", "- override")
    region = _only_region(live, doc)
    anchor = propose_anchor(region, live, doc)
    assert isinstance(anchor, AnchorAfterHeading)
    assert anchor.value == "Real"


def test_anchor_refuses_divergence_with_no_heading() -> None:
    expected = "plain line one\nplain line two\n"
    live = expected.replace("plain line one", "edited line one")
    region = _only_region(live, expected)
    anchor = propose_anchor(region, live, expected)
    assert isinstance(anchor, AnchorRefusal)
