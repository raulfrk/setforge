"""Unit tests for :func:`setforge.spans.validate_span_disposition`.

A ``pinned``/``forked`` span is consumed only on the disposition merge path;
on a ``disposition: None`` file it is silently ignored on deploy and not
excluded on capture (host-local leak). The guard rejects exactly that, while
exempting ``overlay`` spans (the markerless host-local-body mechanism that
runs on the disposition=None path) and allowing any non-None disposition.
"""

from __future__ import annotations

import pytest

from setforge.config import Disposition
from setforge.errors import ConfigError
from setforge.source import AnchorAfterHeading
from setforge.spans import (
    OverlaySpanPayload,
    SpanEntry,
    SpanKind,
    validate_span_disposition,
)


def _pinned(anchor: str = "## Tweaks") -> SpanEntry:
    return SpanEntry(anchor=anchor, kind=SpanKind.PINNED)


def _forked(anchor: str = "editor.fontSize") -> SpanEntry:
    return SpanEntry(anchor=anchor, kind=SpanKind.FORKED)


def _overlay(anchor: str = "## Notes") -> SpanEntry:
    return SpanEntry(
        anchor=anchor,
        kind=SpanKind.OVERLAY,
        overlay=OverlaySpanPayload(
            anchor=AnchorAfterHeading(value="Notes"), body="host body"
        ),
    )


def test_pinned_span_with_none_disposition_raises() -> None:
    with pytest.raises(ConfigError, match="disposition is None"):
        validate_span_disposition("d", [_pinned()], None)


def test_forked_span_with_none_disposition_raises() -> None:
    with pytest.raises(ConfigError, match="disposition is None"):
        validate_span_disposition("d", [_forked()], None)


@pytest.mark.parametrize(
    "disp", [Disposition.SHARED, Disposition.FORKED, Disposition.PINNED]
)
def test_pinned_span_with_any_disposition_ok(disp: Disposition) -> None:
    # pinned-disposition is redundant-but-safe; only None is the leak path,
    # and rejecting `pinned` would break authored configs (local.yaml.bak).
    validate_span_disposition("d", [_pinned()], disp)


def test_overlay_span_exempt_even_without_disposition() -> None:
    # OVERLAY is the host-local-body mechanism; it works on disposition=None.
    validate_span_disposition("d", [_overlay()], None)


def test_overlay_among_pinned_still_rejects_the_pinned() -> None:
    with pytest.raises(ConfigError, match="disposition is None"):
        validate_span_disposition("d", [_overlay(), _pinned()], None)


def test_empty_spans_ok() -> None:
    validate_span_disposition("d", [], None)


def test_message_names_tracked_file_anchor_and_fix() -> None:
    with pytest.raises(ConfigError) as exc:
        validate_span_disposition("myfile", [_pinned("## My Heading")], None)
    msg = str(exc.value)
    assert "'myfile'" in msg
    assert "## My Heading" in msg
    assert "disposition: shared|forked|pinned" in msg
    assert "overlay" in msg
