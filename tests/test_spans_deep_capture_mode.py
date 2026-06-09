"""Tests for the 2.0 span-model expansion: ``deep`` + ``capture_mode``.

The full-parity contract adds two attributes to :class:`setforge.spans.SpanEntry`
so the legacy ``preserve_user_keys_deep`` (deep-recursive merge) and
``preserve_user_sections_mode`` (re-splice vs strip) forms translate losslessly:

- ``deep: bool`` — a PINNED structural span whose live → merge re-assert
  DEEP-merges instead of whole-replacing (tracked-only sub-keys survive).
- ``capture_mode: SectionMode`` — carries the section re-splice vs strip mode for
  section spans.

Both land at schema 2.0 only. The validators reject mis-applied attrs (deep on an
OVERLAY span or a markdown heading anchor); ``capture_mode`` on a non-section span
is accept-and-ignore (no raise) — it is degenerate there, like the legacy mode
flag was degenerate without ``preserve_user_sections``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from setforge.anchors import AnchorAfterHeading
from setforge.disposition_merge import resolve_file
from setforge.section_mode import SectionMode
from setforge.spans import OverlaySpanPayload, SpanEntry, SpanKind

# ---------------------------------------------------------------------------
# SectionMode leaf-module + back-compat re-export (cycle resolution).
# ---------------------------------------------------------------------------


def test_section_mode_reexported_from_config() -> None:
    """``SectionMode`` re-exports from config for back-compat after the move."""
    from setforge.config import SectionMode as ConfigSectionMode

    assert ConfigSectionMode is SectionMode


# ---------------------------------------------------------------------------
# Field defaults + validators.
# ---------------------------------------------------------------------------


def test_deep_defaults_false() -> None:
    """``deep`` defaults to False (the shallow whole-leaf re-assert)."""
    span = SpanEntry(anchor="editor.fontSize", kind=SpanKind.PINNED)
    assert span.deep is False


def test_capture_mode_defaults_keep_defaults() -> None:
    """``capture_mode`` defaults to KEEP_DEFAULTS (non-destructive re-splice)."""
    span = SpanEntry(anchor="## Tweaks", kind=SpanKind.PINNED)
    assert span.capture_mode is SectionMode.KEEP_DEFAULTS


def test_deep_accepted_on_structural_pinned_span() -> None:
    """``deep=True`` is accepted on a structural dotted-path PINNED span."""
    span = SpanEntry(anchor="editor", kind=SpanKind.PINNED, deep=True)
    assert span.deep is True


def test_deep_rejected_on_overlay_kind() -> None:
    """``deep=True`` on an OVERLAY span is a validation error."""
    with pytest.raises(ValidationError, match="deep"):
        SpanEntry(
            anchor="my-note",
            kind=SpanKind.OVERLAY,
            deep=True,
            overlay=OverlaySpanPayload(
                anchor=AnchorAfterHeading(value="Notes"), body="x"
            ),
        )


def test_deep_rejected_on_heading_anchor() -> None:
    """``deep=True`` on a markdown heading-shaped anchor is a validation error."""
    with pytest.raises(ValidationError, match="deep"):
        SpanEntry(anchor="## Tweaks", kind=SpanKind.PINNED, deep=True)


def test_strip_capture_mode_on_structural_span_does_not_raise() -> None:
    """STRIP capture_mode on a non-section (structural) span is accept-and-ignore."""
    span = SpanEntry(
        anchor="editor.fontSize",
        kind=SpanKind.PINNED,
        capture_mode=SectionMode.STRIP,
    )
    assert span.capture_mode is SectionMode.STRIP


# ---------------------------------------------------------------------------
# Task 2: deep-merge re-assert for deep PINNED spans (driven via resolve_file).
# ---------------------------------------------------------------------------


def test_deep_pinned_span_deep_merges_live_over_tracked() -> None:
    """A deep PINNED span keeps a tracked-only sub-key + takes live's edited key.

    ``theme`` is added on the tracked side (absent in base + live); the 3-way
    merge keeps that tracked add. A deep re-assert merges live's
    ``fontSize: 14`` over the merged subtree WITHOUT dropping the merged-in
    ``theme`` (deep-merge: tracked-only sub-keys survive).
    """
    from setforge.config import Disposition

    base = "editor:\n  fontSize: 12\n"
    tracked = "editor:\n  fontSize: 12\n  theme: dark\n"
    live = "editor:\n  fontSize: 14\n"
    span = SpanEntry(anchor="editor", kind=SpanKind.PINNED, deep=True)
    result = resolve_file(
        disposition=Disposition.SHARED,
        dst=Path("settings.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
        structural_spans=[span],
    )
    assert "fontSize: 14" in result.text
    assert "theme: dark" in result.text


def test_deep_false_pinned_span_whole_replaces_with_live() -> None:
    """A shallow (deep=False) PINNED span whole-replaces the subtree with live.

    Same setup as the deep case, but ``deep=False``: the re-assert
    whole-replaces the merged subtree with live's snapshot ``{fontSize: 14}``,
    so the merged-in tracked-only ``theme`` is NOT preserved (live wins
    wholesale).
    """
    from setforge.config import Disposition

    base = "editor:\n  fontSize: 12\n"
    tracked = "editor:\n  fontSize: 12\n  theme: dark\n"
    live = "editor:\n  fontSize: 14\n"
    span = SpanEntry(anchor="editor", kind=SpanKind.PINNED, deep=False)
    result = resolve_file(
        disposition=Disposition.SHARED,
        dst=Path("settings.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
        structural_spans=[span],
    )
    assert "fontSize: 14" in result.text
    # Whole-replace: tracked's theme is NOT preserved (live wins wholesale).
    assert "theme" not in result.text
