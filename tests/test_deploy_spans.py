"""Tests for the span re-overlay branch of copy_atomic (Stage 5 integration).

These exercise :func:`setforge.deploy.copy_atomic` with ``spans`` set,
asserting the pinned splice imposes live bytes over the merged region and
that ``new_base`` re-baselines to the POST-splice bytes that land on disk
(Invariant I1) — never the pre-splice merge result.
"""

import hashlib
from pathlib import Path

from setforge.config import Disposition
from setforge.deploy import copy_atomic
from setforge.markdown_spans import bound_span
from setforge.spans import SpanEntry, SpanKind
from setforge.spans_store import SpanState

_BASE = """\
# Title

## Pinned

Original pinned body.

## Shared

Original shared body.
"""


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


def test_pinned_span_splice_and_base_equals_disk(tmp_path: Path) -> None:
    # live froze Pinned to a custom value; tracked advanced Shared upstream.
    live = _BASE.replace("Original pinned body.", "Custom pinned body.")
    tracked = _BASE.replace("Original shared body.", "Upstream shared body.")
    src = tmp_path / "src.md"
    src.write_text(tracked)
    dst = tmp_path / "dst.md"
    dst.write_text(live)
    state = _state_for(live, "## Pinned")

    result = copy_atomic(
        src,
        dst,
        disposition=Disposition.SHARED,
        base_text=_BASE,
        spans=[SpanEntry(anchor="## Pinned", kind=SpanKind.PINNED)],
        span_states={"## Pinned": state},
    )

    on_disk = dst.read_text()
    # Pinned region kept live; shared region took upstream.
    assert "Custom pinned body." in on_disk
    assert "Upstream shared body." in on_disk
    # Invariant I1: the re-baselined base equals what landed on disk,
    # NOT the pre-splice merge result.
    assert result.new_base == on_disk
    assert result.new_span_states is not None
    assert "## Pinned" in result.new_span_states


def test_pinned_span_survives_two_installs_no_phantom_conflict(tmp_path: Path) -> None:
    # Round-trip: pin a span, edit upstream ELSEWHERE, install twice; the
    # pinned live region must stay byte-stable with no phantom conflict.
    src = tmp_path / "src.md"
    dst = tmp_path / "dst.md"
    src.write_text(_BASE)
    dst.write_text(_BASE)

    # First install seeds base = tracked (no base yet).
    r0 = copy_atomic(
        src,
        dst,
        disposition=Disposition.SHARED,
        base_text=None,
        spans=[SpanEntry(anchor="## Pinned", kind=SpanKind.PINNED)],
        span_states={},
    )
    base = r0.new_base
    states = r0.new_span_states or {}

    # User edits the live pinned region; upstream edits the shared region.
    live = dst.read_text().replace("Original pinned body.", "MY PIN.")
    dst.write_text(live)
    src.write_text(_BASE.replace("Original shared body.", "Upstream shared body."))

    r1 = copy_atomic(
        src,
        dst,
        disposition=Disposition.SHARED,
        base_text=base,
        spans=[SpanEntry(anchor="## Pinned", kind=SpanKind.PINNED)],
        span_states=states,
    )
    assert "MY PIN." in dst.read_text()
    assert "Upstream shared body." in dst.read_text()
    base1 = r1.new_base
    states1 = r1.new_span_states or {}

    # Second install with NO new edits: must be a no-op, no phantom conflict,
    # pinned region byte-stable.
    r2 = copy_atomic(
        src,
        dst,
        disposition=Disposition.SHARED,
        base_text=base1,
        spans=[SpanEntry(anchor="## Pinned", kind=SpanKind.PINNED)],
        span_states=states1,
    )
    assert r2.merge_conflicts == []
    assert "MY PIN." in dst.read_text()


def test_forked_span_merges_upstream_through_copy_atomic(tmp_path: Path) -> None:
    # Live leaves the forked region at base; upstream edits it. A forked
    # span has no merge override, so the upstream edit must flow through
    # (unlike a pinned span, which would keep live).
    live = _BASE  # unchanged forked region
    tracked = _BASE.replace("Original pinned body.", "Forked upstream body.")
    src = tmp_path / "src.md"
    src.write_text(tracked)
    dst = tmp_path / "dst.md"
    dst.write_text(live)
    state = _state_for(live, "## Pinned")

    copy_atomic(
        src,
        dst,
        disposition=Disposition.SHARED,
        base_text=_BASE,
        spans=[SpanEntry(anchor="## Pinned", kind=SpanKind.FORKED)],
        span_states={"## Pinned": state},
    )
    # Forked span has no merge override -> upstream wins.
    assert "Forked upstream body." in dst.read_text()
