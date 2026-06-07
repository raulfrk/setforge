"""Schema-layer tests for the sub-file span model.

Covers the :class:`setforge.spans.SpanEntry` value object (kind +
semantics + anchor), the :data:`setforge.spans.SpanKind` /
:data:`setforge.spans.SpanSemantics` closed sets, the per-file-type
anchor validator, and the ``spans:`` field on both
:class:`setforge.source._LocalTrackedFileOverlay` (host-local intent)
and :class:`setforge.config.TrackedFile` (shared intent).
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from setforge.config import TrackedFile
from setforge.errors import ConfigError
from setforge.source import _LocalTrackedFileOverlay
from setforge.spans import (
    SpanEntry,
    SpanKind,
    SpanSemantics,
    validate_spans_file_type,
)


def test_span_kind_values() -> None:
    assert SpanKind.PINNED.value == "pinned"
    assert SpanKind.FORKED.value == "forked"


def test_span_semantics_values() -> None:
    assert SpanSemantics.HOST_LOCAL.value == "host-local"
    assert SpanSemantics.SHARED.value == "shared"


def test_span_entry_parses_minimal() -> None:
    entry = SpanEntry.model_validate({"anchor": "## My Tweaks"})
    assert entry.anchor == "## My Tweaks"
    # Defaults: pinned + host-local (the siblings rely on both being
    # representable but the common case is a host-local pin).
    assert entry.kind is SpanKind.PINNED
    assert entry.semantics is SpanSemantics.HOST_LOCAL


def test_span_entry_parses_full() -> None:
    entry = SpanEntry.model_validate(
        {"anchor": "## Shared", "kind": "forked", "semantics": "shared"}
    )
    assert entry.kind is SpanKind.FORKED
    assert entry.semantics is SpanSemantics.SHARED


def test_span_entry_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        SpanEntry.model_validate({"anchor": "## X", "bogus": 1})


def test_span_entry_rejects_empty_anchor() -> None:
    with pytest.raises(ValidationError):
        SpanEntry.model_validate({"anchor": "   "})


def test_overlay_accepts_spans() -> None:
    overlay = _LocalTrackedFileOverlay.model_validate(
        {"spans": [{"anchor": "## My Tweaks", "kind": "pinned"}]}
    )
    assert len(overlay.spans) == 1
    assert overlay.spans[0].kind is SpanKind.PINNED


def test_overlay_rejects_unknown_key_with_spans_present() -> None:
    with pytest.raises(ValidationError):
        _LocalTrackedFileOverlay.model_validate({"spans": [], "nope": 1})


def test_tracked_file_accepts_spans() -> None:
    tf = TrackedFile.model_validate(
        {
            "src": "claude/CLAUDE.md",
            "dst": "~/.claude/CLAUDE.md",
            "spans": [{"anchor": "## Shared", "semantics": "shared"}],
        }
    )
    assert tf.spans[0].semantics is SpanSemantics.SHARED


def test_validate_spans_file_type_allows_markdown() -> None:
    # Heading-text anchor on a markdown file is fine.
    validate_spans_file_type(
        "claude/CLAUDE.md",
        [SpanEntry.model_validate({"anchor": "## Foo"})],
        Path("claude/CLAUDE.md"),
    )


def test_validate_spans_file_type_rejects_non_markdown() -> None:
    with pytest.raises(ConfigError):
        validate_spans_file_type(
            "settings.json",
            [SpanEntry.model_validate({"anchor": "## Foo"})],
            Path("settings.json"),
        )


def test_validate_spans_file_type_noop_when_empty() -> None:
    # No spans declared: never raises, even on a non-markdown file.
    validate_spans_file_type("settings.json", [], Path("settings.json"))


def test_validate_spans_file_type_overlay_identity_exempt_from_heading_shape() -> None:
    # An OVERLAY span's top-level anchor is the sidecar IDENTITY, not the
    # splice point (that is overlay.anchor). A non-heading-shaped identity
    # (e.g. a retired host_local_sections.<name> key) is legal on markdown.
    validate_spans_file_type(
        "claude/CLAUDE.md",
        [
            SpanEntry.model_validate(
                {
                    "anchor": "my-notes",
                    "kind": "overlay",
                    "overlay": {
                        "anchor": {"kind": "after-heading", "value": "Notes"},
                        "body": "host-local body",
                    },
                }
            )
        ],
        Path("claude/CLAUDE.md"),
    )
