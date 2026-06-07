"""auto-promote happy-path unit tests for setforge.section_promote.

Covers the executor's atomic four-mutation contract directly (no CLI,
no PTY): set up a tempdir-rooted local.yaml + tracked.md + live.md
trio, build a :class:`PromotePlan` against a no-op secrets scanner,
call :func:`execute_promote_to_shared`, and assert each of the three
files has the expected post-promote shape.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from setforge.secrets import SecretsScanResult
from setforge.section_promote import (
    PromotePlan,
    execute_promote_to_shared,
    offer_promote,
    rewrite_live_markers_to_shared,
)
from setforge.source import (
    AnchorAfterHeading,
    AnchorKind,
    HostLocalSectionName,
)

_TRACKED_BEFORE = """\
# Demo

## Workflow

workflow body

## Trailing

trailing body
"""

_LIVE_BEFORE = """\
# Demo

## Workflow

workflow body
<!-- setforge:user-section start host-local work-overrides -->
WORK OVERRIDES BODY
<!-- setforge:user-section end host-local work-overrides \
hash=4ce29fbd1a93cd8de3cab7d3e8a5f12b6c0e35c4a9fa1217d4e2a6f0a8d3e7b9 -->

## Trailing

trailing body
"""

_LOCAL_YAML_BEFORE = """\
tracked_files:
  demo_md:
    host_local_sections:
      work-overrides:
        anchor:
          kind: after-heading
          value: Workflow
        body: |
          WORK OVERRIDES BODY
"""


def _no_secrets(_body: str) -> SecretsScanResult:
    return SecretsScanResult(findings=(), files_scanned=0)


@pytest.fixture
def promote_scaffold(tmp_path: Path) -> dict[str, Path]:
    tracked = tmp_path / "tracked.md"
    live = tmp_path / "live.md"
    local_yaml = tmp_path / "local.yaml"
    tracked.write_text(_TRACKED_BEFORE, encoding="utf-8")
    live.write_text(_LIVE_BEFORE, encoding="utf-8")
    local_yaml.write_text(_LOCAL_YAML_BEFORE, encoding="utf-8")
    return {
        "tracked": tracked,
        "live": live,
        "local_yaml": local_yaml,
        "snapshot_base": tmp_path / "snap",
    }


def _make_plan(scaffold: dict[str, Path]) -> PromotePlan:
    return PromotePlan(
        section_name=HostLocalSectionName("work-overrides"),
        local_yaml_path=scaffold["local_yaml"],
        tracked_path=scaffold["tracked"],
        live_path=scaffold["live"],
        body="WORK OVERRIDES BODY\n",
        anchor=AnchorAfterHeading(kind=AnchorKind.AFTER_HEADING, value="Workflow"),
        revert_command="setforge revert --profile=demo",
        secrets=_no_secrets(""),
    )


def test_execute_inserts_shared_section_into_tracked_file(
    promote_scaffold: dict[str, Path],
) -> None:
    """After execute, tracked-file carries a NEW shared marker pair."""
    plan = _make_plan(promote_scaffold)
    execute_promote_to_shared(
        plan, tracked_file_id="demo_md", snapshot_base=promote_scaffold["snapshot_base"]
    )
    tracked_after = promote_scaffold["tracked"].read_text(encoding="utf-8")
    assert "start shared work-overrides" in tracked_after
    assert "end shared work-overrides" in tracked_after
    assert "WORK OVERRIDES BODY" in tracked_after


def test_execute_rewrites_live_markers_to_shared(
    promote_scaffold: dict[str, Path],
) -> None:
    """After execute, live-file markers carry the shared keyword."""
    plan = _make_plan(promote_scaffold)
    execute_promote_to_shared(
        plan, tracked_file_id="demo_md", snapshot_base=promote_scaffold["snapshot_base"]
    )
    live_after = promote_scaffold["live"].read_text(encoding="utf-8")
    assert "start shared work-overrides" in live_after
    assert "end shared work-overrides" in live_after
    # The body bytes between markers are unchanged.
    assert "WORK OVERRIDES BODY" in live_after
    # The legacy host-local keyword no longer appears on the work-overrides pair.
    assert "host-local work-overrides" not in live_after


def test_execute_drops_local_yaml_host_local_entry(
    promote_scaffold: dict[str, Path],
) -> None:
    """After execute, local.yaml no longer contains the host_local_sections entry."""
    plan = _make_plan(promote_scaffold)
    execute_promote_to_shared(
        plan, tracked_file_id="demo_md", snapshot_base=promote_scaffold["snapshot_base"]
    )
    yaml = YAML(typ="safe")
    doc = yaml.load(promote_scaffold["local_yaml"].read_text(encoding="utf-8"))
    # Either tracked_files.demo_md was dropped entirely (when host_local_sections
    # was its only sub-key) or host_local_sections itself is gone.
    if doc is not None and "tracked_files" in doc:
        demo = doc["tracked_files"].get("demo_md", {})
        hls = demo.get("host_local_sections") if isinstance(demo, dict) else None
        assert hls is None or "work-overrides" not in hls


_LOCAL_YAML_MIGRATED = """\
tracked_files:
  demo_md:
    spans:
    - anchor: work-overrides
      kind: overlay
      semantics: host-local
      overlay:
        anchor:
          kind: after-heading
          value: Workflow
        body: |
          WORK OVERRIDES BODY
