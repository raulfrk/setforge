"""Compare-overlay unit tests for markerless host-local injection.

Asserts that :func:`setforge.compare.diff_file` excises injected host-local
OVERLAY bodies from the live side so an already-deployed live file does NOT
report drift on the next ``setforge compare`` — the symmetric read of deploy's
markerless overlay inject (post marker-retire migration).
"""

from __future__ import annotations

from pathlib import Path

from setforge.compare import diff_file
from setforge.deploy import copy_atomic
from setforge.source import AnchorAfterHeading
from setforge.spans import OverlaySpanPayload, SpanEntry, SpanKind


def _write_src(tmp_path: Path, content: str) -> Path:
    src = tmp_path / "src.md"
    src.write_text(content, encoding="utf-8")
    return src


def _overlay_span(identity: str, heading: str, body: str) -> SpanEntry:
    """A host-local OVERLAY span splicing ``body`` after ``heading``."""
    return SpanEntry(
        anchor=identity,
        kind=SpanKind.OVERLAY,
        overlay=OverlaySpanPayload(anchor=AnchorAfterHeading(value=heading), body=body),
    )


def test_compare_no_drift_after_host_local_install(tmp_path: Path) -> None:
    src = _write_src(tmp_path, "# Title\n\n## Workflow\n\nbody\n")
    dst = tmp_path / "dst.md"
    span = _overlay_span("work-overrides", "Workflow", "WORK OVERRIDES\n")
    copy_atomic(src, dst, spans=[span], span_states={})
    # diff_file excises the overlay body from live, so the deployed file matches
    # the markerless tracked source — no drift.
    diff = diff_file(src, dst, overlay_spans=[span])
    assert diff == ""


def test_compare_without_overlay_arg_shows_drift_for_injected_body(
    tmp_path: Path,
) -> None:
    """Sanity-check: without the overlay spans, a deployed live file DOES surface
    drift — confirms the overlay-aware path actively excises it (rather than
    masking unconditionally).
    """
    src = _write_src(tmp_path, "# Title\n\n## Workflow\n\nbody\n")
    dst = tmp_path / "dst.md"
    span = _overlay_span("work-overrides", "Workflow", "WORK OVERRIDES\n")
    copy_atomic(src, dst, spans=[span], span_states={})
    diff_no_arg = diff_file(src, dst)
    assert diff_no_arg != ""
    assert "WORK OVERRIDES" in diff_no_arg


def test_compare_with_extra_tracked_drift_still_reports(tmp_path: Path) -> None:
    """When the tracked content drifts (independent of host-local), compare still
    reports the drift body — the overlay excise must be SCOPED to the injected
    body, not a blanket diff suppression.
    """
    src = _write_src(tmp_path, "# Title\n\n## Workflow\n\nbody\n")
    dst = tmp_path / "dst.md"
    span = _overlay_span("work-overrides", "Workflow", "WORK\n")
    copy_atomic(src, dst, spans=[span], span_states={})
    # Modify the live file's NON-injected content to introduce real drift.
    text = dst.read_text(encoding="utf-8")
    dst.write_text(text.replace("body\n", "MUTATED BODY\n"), encoding="utf-8")
    diff = diff_file(src, dst, overlay_spans=[span])
    assert diff != ""
    assert "MUTATED" in diff
