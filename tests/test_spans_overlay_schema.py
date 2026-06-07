"""Schema tests for the markerless OVERLAY span kind + body payload.

An OVERLAY :class:`~setforge.spans.SpanEntry` carries a structured
:data:`~setforge.anchors.Anchor` splice point plus exactly one of ``body``
(inline) / ``body_file`` (path). The payload is present iff
``kind == overlay``; a pinned/forked span MUST NOT carry it, and an OVERLAY
span MUST. OVERLAY is markdown-only.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from setforge.errors import ConfigError
from setforge.spans import (
    SpanEntry,
    SpanKind,
    SpanSemantics,
    validate_spans_file_type,
)


def test_overlay_kind_value() -> None:
    assert SpanKind.OVERLAY.value == "overlay"


def test_overlay_span_parses_with_inline_body() -> None:
    entry = SpanEntry.model_validate(
        {
            "anchor": "## My Tweaks",
            "kind": "overlay",
            "semantics": "host-local",
            "overlay": {
                "anchor": {"kind": "after-heading", "value": "Notes"},
                "body": "host-local body",
            },
        }
    )
    assert entry.kind is SpanKind.OVERLAY
    assert entry.semantics is SpanSemantics.HOST_LOCAL
    assert entry.overlay is not None
    assert entry.overlay.body == "host-local body"


def test_overlay_span_parses_with_body_file() -> None:
    entry = SpanEntry.model_validate(
        {
            "anchor": "## X",
            "kind": "overlay",
            "overlay": {
                "anchor": {"kind": "at-end-of-file"},
                "body_file": "snippets/x.md",
            },
        }
    )
    assert entry.overlay is not None
    assert entry.overlay.body_file == Path("snippets/x.md")


def test_overlay_requires_payload() -> None:
    with pytest.raises(ValidationError):
        SpanEntry.model_validate({"anchor": "## X", "kind": "overlay"})


def test_non_overlay_rejects_payload() -> None:
    with pytest.raises(ValidationError):
        SpanEntry.model_validate(
            {
                "anchor": "## X",
                "kind": "pinned",
                "overlay": {
                    "anchor": {"kind": "at-end-of-file"},
                    "body": "no",
                },
            }
        )


def test_overlay_payload_rejects_both_body_sources() -> None:
    with pytest.raises(ValidationError):
        SpanEntry.model_validate(
            {
                "anchor": "## X",
                "kind": "overlay",
                "overlay": {
                    "anchor": {"kind": "at-end-of-file"},
                    "body": "a",
                    "body_file": "b.md",
                },
            }
        )


def test_overlay_payload_rejects_neither_body_source() -> None:
    with pytest.raises(ValidationError):
        SpanEntry.model_validate(
            {
                "anchor": "## X",
                "kind": "overlay",
                "overlay": {"anchor": {"kind": "at-end-of-file"}},
            }
        )


def test_validate_overlay_rejects_structural_file() -> None:
    overlay_span = SpanEntry.model_validate(
        {
            "anchor": "## X",
            "kind": "overlay",
            "overlay": {
                "anchor": {"kind": "after-heading", "value": "Notes"},
                "body": "b",
            },
        }
    )
    with pytest.raises(ConfigError):
        validate_spans_file_type("settings.json", [overlay_span], Path("settings.json"))
