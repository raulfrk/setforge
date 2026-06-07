"""Deploy compose: markerless host-local OVERLAY on the preserve branch (14.17).

A ``preserve_user_sections`` markdown file (claude_md) whose host-local
sections have been migrated to markerless OVERLAY spans must deploy with EVERY
host-local marker pair stripped and each body injected exactly ONCE, markerless.

The load-bearing correctness case is the projection-fed double-injection trap:
``source._host_local_sections_for_overlay`` projects OVERLAY spans back INTO the
``host_local_sections`` map (for capture / compare / promote). On the deploy
preserve path those names must NOT also reach ``host_local_inject.inject_all``
(which injects WITH markers) — else the body lands twice.
"""

from __future__ import annotations

from setforge.deploy import _legacy_only_host_local
from setforge.source import AnchorAtEndOfFile, HostLocalSection, HostLocalSectionName
from setforge.spans import OverlaySpanPayload, SpanEntry, SpanKind


def _overlay_span(identity: str, body: str) -> SpanEntry:
    """An OVERLAY span: ``identity`` is the heading-shaped sidecar key, body at EOF."""
    return SpanEntry(
        anchor=identity,
        kind=SpanKind.OVERLAY,
        overlay=OverlaySpanPayload(anchor=AnchorAtEndOfFile(), body=body),
    )


def _projected_section(body: str) -> HostLocalSection:
    """Mirror the projection: HostLocalSection carrying the overlay's EOF anchor."""
    return HostLocalSection(anchor=AnchorAtEndOfFile(), body=body, body_file=None)


# --- Task 1: _legacy_only_host_local filter -------------------------------


def test_legacy_only_excludes_overlay_anchor_names() -> None:
    host_local = {
        HostLocalSectionName("## Python"): _projected_section("## Python\n\nuv\n"),
        HostLocalSectionName("## Legacy"): _projected_section("## Legacy\n\nold\n"),
    }
    spans = [_overlay_span("## Python", "## Python\n\nuv\n")]
    out = _legacy_only_host_local(host_local, spans)
    assert out is not None
    assert set(out) == {HostLocalSectionName("## Legacy")}


def test_legacy_only_none_or_no_spans_is_identity() -> None:
    assert _legacy_only_host_local(None, None) is None
    hl = {HostLocalSectionName("## X"): _projected_section("## X\n\nb\n")}
    assert _legacy_only_host_local(hl, None) == hl
    assert _legacy_only_host_local(hl, []) == hl
