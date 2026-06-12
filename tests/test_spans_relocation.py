"""Tests for the markdown span relocation ladder (Stage 4).

First-hit-wins ladder, each stage a hint not a pointer:
(a) fingerprint exact-match at position_hint;
(b) fingerprint unique-anywhere (multi-hit -> ORPHAN);
(c) heading resolve (duplicate heading -> ORPHAN);
(d) diff-match-patch match_main fuzzy, conservative, biased to orphan;
(e) ORPHAN.

Invariant I8: ambiguity orphans, never picks-first.
"""

import hashlib

from setforge.markdown_spans import bound_span
from setforge.spans_relocation import RelocationStatus, relocate_span
from setforge.spans_store import SpanState


def _state_for(doc: str, anchor: str) -> SpanState:
    """Build a SpanState as the install path would, from a known doc."""
    span = bound_span(doc, anchor)
    lines = doc.splitlines()
    fp = hashlib.sha256(span.body.encode("utf-8")).hexdigest()
    return SpanState(
        anchor=anchor,
        fingerprint=fp,
        prefix=lines[max(0, span.start_line - 3) : span.start_line],
        suffix=lines[span.end_line : span.end_line + 3],
        position_hint_start_line=span.start_line,
        position_hint_n_lines=span.end_line - span.start_line,
        heading_level=span.level,
    )


_DOC = """\
# Title

## Foo

Body of Foo.

## Bar

Body of Bar.
"""


def test_relocate_fingerprint_exact_at_hint() -> None:
    state = _state_for(_DOC, "## Foo")
    result = relocate_span(_DOC, "## Foo", state)
    assert result.status is RelocationStatus.LOCATED
    assert result.span is not None
    assert "Body of Foo." in result.span.body
    assert "## Bar" not in result.span.body


def test_relocate_fingerprint_unique_anywhere_after_shift() -> None:
    state = _state_for(_DOC, "## Foo")
    # Prepend lines so the position hint is stale but the body is intact
    # and unique elsewhere.
    shifted = "Extra top matter.\n\nAnother line.\n\n" + _DOC
    result = relocate_span(shifted, "## Foo", state)
    assert result.status is RelocationStatus.LOCATED
    assert result.span is not None
    assert "Body of Foo." in result.span.body


def test_relocate_heading_resolve_when_body_edited() -> None:
    state = _state_for(_DOC, "## Foo")
    # Body changed (fingerprint no longer matches) but the heading is
    # still present and unique -> heading-resolve stage.
    edited = _DOC.replace("Body of Foo.", "Body of Foo, now revised entirely.")
    result = relocate_span(edited, "## Foo", state)
    assert result.status is RelocationStatus.LOCATED
    assert result.span is not None
    assert "now revised entirely" in result.span.body


def test_relocate_orphan_when_heading_gone() -> None:
    state = _state_for(_DOC, "## Foo")
    gone = _DOC.replace("## Foo\n\nBody of Foo.\n\n", "")
    result = relocate_span(gone, "## Foo", state)
    assert result.status is RelocationStatus.ORPHAN
    assert result.span is None


def test_relocate_orphan_on_duplicate_heading() -> None:
    state = _state_for(_DOC, "## Foo")
    # Two identical "## Foo" headings, both with edited bodies so neither
    # fingerprint-matches -> heading resolve sees a duplicate -> ORPHAN
    # (never pick-first).
    dup = "## Foo\n\nEdited A.\n\n## Foo\n\nEdited B.\n"
    result = relocate_span(dup, "## Foo", state)
    assert result.status is RelocationStatus.ORPHAN


def test_relocate_orphan_on_multiple_fingerprint_hits() -> None:
    state = _state_for(_DOC, "## Foo")
    # The exact span body appears twice -> fingerprint unique stage must
    # ORPHAN rather than pick the first.
    span_body = bound_span(_DOC, "## Foo").body
    twinned = span_body + "\n" + span_body + "\n## Tail\n\nx\n"
    result = relocate_span(twinned, "## Foo", state)
    assert result.status is RelocationStatus.ORPHAN


def test_relocate_fuzzy_finds_lightly_renamed_heading() -> None:
    state = _state_for(_DOC, "## Foo")
    # Heading text drifts by one char so byte-exact heading-resolve fails
    # but the fuzzy stage recovers it near the hint.
    drifted = _DOC.replace("## Foo", "## Fooo")
    result = relocate_span(drifted, "## Foo", state)
    assert result.status is RelocationStatus.LOCATED
    assert result.stage == "fuzzy"


def test_relocate_orphan_when_content_wholly_different() -> None:
    state = _state_for(_DOC, "## Foo")
    # No fingerprint hit, no heading, nothing close -> fuzzy biased to
    # orphan rather than confidently mis-relocating.
    unrelated = "# Other\n\nCompletely different content here.\n"
    result = relocate_span(unrelated, "## Foo", state)
    assert result.status is RelocationStatus.ORPHAN


def test_relocate_malformed_stored_anchor_orphans_no_crash() -> None:
    # A stored anchor that is NOT a well-formed ATX heading line must
    # orphan, never crash: _parse_anchor raising would break the
    # "never a crash" contract apply_spans / capture rely on.
    state = SpanState(
        anchor="not a heading at all",
        fingerprint="deadbeef",
        prefix=[],
        suffix=[],
        position_hint_start_line=0,
        position_hint_n_lines=1,
        heading_level=2,
    )
    result = relocate_span(_DOC, "not a heading at all", state)
    assert result.status is RelocationStatus.ORPHAN
    assert result.span is None


def test_relocate_breadcrumb_anchor_resolves_via_heading_stage() -> None:
    # A stored BREADCRUMB anchor parses under _parse_anchor as ONE heading
    # whose text is the whole chain, so the fingerprint stages find no
    # candidate heading; Stage 3's bound_span understands the breadcrumb
    # and resolves the edited (stale-fingerprint) leaf.
    doc = """\
## Final checks

### Failure handling

final body

## Deployment

### Failure handling

deploy body
"""
    anchor = "## Final checks > ### Failure handling"
    state = _state_for(doc, anchor)
    edited = doc.replace("final body", "final body, revised")
    result = relocate_span(edited, anchor, state)
    assert result.status is RelocationStatus.LOCATED
    assert result.stage == "heading-resolve"
    assert result.span is not None
    assert "final body, revised" in result.span.body
    assert "deploy body" not in result.span.body


def test_fuzzy_post_match_rejects_non_heading_landing() -> None:
    # The fuzzy stage's char-offset match could drift onto a non-heading
    # line; the post-match structural guard rejects it (orphan) rather
    # than accept a confident wrong-relocation. Here the heading is gone
    # and only a prose line vaguely resembling it remains.
    state = _state_for(_DOC, "## Foo")
    drifted = _DOC.replace("## Foo\n", "Foo discussion paragraph\n")
    result = relocate_span(drifted, "## Foo", state)
    assert result.status is RelocationStatus.ORPHAN
