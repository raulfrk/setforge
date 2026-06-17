"""Unit tests for the exact-position OVERLAY anchor.

Regression coverage for the bug where carved host-local content relocated to
just under its enclosing heading instead of staying where it was hand-typed.
These are pure-function tests over the carve → deploy primitives
(``propose_anchor`` → ``inject_body_at_anchor``), mirroring the real
``section detect`` → ``install`` round-trip without the docker e2e cost.
"""

from __future__ import annotations

import logging

import pytest

from setforge.anchors import AnchorAfterHeading, AnchorInSection
from setforge.errors import AnchorAmbiguousError, AnchorNotFoundError
from setforge.host_local_inject import _resolve_in_section
from setforge.overlay_deploy import inject_overlay_bodies
from setforge.overlay_inject import canonical_body, inject_body_at_anchor
from setforge.section_detect import (
    DetectRegion,
    RegionKind,
    compute_detect_regions,
    propose_anchor,
)
from setforge.spans import OverlaySpanPayload, SpanEntry, SpanKind


def _only_region(live: str, expected: str) -> DetectRegion:
    regions = compute_detect_regions(live, expected)
    assert len(regions) == 1, regions
    return regions[0]


def _deploy(expected: str, anchor: object, region: DetectRegion) -> list[str]:
    """Re-inject the carved body into ``expected`` exactly as install does."""
    body = canonical_body(region.live_text)
    return inject_body_at_anchor(expected, anchor, body).splitlines()  # type: ignore[arg-type]


_MULTI = "# Title\n\n## A\na1\na2\na3\n\n## B\nb1\n"


def test_mid_section_insert_relands_where_typed() -> None:
    """Content typed between a1 and a2 re-lands between a1 and a2 — not under
    the heading (the core anchor-positioning regression)."""
    live = "# Title\n\n## A\na1\nHOST NOTE\na2\na3\n\n## B\nb1\n"
    region = _only_region(live, _MULTI)
    assert region.kind is RegionKind.NEW_CONTENT

    anchor = propose_anchor(region, live, _MULTI)
    assert isinstance(anchor, AnchorInSection)
    assert anchor.heading == "A"

    lines = _deploy(_MULTI, anchor, region)
    i = lines.index("HOST NOTE")
    assert lines[i - 1] == "a1"
    assert lines[i + 1] == "a2"


def test_end_of_section_insert_relands_at_section_end() -> None:
    """Content typed at the end of a section (after a3, before ## B) re-lands
    at the section end — exact and snap-to-end coincide here."""
    live = "# Title\n\n## A\na1\na2\na3\nHOST NOTE\n\n## B\nb1\n"
    region = _only_region(live, _MULTI)
    anchor = propose_anchor(region, live, _MULTI)
    assert isinstance(anchor, AnchorInSection)
    assert anchor.after_line == "a3"

    lines = _deploy(_MULTI, anchor, region)
    i = lines.index("HOST NOTE")
    assert lines[i - 1] == "a3"  # immediately after the last content line
    assert i < lines.index("## B")  # still inside section A


def test_top_of_section_insert_stays_after_heading() -> None:
    """Content typed directly under the heading (before a1) keeps the degenerate
    after-heading anchor — no new kind, well-tested path (decision #5)."""
    live = "# Title\n\n## A\nHOST NOTE\na1\na2\na3\n\n## B\nb1\n"
    region = _only_region(live, _MULTI)
    anchor = propose_anchor(region, live, _MULTI)
    assert isinstance(anchor, AnchorAfterHeading)
    assert anchor.value == "A"


def test_non_unique_preceding_line_falls_to_offset() -> None:
    """When the preceding line is not unique in the section, after_line is None
    and the offset carries the position (decision #2)."""
    expected = "# Title\n\n## A\nx\ny\nx\n\n## B\nb1\n"
    live = "# Title\n\n## A\nx\ny\nx\nHOST NOTE\n\n## B\nb1\n"
    region = _only_region(live, expected)
    anchor = propose_anchor(region, live, expected)
    assert isinstance(anchor, AnchorInSection)
    # "x" is duplicated in section A → after_line cannot disambiguate.
    assert anchor.after_line is None

    lines = inject_body_at_anchor(
        expected, anchor, canonical_body(region.live_text)
    ).splitlines()
    i = lines.index("HOST NOTE")
    # offset places it after the SECOND "x" (the last content line of section A).
    assert lines[i - 1] == "x"
    assert i < lines.index("## B")  # still inside section A


