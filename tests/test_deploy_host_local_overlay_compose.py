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

from pathlib import Path

from setforge import capture, deploy
from setforge.deploy import _legacy_only_host_local
from setforge.source import AnchorAtEndOfFile, HostLocalSection, HostLocalSectionName
from setforge.spans import OverlaySpanPayload, SpanEntry, SpanKind

_HASH = "a" * 64  # any well-formed sha256-hex passes the strict tracked-side parse;
# maintain_marker_hashes rewrites it before the de-marker strip removes the pair.


def _placeholder(name: str) -> str:
    """An empty tracked-authored host-local marker pair (the de-marker target).

    The tracked source is parsed strictly (``allow_legacy=False``), so the end
    marker MUST carry a well-formed ``hash=`` segment — mirroring a real
    install-stamped tracked file.
    """
    return (
        f"<!-- setforge:user-section start host-local {name} -->\n"
        f"<!-- setforge:user-section end host-local {name} hash={_HASH} -->\n"
    )


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


# --- Task 2: compose strip + overlay-inject in copy_atomic preserve branch ---


def test_copy_atomic_preserve_overlay_injects_once_markerless(tmp_path: Path) -> None:
    # tracked src: two host-local placeholder pairs (one migrated, one empty-drop).
    src = tmp_path / "CLAUDE.md"
    src.write_text(
        "# Title\n\n" + _placeholder("python") + "\n" + _placeholder("workctx")
    )
    dst = tmp_path / "live" / "CLAUDE.md"
    dst.parent.mkdir()
    dst.write_text("# Title\n")  # markerless live (post first-install)

    body = "## Python\n\nuse uv\n"
    result = deploy.copy_atomic(
        src,
        dst,
        preserve_user_sections=True,
        # host_local_sections is the PROJECTION — already contains the overlay name.
        host_local_sections={
            HostLocalSectionName("## Python"): _projected_section(body)
        },
        spans=[_overlay_span("## Python", body)],
        span_states={},
    )
    out = dst.read_text()
    assert "setforge:user-section" not in out  # every host-local marker stripped
    assert out.count("## Python") == 1  # injected exactly once, markerless
    assert "use uv" in out
    assert result.new_span_states is not None
    assert "## Python" in result.new_span_states


# --- Task 4: projection-fed double-injection regression guard ---------------


def test_projection_fed_overlay_never_double_injects(tmp_path: Path) -> None:
    """The loader's projection must NOT cause the body to inject twice.

    ``source._host_local_sections_for_overlay`` projects the OVERLAY span back
    INTO the ``host_local_sections`` map; if that name reached ``inject_all`` the
    body would land WITH markers there AND markerless via ``inject_overlay_bodies``.
    Named regression so a future projection / inject change re-trips it.
    """
    src = tmp_path / "CLAUDE.md"
    src.write_text("# T\n\n" + _placeholder("python"))
    dst = tmp_path / "live.md"
    dst.write_text("# T\n")
    body = "## Python\n\nbody\n"
    deploy.copy_atomic(
        src,
        dst,
        preserve_user_sections=True,
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
    live WITHOUT markers, so the name-scoped marker strip can't see it. Capture
    must excise it by its exact recorded bytes before the section merge, else
    ``sync`` would bake the per-host body into the shared tracked source.
    """
    body = "## Python\n\nuse uv\n"
    src = tmp_path / "CLAUDE.md"  # tracked (shared) — body must NEVER land here
    src.write_text("# Title\n")
    dst = tmp_path / "live.md"  # live — body present markerless (post-deploy)
    dst.write_text("# Title\n" + body)

    capture.capture_tracked_file(
        src,
        dst,
        preserve_user_sections=True,
        preserve_user_keys=[],
        spans=[_overlay_span("## Python", body)],
        span_states={},
    )
    out = src.read_text()
    assert "use uv" not in out  # body excised, not leaked into tracked
    assert "setforge:user-section" not in out
