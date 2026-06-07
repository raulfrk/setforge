"""Deploy-level tests for markerless OVERLAY spans through copy_atomic.

The canonical LEAK GATE at the deploy seam: a heading-less host-local body
is injected into live on deploy, but the re-baselined base is body-free
(the body never reaches the stored base or tracked). Plus idempotency and
the first-deploy unconditional inject.
"""

from __future__ import annotations

from pathlib import Path

from setforge.config import Disposition
from setforge.deploy import DeployAction, copy_atomic
from setforge.overlay_inject import canonical_body
from setforge.source import AnchorAfterHeading
from setforge.spans import OverlaySpanPayload, SpanEntry, SpanKind

_TRACKED = "# Title\n\n## Notes\n\nshared body\n"


def _overlay(body: str = "HOST LOCAL ONLY") -> SpanEntry:
    return SpanEntry(
        anchor="## Notes",
        kind=SpanKind.OVERLAY,
        overlay=OverlaySpanPayload(anchor=AnchorAfterHeading(value="Notes"), body=body),
    )


def test_first_deploy_injects_body_and_base_is_body_free(tmp_path: Path) -> None:
    src = tmp_path / "src.md"
    src.write_text(_TRACKED)
    dst = tmp_path / "dst.md"  # absent: first deploy

    result = copy_atomic(
        src,
        dst,
        disposition=Disposition.SHARED,
        base_text=None,
        spans=[_overlay()],
        span_states=None,
    )
    on_disk = dst.read_text()
    assert "HOST LOCAL ONLY" in on_disk  # body injected unconditionally
    # LEAK GATE: the re-baselined base is body-free.
    assert result.new_base is not None
    assert "HOST LOCAL ONLY" not in result.new_base
    # tracked src untouched / body-free.
    assert "HOST LOCAL ONLY" not in src.read_text()
    # Sidecar records the exact injected bytes.
    assert result.new_span_states is not None
    st = result.new_span_states["## Notes"]
    assert st.last_deployed_body == canonical_body("HOST LOCAL ONLY")


def test_redeploy_is_idempotent_no_double_body(tmp_path: Path) -> None:
    src = tmp_path / "src.md"
    src.write_text(_TRACKED)
    dst = tmp_path / "dst.md"

    r0 = copy_atomic(
        src, dst, disposition=Disposition.SHARED, base_text=None, spans=[_overlay()]
    )
    states = r0.new_span_states
    r1 = copy_atomic(
        src,
        dst,
        disposition=Disposition.SHARED,
        base_text=r0.new_base,
        spans=[_overlay()],
        span_states=states,
    )
    on_disk = dst.read_text()
    assert on_disk.count("HOST LOCAL ONLY") == 1
    assert r1.action is DeployAction.NOOP
    assert r1.new_base is not None
    assert "HOST LOCAL ONLY" not in r1.new_base


def test_upstream_shared_edit_merges_under_overlay(tmp_path: Path) -> None:
    # First deploy with the body present.
    src = tmp_path / "src.md"
    src.write_text(_TRACKED)
    dst = tmp_path / "dst.md"
    r0 = copy_atomic(
        src, dst, disposition=Disposition.SHARED, base_text=None, spans=[_overlay()]
    )
    # Upstream advances the shared body; redeploy.
    src.write_text(_TRACKED.replace("shared body", "UPSTREAM shared body"))
    r1 = copy_atomic(
        src,
        dst,
        disposition=Disposition.SHARED,
        base_text=r0.new_base,
        spans=[_overlay()],
        span_states=r0.new_span_states,
    )
    on_disk = dst.read_text()
    assert "UPSTREAM shared body" in on_disk
    assert on_disk.count("HOST LOCAL ONLY") == 1
    assert r1.new_base is not None
    assert "HOST LOCAL ONLY" not in r1.new_base
