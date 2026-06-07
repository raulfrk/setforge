"""Tests for the overlay-body edit wizard (hand-edited deployed body).

When a deployed host-local OVERLAY body is hand-edited in the live file,
the exact-bytes excise misses, but the body is still fuzzy-locatable near
its anchor. The wizard offers keep (write the edit to local.yaml's spans
body) / discard (re-impose canonical next deploy) / skip. The keep path
NEVER writes tracked — only local.yaml.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from setforge.errors import InvalidLocalConfigShape
from setforge.overlay_body_wizard import (
    OverlayBodyEdit,
    OverlayEditChoice,
    detect_overlay_body_edit,
    resolve_auto,
    write_edited_body_to_local,
)
from setforge.overlay_inject import canonical_body, inject_body_at_anchor
from setforge.source import AnchorAfterHeading
from setforge.spans import OverlaySpanPayload, SpanEntry, SpanKind
from setforge.spans_store import SpanState

_TRACKED = "# Title\n\n## Notes\n\nshared body\n"


def _overlay(body: str = "ORIGINAL BODY") -> SpanEntry:
    return SpanEntry(
        anchor="## Notes",
        kind=SpanKind.OVERLAY,
        overlay=OverlaySpanPayload(anchor=AnchorAfterHeading(value="Notes"), body=body),
    )


def _state(body: str) -> SpanState:
    return SpanState(
        anchor="## Notes",
        fingerprint="a" * 64,
        prefix=["## Notes", ""],
        suffix=[],
        position_hint_start_line=3,
        position_hint_n_lines=1,
        heading_level=2,
        last_deployed_body=canonical_body(body),
    )


def test_detect_returns_none_when_body_unchanged() -> None:
    live = inject_body_at_anchor(
        _TRACKED, AnchorAfterHeading(value="Notes"), canonical_body("ORIGINAL BODY")
    )
    edit = detect_overlay_body_edit(live, _overlay(), _state("ORIGINAL BODY"))
    assert edit is None


def test_detect_finds_hand_edited_body() -> None:
    live = inject_body_at_anchor(
        _TRACKED, AnchorAfterHeading(value="Notes"), canonical_body("EDITED BODY HERE")
    )
    edit = detect_overlay_body_edit(live, _overlay(), _state("ORIGINAL BODY"))
    assert edit is not None
    assert "EDITED BODY HERE" in edit.live_body
    assert edit.canonical_body == canonical_body("ORIGINAL BODY")


def test_resolve_auto_maps_keep_live_and_use_tracked() -> None:
    from setforge.capture import CaptureAuto

    assert resolve_auto(CaptureAuto.USE_LIVE) is OverlayEditChoice.KEEP
    assert resolve_auto(CaptureAuto.KEEP_TRACKED) is OverlayEditChoice.DISCARD
    assert resolve_auto(None) is None


def test_write_edited_body_to_local_only_touches_local_yaml(tmp_path: Path) -> None:
    local = tmp_path / "local.yaml"
    local.write_text(
        "tracked_files:\n"
        "  notes:\n"
        "    spans:\n"
        "      - anchor: '## Notes'\n"
        "        kind: overlay\n"
        "        overlay:\n"
        "          anchor:\n"
        "            kind: after-heading\n"
        "            value: Notes\n"
        "          body: ORIGINAL BODY\n",
        encoding="utf-8",
    )
    edit = OverlayBodyEdit(
        tracked_file_id="notes",
        anchor="## Notes",
        canonical_body=canonical_body("ORIGINAL BODY"),
        live_body=canonical_body("EDITED BODY HERE"),
    )
    write_edited_body_to_local(edit, local_config_path=local)
    text = local.read_text(encoding="utf-8")
    assert "EDITED BODY HERE" in text
    assert "ORIGINAL BODY" not in text


def test_write_edited_body_missing_span_raises(tmp_path: Path) -> None:
    local = tmp_path / "local.yaml"
    local.write_text("tracked_files: {}\n", encoding="utf-8")
    edit = OverlayBodyEdit(
        tracked_file_id="absent",
        anchor="## Notes",
        canonical_body="x\n",
        live_body="y\n",
    )
    with pytest.raises(InvalidLocalConfigShape):
        write_edited_body_to_local(edit, local_config_path=local)
