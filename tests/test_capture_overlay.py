"""Capture-side leak gate for markerless OVERLAY spans.

The capture half of the canonical leak gate: a live file carrying an
injected host-local body is captured back to tracked, and the body NEVER
reaches the tracked src nor the re-baselined stored base. Overlay capture
owns its excise (exact recorded bytes), never the leaky
exclude_spans_for_capture pass-through.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from setforge import base_store, spans_store
from setforge.capture import CaptureAction, _capture_disposition_file
from setforge.config import Disposition
from setforge.overlay_inject import (
    OverlayAmbiguousError,
    canonical_body,
    inject_body_at_anchor,
)
from setforge.source import AnchorAfterHeading
from setforge.spans import OverlaySpanPayload, SpanEntry, SpanKind
from setforge.spans_store import SpanState

_PROFILE = "p"
_SUB_NAME = "notes/CLAUDE.md"
_TRACKED = "# Title\n\n## Notes\n\nshared body\n"


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))


def _overlay(body: str = "HOST LOCAL ONLY") -> SpanEntry:
    return SpanEntry(
        anchor="## Notes",
        kind=SpanKind.OVERLAY,
        overlay=OverlaySpanPayload(anchor=AnchorAfterHeading(value="Notes"), body=body),
    )


def _seed_state(body: str) -> None:
    st = SpanState(
        anchor="## Notes",
        fingerprint="a" * 64,
        prefix=[],
        suffix=[],
        position_hint_start_line=3,
        position_hint_n_lines=1,
        heading_level=2,
        last_deployed_body=canonical_body(body),
    )
    spans_store.set_states(_PROFILE, _SUB_NAME, {"## Notes": st})


def test_capture_excises_body_from_tracked_and_base(tmp_path: Path) -> None:
    src = tmp_path / "src.md"
    src.write_text(_TRACKED)
    dst = tmp_path / "dst.md"
    # Live carries the injected body (what deploy produced).
    body = canonical_body("HOST LOCAL ONLY")
    dst.write_text(
        inject_body_at_anchor(_TRACKED, AnchorAfterHeading(value="Notes"), body)
    )
    _seed_state("HOST LOCAL ONLY")

    result = _capture_disposition_file(
        _SUB_NAME,
        src,
        dst,
        disposition=Disposition.SHARED,
        profile=_PROFILE,
        spans=[_overlay()],
    )
    assert result.action in (CaptureAction.UPDATED, CaptureAction.NOOP)
    # LEAK GATE: tracked src is body-free.
    assert "HOST LOCAL ONLY" not in src.read_text()
    assert src.read_text() == _TRACKED
    # LEAK GATE: stored base is body-free.
    stored_base = base_store.read_base(_PROFILE, _SUB_NAME)
    assert stored_base is not None
    assert b"HOST LOCAL ONLY" not in stored_base


def test_capture_ambiguous_body_refuses(tmp_path: Path) -> None:
    body = canonical_body("DUP")
    src = tmp_path / "src.md"
    src.write_text(_TRACKED)
    dst = tmp_path / "dst.md"
    dst.write_text(f"x\n{body}y\n{body}z\n")
    _seed_state("DUP")
    with pytest.raises(OverlayAmbiguousError):
        _capture_disposition_file(
            _SUB_NAME,
            src,
            dst,
            disposition=Disposition.SHARED,
            profile=_PROFILE,
            spans=[_overlay("DUP")],
        )
