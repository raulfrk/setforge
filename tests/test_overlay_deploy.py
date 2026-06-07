"""Unit tests for the OVERLAY deploy helper (excise-before / inject-after).

These exercise :mod:`setforge.overlay_deploy` directly: the pre-merge
excise that strips a host-local body from live by its exact recorded bytes,
and the post-merge inject that re-imposes the canonical body. The deploy
seam (:func:`setforge.deploy.copy_atomic`) composes these; the leak gate
ultimately rests on them.
"""

from __future__ import annotations

import pytest

from setforge.overlay_deploy import (
    excise_overlay_bodies,
    inject_overlay_bodies,
)
from setforge.overlay_inject import (
    OverlayAmbiguousError,
    canonical_body,
    inject_body_at_anchor,
)
from setforge.source import AnchorAfterHeading
from setforge.spans import OverlaySpanPayload, SpanEntry, SpanKind
from setforge.spans_store import SpanState


def _overlay(anchor_id: str, value: str, body: str) -> SpanEntry:
    return SpanEntry(
        anchor=anchor_id,
        kind=SpanKind.OVERLAY,
        overlay=OverlaySpanPayload(anchor=AnchorAfterHeading(value=value), body=body),
    )


_TRACKED = "# Title\n\n## Notes\n\nshared body\n"


def test_inject_then_excise_round_trips_with_no_prior_state() -> None:
    spans = [_overlay("## Notes", "Notes", "HOST LOCAL")]
    injected, new_states = inject_overlay_bodies(_TRACKED, spans, {})
    assert "HOST LOCAL" in injected
    assert new_states["## Notes"].last_deployed_body == canonical_body("HOST LOCAL")
    # Excise by the freshly-recorded body removes it cleanly.
    body_free, _ = excise_overlay_bodies(injected, spans, new_states)
    assert "HOST LOCAL" not in body_free
    assert body_free == _TRACKED


def test_excise_prefers_last_deployed_body_over_canonical() -> None:
    # The user changed local.yaml's body; live still carries the OLD body.
    spans = [_overlay("## Notes", "Notes", "NEW BODY")]
    states = {
        "## Notes": SpanState(
            anchor="## Notes",
            fingerprint="x" * 64,
            prefix=[],
            suffix=[],
            position_hint_start_line=0,
            position_hint_n_lines=1,
            heading_level=2,
            last_deployed_body=canonical_body("OLD BODY"),
        )
    }
    # Live carries the OLD body (last_deployed_body), not the new canonical.
    live_text = inject_body_at_anchor(
        _TRACKED, AnchorAfterHeading(value="Notes"), canonical_body("OLD BODY")
    )
    body_free, _ = excise_overlay_bodies(live_text, spans, states)
    assert "OLD BODY" not in body_free
    assert body_free == _TRACKED


def test_excise_first_deploy_no_body_present_is_noop() -> None:
    spans = [_overlay("## Notes", "Notes", "HOST LOCAL")]
    body_free, found_any = excise_overlay_bodies(_TRACKED, spans, {})
    assert body_free == _TRACKED
    assert found_any is False


def test_excise_ambiguous_body_refuses() -> None:
    body = canonical_body("DUP")
    spans = [_overlay("## Notes", "Notes", "DUP")]
    text = f"a\n{body}b\n{body}c\n"
    with pytest.raises(OverlayAmbiguousError):
        excise_overlay_bodies(text, spans, {})


def test_inject_multi_section_bottom_up() -> None:
    tracked = "# T\n\n## A\n\naa\n\n## B\n\nbb\n"
    spans = [
        _overlay("## A", "A", "BODY-A"),
        _overlay("## B", "B", "BODY-B"),
    ]
    injected, new_states = inject_overlay_bodies(tracked, spans, {})
    assert "BODY-A" in injected
    assert "BODY-B" in injected
    assert set(new_states) == {"## A", "## B"}
