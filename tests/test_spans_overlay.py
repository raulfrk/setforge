"""Tests for the span merge re-overlay (Stage 5).

The re-overlay runs AFTER the whole-file merge: it splices live bytes over
each PINNED span (forked spans get no override) and recomputes the
per-span derived state from the POST-splice text so the base can
re-baseline to exactly what landed on disk (Invariant I1).
"""

import hashlib

from setforge.markdown_spans import bound_span
from setforge.spans import SpanEntry, SpanKind
from setforge.spans_overlay import apply_spans
from setforge.spans_store import SpanState


def _state_for(doc: str, anchor: str) -> SpanState:
    span = bound_span(doc, anchor)
    lines = doc.splitlines()
    return SpanState(
        anchor=anchor,
        fingerprint=hashlib.sha256(span.body.encode("utf-8")).hexdigest(),
        prefix=lines[max(0, span.start_line - 3) : span.start_line],
        suffix=lines[span.end_line : span.end_line + 3],
        position_hint_start_line=span.start_line,
        position_hint_n_lines=span.end_line - span.start_line,
        heading_level=span.level,
    )


_BASE = """\
# Title

## Foo

Pinned body original.

## Bar

Shared body original.
"""


def test_pinned_span_splices_live_over_merge() -> None:
    # Live froze Foo to a custom value; merge (theirs) advanced both Foo
    # and Bar. The pinned splice must restore live's Foo, keep merged Bar.
    live = _BASE.replace("Pinned body original.", "Pinned body LIVE custom.")
    merged = _BASE.replace("Pinned body original.", "Pinned body UPSTREAM.").replace(
        "Shared body original.", "Shared body UPSTREAM."
    )
    state = _state_for(live, "## Foo")
    spans = [SpanEntry(anchor="## Foo", kind=SpanKind.PINNED)]

    result = apply_spans(merged, live, spans, {"## Foo": state})

    assert "Pinned body LIVE custom." in result.text  # live wins for Foo
    assert "Shared body UPSTREAM." in result.text  # merge wins for Bar
    assert "Pinned body UPSTREAM." not in result.text
    assert not result.orphans


def test_forked_span_keeps_merge_no_override() -> None:
    live = _BASE.replace("Pinned body original.", "Forked body LIVE.")
    merged = _BASE.replace("Pinned body original.", "Forked body UPSTREAM.")
    state = _state_for(live, "## Foo")
    spans = [SpanEntry(anchor="## Foo", kind=SpanKind.FORKED)]

    result = apply_spans(merged, live, spans, {"## Foo": state})

    # Forked: merge result is kept (no live override on merge).
    assert "Forked body UPSTREAM." in result.text
    assert "Forked body LIVE." not in result.text


def test_recomputed_state_matches_post_splice_body() -> None:
    live = _BASE.replace("Pinned body original.", "Pinned body LIVE custom.")
    merged = _BASE.replace("Pinned body original.", "Pinned body UPSTREAM.")
    state = _state_for(live, "## Foo")
    spans = [SpanEntry(anchor="## Foo", kind=SpanKind.PINNED)]

    result = apply_spans(merged, live, spans, {"## Foo": state})

    new_state = result.new_states["## Foo"]
    post_span = bound_span(result.text, "## Foo")
    expected_fp = hashlib.sha256(post_span.body.encode("utf-8")).hexdigest()
    assert new_state.fingerprint == expected_fp


def test_orphaned_pinned_span_preserved_and_reported() -> None:
    # The pinned heading is gone from the merged text AND from live.
    live = _BASE.replace("## Foo\n\nPinned body original.\n\n", "")
    merged = _BASE.replace("## Foo\n\nPinned body original.\n\n", "")
    state = _state_for(_BASE, "## Foo")
    spans = [SpanEntry(anchor="## Foo", kind=SpanKind.PINNED)]

    result = apply_spans(merged, live, spans, {"## Foo": state})

    # Orphan reported; install does not crash; merged text is preserved.
    assert "## Foo" in [o.anchor for o in result.orphans]
    assert result.orphans[0].kind is SpanKind.PINNED


def test_no_spans_is_identity() -> None:
    result = apply_spans(_BASE, _BASE, [], {})
    assert result.text == _BASE
    assert not result.orphans
    assert result.new_states == {}
