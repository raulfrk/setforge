"""Deploy compose: markerless host-local OVERLAY on the preserve branch.

A ``preserve_user_sections`` markdown file (claude_md) whose host-local sections
are markerless OVERLAY spans must deploy with each body injected exactly ONCE,
markerless. After the marker-retire migration the tracked source carries NO
host-local marker pairs at all — host-local content lives only as OVERLAY spans
in local.yaml — so deploy simply splices each overlay body at its anchor.

The load-bearing correctness case is the projection-fed double-injection trap:
``source._host_local_sections_for_overlay`` projects OVERLAY spans back INTO the
``host_local_sections`` map (for capture / compare / promote); the deploy
preserve path must inject each body once via the markerless overlay injector,
never twice.
"""

from __future__ import annotations

from pathlib import Path

from setforge import capture, deploy
from setforge.source import AnchorAtEndOfFile, HostLocalSection, HostLocalSectionName
from setforge.span_types import OverlaySpanPayload, SpanEntry, SpanKind


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


# --- compose overlay-inject in copy_atomic preserve branch ------------------


def test_copy_atomic_preserve_overlay_injects_once_markerless(tmp_path: Path) -> None:
    # tracked src is markerless (post marker-retire migration) — no host-local
    # marker pairs remain to strip; only the overlay body is spliced in.
    src = tmp_path / "CLAUDE.md"
    src.write_text("# Title\n")
    dst = tmp_path / "live" / "CLAUDE.md"
    dst.parent.mkdir()
    dst.write_text("# Title\n")  # markerless live (post first-install)

    body = "## Python\n\nuse uv\n"
    result = deploy.copy_atomic(
        src,
        dst,
        # host_local_sections is the PROJECTION — already contains the overlay name.
        host_local_sections={
            HostLocalSectionName("## Python"): _projected_section(body)
        },
        spans=[_overlay_span("## Python", body)],
        span_states={},
    )
    out = dst.read_text()
    assert "setforge:user-section" not in out  # never emits a marker
    assert out.count("## Python") == 1  # injected exactly once, markerless
    assert "use uv" in out
    assert result.new_span_states is not None
    assert "## Python" in result.new_span_states


def test_projection_fed_overlay_never_double_injects(tmp_path: Path) -> None:
    """The loader's projection must NOT cause the body to inject twice.

    ``source._host_local_sections_for_overlay`` projects the OVERLAY span back
    INTO the ``host_local_sections`` map; the deploy preserve path injects each
    body once via the markerless overlay injector and never re-emits it. Named
    regression so a future projection / inject change re-trips it.
    """
    src = tmp_path / "CLAUDE.md"
    src.write_text("# T\n")
    dst = tmp_path / "live.md"
    dst.write_text("# T\n")
    body = "## Python\n\nbody\n"
    deploy.copy_atomic(
        src,
        dst,
        # The PROJECTION already contains the overlay name (post-migration shape).
        host_local_sections={
            HostLocalSectionName("## Python"): _projected_section(body)
        },
        spans=[_overlay_span("## Python", body)],
        span_states={},
    )
    out = dst.read_text()
    assert out.count("## Python") == 1  # exactly once, never doubled
    assert out.count("body") == 1
    assert "setforge:user-section" not in out


# --- Capture symmetry: markerless overlay body must NOT leak into tracked ----


def test_capture_tracked_file_excises_markerless_overlay_body(tmp_path: Path) -> None:
    """Capture strips a markerless host-local overlay body — never leaks it to tracked.

    Symmetric to the deploy inject: ``install`` writes the host-local body into
    live WITHOUT markers, so capture must excise it by its exact recorded bytes
    before the section merge, else ``sync`` would bake the per-host body into the
    shared tracked source.
    """
    body = "## Python\n\nuse uv\n"
    src = tmp_path / "CLAUDE.md"  # tracked (shared) — body must NEVER land here
    src.write_text("# Title\n")
    dst = tmp_path / "live.md"  # live — body present markerless (post-deploy)
    dst.write_text("# Title\n" + body)

    capture.capture_tracked_file(
        src,
        dst,
        spans=[_overlay_span("## Python", body)],
        span_states={},
    )
    out = src.read_text()
    assert "use uv" not in out  # body excised, not leaked into tracked
    assert "setforge:user-section" not in out