"""


def test_execute_drops_migrated_overlay_span_entry(
    promote_scaffold: dict[str, Path],
) -> None:
    """Promote on an already-migrated host drops the OVERLAY ``spans`` entry.

    Post-migration ``local.yaml`` carries the host-local body as a
    ``spans`` OVERLAY entry (identity ``anchor`` = section name), NOT a
    legacy ``host_local_sections`` block. ``execute_promote_to_shared``
    must drop that representation; otherwise the promote would crash (no
    legacy block to drop) and the section would be promoted-but-not-dropped.
    """
    promote_scaffold["local_yaml"].write_text(_LOCAL_YAML_MIGRATED, encoding="utf-8")
    plan = _make_plan(promote_scaffold)
    execute_promote_to_shared(
        plan, tracked_file_id="demo_md", snapshot_base=promote_scaffold["snapshot_base"]
    )
    yaml = YAML(typ="safe")
    doc = yaml.load(promote_scaffold["local_yaml"].read_text(encoding="utf-8"))
    # The overlay span (and its now-empty parent) is gone; no overlay span
    # named work-overrides survives.
    if doc is not None and "tracked_files" in doc:
        demo = doc["tracked_files"].get("demo_md", {})
        spans = demo.get("spans") if isinstance(demo, dict) else None
        assert spans is None or all(s.get("anchor") != "work-overrides" for s in spans)


def test_rewrite_live_markers_preserves_body_bytes() -> None:
    """rewrite_live_markers_to_shared only changes the keyword on marker lines."""
    src = (
        "<!-- setforge:user-section start host-local x -->\n"
        "byte-exact body line 1\n"
        "byte-exact body line 2\n"
        "<!-- setforge:user-section end host-local x hash=abc -->\n"
    )
    out = rewrite_live_markers_to_shared(src, HostLocalSectionName("x"))
    # Exact-equality assertion: the only bytes that change are the
    # `host-local` → `shared` keyword swap on the two marker lines.
    # No extra prefix / suffix / body mutation is allowed (the function
    # is contractually byte-preserving for body content — anti-smell 4).
    expected = (
        "<!-- setforge:user-section start shared x -->\n"
        "byte-exact body line 1\n"
        "byte-exact body line 2\n"
        "<!-- setforge:user-section end shared x hash=abc -->\n"
    )
    assert out == expected


def test_offer_promote_gates_on_local_yaml_presence() -> None:
    """offer_promote returns True only for sections in host_local_sections overlay."""
    overlay = {
        "demo_md": {HostLocalSectionName("work-overrides"): object()},
    }
    assert offer_promote(
        section_name="work-overrides",
        host_local_sections=overlay,
        tracked_file_id="demo_md",
    )
    # Wrong tracked_file_id.
    assert not offer_promote(
        section_name="work-overrides",
        host_local_sections=overlay,
        tracked_file_id="other_md",
    )
    # Section absent from overlay.
    assert not offer_promote(
        section_name="not-declared",
        host_local_sections=overlay,
        tracked_file_id="demo_md",
    )


def test_post_promote_yaml_round_trips_comments(
    promote_scaffold: dict[str, Path],
) -> None:
    """Local.yaml comments / order survive the entry-drop (round-trip mode)."""
    # Seed a comment that round-trip mode preserves.
    yaml = YAML(typ="rt")
    doc = yaml.load(_LOCAL_YAML_BEFORE)
    doc.yaml_set_comment_before_after_key("tracked_files", before="# top comment")
    buf = io.StringIO()
    yaml.dump(doc, buf)
    promote_scaffold["local_yaml"].write_text(buf.getvalue(), encoding="utf-8")

    plan = _make_plan(promote_scaffold)
    execute_promote_to_shared(
        plan, tracked_file_id="demo_md", snapshot_base=promote_scaffold["snapshot_base"]
    )
    final = promote_scaffold["local_yaml"].read_text(encoding="utf-8")
    assert "# top comment" in final