def test_fence_heading_name_not_matched() -> None:
    """A heading name appearing inside a fenced code block is never the anchor."""
    expected = "# Title\n\n## A\na1\n\n```\n## A\nfake\n```\n\n## B\nb1\n"
    live = "# Title\n\n## A\na1\nHOST NOTE\n\n```\n## A\nfake\n```\n\n## B\nb1\n"
    region = _only_region(live, expected)
    anchor = propose_anchor(region, live, expected)
    assert isinstance(anchor, AnchorInSection)
    lines = inject_body_at_anchor(
        expected, anchor, canonical_body(region.live_text)
    ).splitlines()
    i = lines.index("HOST NOTE")
    assert lines[i - 1] == "a1"  # the real heading's content, not the fence


def test_crlf_live_produces_correct_in_section_anchor() -> None:
    """A CRLF live file resolves to the same section position as LF."""
    live = "# Title\r\n\r\n## A\r\na1\r\nHOST NOTE\r\na2\r\na3\r\n\r\n## B\r\nb1\r\n"
    region = _only_region(live, _MULTI)
    anchor = propose_anchor(region, live, _MULTI)
    assert isinstance(anchor, AnchorInSection)
    assert anchor.after_line == "a1"


# ---------------------------------------------------------------------------
# Resolver edges: fallback + heading-gone hard-fail (decisions #3, #4)
# ---------------------------------------------------------------------------

_TRACKED = "# Title\n\n## A\nx1\nx2\n\n## B\nb1\n"


def test_resolver_exact_preceding_line() -> None:
    anchor = AnchorInSection(heading="A", level=2, after_line="x1", offset=1)
    line, fell_back = _resolve_in_section(_TRACKED, anchor)
    assert fell_back is False
    assert _TRACKED.splitlines()[line] == "x2"  # spliced immediately after x1


def test_resolver_offset_when_after_line_absent() -> None:
    anchor = AnchorInSection(heading="A", level=2, after_line=None, offset=2)
    line, fell_back = _resolve_in_section(_TRACKED, anchor)
    assert fell_back is False
    # heading at line 2 → 2 + 1 + offset(2) = 5, inside section A (## B is line 6).
    assert line == 5


def test_resolver_falls_back_to_end_of_section() -> None:
    """Neither resolver matches but the heading is present → end-of-section."""
    anchor = AnchorInSection(heading="A", level=2, after_line="GONE", offset=99)
    line, fell_back = _resolve_in_section(_TRACKED, anchor)
    assert fell_back is True
    # section A ends at the "## B" heading line.
    assert _TRACKED.splitlines()[line] == "## B"


def test_resolver_heading_gone_hard_fails() -> None:
    anchor = AnchorInSection(heading="A", level=2, after_line="x1", offset=1)
    with pytest.raises(AnchorNotFoundError):
        _resolve_in_section("# Title\n\n## Z\nz1\n", anchor)


def test_resolver_heading_duplicated_hard_fails() -> None:
    anchor = AnchorInSection(heading="A", level=2, after_line="x1", offset=1)
    with pytest.raises(AnchorAmbiguousError):
        _resolve_in_section("## A\nx1\n\n## A\nq\n", anchor)


def test_deploy_warns_on_fallback(caplog: pytest.LogCaptureFixture) -> None:
    """inject_overlay_bodies warns (and still injects) when an in-section body
    falls back to end-of-section (decision #3)."""
    span = SpanEntry(
        anchor="vmnotes",
        kind=SpanKind.OVERLAY,
        overlay=OverlaySpanPayload(
            anchor=AnchorInSection(heading="A", level=2, after_line="GONE", offset=99),
            body="HOST NOTE\n",
        ),
    )
    with caplog.at_level(logging.WARNING, logger="setforge.overlay_deploy"):
        injected, _states = inject_overlay_bodies(_TRACKED, [span], {})
    assert "HOST NOTE" in injected  # body still deployed
    assert "end of section" in caplog.text
    assert "vmnotes" in caplog.text
