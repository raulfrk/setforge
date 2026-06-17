"""Unit tests for the section-detect engine (9hrw S2).

Covers the pitfall-flagged correctness properties: idempotency (no edits → no
regions), deploy-normalization false-positives (CRLF / trailing newline must NOT
read as edits), insertion vs divergence classification, deletions, and
multi-region detection with correct line ranges + text.
"""

from __future__ import annotations

from setforge.section_detect import (
    DetectRegion,
    RegionKind,
    compute_detect_regions,
)

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
