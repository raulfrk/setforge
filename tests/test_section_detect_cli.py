"""Unit tests for ``setforge section detect`` (S4/S5) — the CLI orchestration
layer in :mod:`setforge.cli._detect_helpers`.

The pure detect engine (``compute_detect_regions`` / ``propose_anchor``) is
covered by :mod:`tests.test_section_detect`; these tests cover the config/IO
plumbing, KIND gating, anchor reconstruction, atomic carve writes, the wizard
loop, and S5 overlay re-capture.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from setforge.cli import app
from setforge.cli._detect_helpers import DetectTarget
from setforge.section_detect import DetectRegion, RegionKind

_CFG = "tests/fixtures/e2e/setforge.test.yaml"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _silent_console() -> Console:
    return Console(file=io.StringIO())


def test_detect_no_drift_exits_zero(runner: CliRunner) -> None:
    """When no live file drifts (here: the live dst does not exist in the unit
    sandbox), detect reports no changes and exits 0."""
    result = runner.invoke(
        app,
        [
            "section",
            "detect",
            f"--config={_CFG}",
            "--profile=test-text-sections",
            "--tracked-file=sections_md",
        ],
    )
    assert result.exit_code == 0, result.output
    out = result.output.lower()
    assert "no changes detected" in out


def test_expected_matches_deploy_then_edit_surfaces() -> None:
    """The expected-deploy string is deterministic + live-independent: it equals
    a no-drift baseline (idempotency) and a fresh live edit surfaces as a
    NEW_CONTENT region (plan P1/P2)."""
    from setforge.cli import _detect_helpers as dh
    from setforge.config import load_config
    from setforge.section_detect import compute_detect_regions

    cfg_path = Path(_CFG)
    cfg = load_config(cfg_path)
    repo_root = cfg_path.resolve().parent
    target = dh._markdown_targets(cfg, "test-text-sections", repo_root, "sections_md")[
        0
    ]

    expected = dh.expected_deploy_text("test-text-sections", target, None)
    assert expected.strip(), "expected deploy output must be non-empty"

    # Idempotency: live == expected → zero regions.
    assert compute_detect_regions(expected, expected) == []

    # A fresh live edit (a new trailing block) surfaces as NEW_CONTENT.
    edited = expected + "\nHand-written host note\n"
    regions = compute_detect_regions(edited, expected)
    assert len(regions) == 1
    assert regions[0].kind.value == "new-content"
    assert "Hand-written host note" in regions[0].live_text


def test_disposition_file_expected_matches_deploy() -> None:
    """For a disposition file, expected is the deploy output (plan P1/P3 — the
    pinned-carve target); a divergence from it surfaces as a region."""
    from setforge.cli import _detect_helpers as dh
    from setforge.config import load_config
    from setforge.section_detect import compute_detect_regions

    cfg_path = Path(_CFG)
    cfg = load_config(cfg_path)
    repo_root = cfg_path.resolve().parent
    target = dh._markdown_targets(cfg, "test-host-local", repo_root, "host_local_md")[0]
    assert target.tracked_file.disposition is not None

    expected = dh.expected_deploy_text("test-host-local", target, None)
    assert expected.strip()
    assert compute_detect_regions(expected, expected) == []

    edited = expected.replace("Workflow body content", "MY workflow override")
    assert edited != expected
    regions = compute_detect_regions(edited, expected)
    assert any(r.kind.value == "divergence" for r in regions)


# --- Task 3: KIND gating + pinned anchor reconstruction -----------------------


def _target(disposition_key: str) -> DetectTarget:
    """A real DetectTarget from the fixture: ``sections_md`` (disposition=None)
    or ``host_local_md`` (disposition=shared)."""
    from setforge.cli import _detect_helpers as dh
    from setforge.config import load_config

    cfg_path = Path(_CFG)
    cfg = load_config(cfg_path)
    repo_root = cfg_path.resolve().parent
    if disposition_key == "none":
        return dh._markdown_targets(
            cfg, "test-text-sections", repo_root, "sections_md"
        )[0]
    return dh._markdown_targets(cfg, "test-host-local", repo_root, "host_local_md")[0]


def _new_content_region(live_text: str = "X\n", live_start: int = 0) -> DetectRegion:
    return DetectRegion(
        kind=RegionKind.NEW_CONTENT,
        live_start=live_start,
        live_end=live_start + 1,
        expected_start=0,
        expected_end=0,
        live_text=live_text,
        expected_text="",
    )


def _divergence_region(live_start: int = 0, live_end: int = 1) -> DetectRegion:
    return DetectRegion(
        kind=RegionKind.DIVERGENCE,
        live_start=live_start,
        live_end=live_end,
        expected_start=live_start,
        expected_end=live_end,
        live_text="changed\n",
        expected_text="old\n",
    )


def test_allowed_kinds_new_content_overlay_only() -> None:
    from setforge.cli import _detect_helpers as dh

    assert dh.allowed_kinds(_new_content_region(), _target("none")) == ["overlay"]


def test_allowed_kinds_divergence_needs_disposition() -> None:
    from setforge.cli import _detect_helpers as dh

    div = _divergence_region()
    assert dh.allowed_kinds(div, _target("shared")) == ["pinned", "forked"]
    # DIVERGENCE on a disposition=None file → no valid kind (refused; plan P3).
    assert dh.allowed_kinds(div, _target("none")) == []


def test_pinned_anchor_reconstructs_hashes() -> None:
    from setforge.cli import _detect_helpers as dh

    live = "# Top\n\n## My Heading\n\nbody changed\n"
    # line 4 (0-indexed) is 'body changed'; enclosing heading is '## My Heading'.
    region = _divergence_region(live_start=4, live_end=5)
    assert dh.pinned_anchor_string(region, live) == "## My Heading"


# --- Task 4: span construction + atomic carve writes --------------------------


def test_build_overlay_span() -> None:
    from setforge.anchors import AnchorAfterHeading
    from setforge.cli import _detect_helpers as dh
    from setforge.overlay_inject import canonical_body
    from setforge.spans import SpanKind, SpanSemantics

    plan = dh.CarvePlan(
        kind="overlay",
        name="vm-notes",
        anchor=AnchorAfterHeading(value="My Notes"),
        body="  - x\n",
        semantics="host-local",
    )
    span = dh.build_span(plan)
    assert span.kind is SpanKind.OVERLAY
    assert span.semantics is SpanSemantics.HOST_LOCAL
    assert span.anchor == "vm-notes"  # identity anchor = section name (dual-anchor)
    assert span.overlay is not None
    assert span.overlay.body == canonical_body("  - x\n")
    assert span.overlay.anchor == AnchorAfterHeading(value="My Notes")


def test_build_pinned_span() -> None:
    from setforge.cli import _detect_helpers as dh
    from setforge.spans import SpanKind

    plan = dh.CarvePlan(
        kind="pinned",
        name="notes",
        anchor="## My Heading",
        body=None,
        semantics="host-local",
    )
    span = dh.build_span(plan)
    assert span.kind is SpanKind.PINNED
    assert span.anchor == "## My Heading"
    assert span.overlay is None


def test_seed_overlay_state_has_canonical_last_deployed_body() -> None:
    from setforge.cli import _detect_helpers as dh
    from setforge.overlay_inject import canonical_body

    live = "# H\n\nbody line\n"
    region = _new_content_region(live_text="body line\n", live_start=2)
    state = dh.seed_overlay_state("nm", live, region)
    assert state.last_deployed_body == canonical_body("body line\n")


def test_commit_writes_spans(tmp_path: Path) -> None:
    from setforge.cli import _detect_helpers as dh
    from setforge.cli import override

    plans = [
        dh.CarvePlan(
            kind="pinned", name="a", anchor="## A", body=None, semantics="host-local"
        )
    ]
    dh.commit_carves("p", "sections_md", plans, snapshot_base=tmp_path)
    data = override._load_local_data()
    spans = data["tracked_files"]["sections_md"]["spans"]  # type: ignore[index]
    assert any(s["anchor"] == "## A" for s in spans)


def test_commit_rolls_back_on_failure(tmp_path: Path, monkeypatch) -> None:
    from setforge.cli import _detect_helpers as dh
    from setforge.cli import override

    # Pre-seed local.yaml so we have a known baseline to restore to.
    override._append_span_host_local(
        "sections_md",
        dh.build_span(
            dh.CarvePlan(
                kind="pinned",
                name="pre",
                anchor="## Pre",
                body=None,
                semantics="host-local",
            )
        ),
    )
    before = override._local_config_path().read_text(encoding="utf-8")

    real = override._append_span_host_local
    calls = {"n": 0}

    def flaky(file_id: str, span: object) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        real(file_id, span)  # type: ignore[arg-type]

    monkeypatch.setattr(dh.override, "_append_span_host_local", flaky)
    plans = [
        dh.CarvePlan(
            kind="pinned", name="x", anchor="## X", body=None, semantics="host-local"
        ),
        dh.CarvePlan(
            kind="pinned", name="y", anchor="## Y", body=None, semantics="host-local"
        ),
    ]
    with pytest.raises(RuntimeError):
        dh.commit_carves("p", "sections_md", plans, snapshot_base=tmp_path)

    after = override._local_config_path().read_text(encoding="utf-8")
    assert after == before  # snapshot restored — no half-created span


# --- Task 5: carve wizard (line-prompt UI) ------------------------------------


def test_wizard_carves_one_overlay(monkeypatch) -> None:
    from setforge.cli import _detect_helpers as dh
    from setforge.section_detect import compute_detect_regions

    live = "# Title\n\nbody\n  - x\n"
    expected = "# Title\n\nbody\n"
    regions = compute_detect_regions(live, expected)
    assert len(regions) == 1
    assert regions[0].kind is RegionKind.NEW_CONTENT

    # overlay kind is auto-selected (only one allowed), so no kind prompt.
    answers = iter(["carve", "vm-notes", "host-local"])
    monkeypatch.setattr(dh, "_ask", lambda _prompt: next(answers))

    plans = dh.carve_wizard(_target("none"), regions, live, expected, _silent_console())
    assert len(plans) == 1
    plan = plans[0]
    assert plan.kind == "overlay"
    assert plan.name == "vm-notes"
    assert plan.body == "  - x\n"
    assert plan.seed_state is not None


def test_wizard_skip_produces_no_plan(monkeypatch) -> None:
    from setforge.cli import _detect_helpers as dh
    from setforge.section_detect import compute_detect_regions

    live = "# Title\n\nbody\n  - x\n"
    expected = "# Title\n\nbody\n"
    regions = compute_detect_regions(live, expected)
    monkeypatch.setattr(dh, "_ask", lambda _prompt: "skip")
    plans = dh.carve_wizard(_target("none"), regions, live, expected, _silent_console())
    assert plans == []


def test_wizard_refuses_shared_scope(monkeypatch) -> None:
    """Scope is asked explicitly and never auto-defaulted; shared is out of
    scope for detect, so a shared answer skips the carve (no plan)."""
    from setforge.cli import _detect_helpers as dh
    from setforge.section_detect import compute_detect_regions

    live = "# Title\n\nbody\n  - x\n"
    expected = "# Title\n\nbody\n"
    regions = compute_detect_regions(live, expected)
    answers = iter(["carve", "vm-notes", "shared"])
    monkeypatch.setattr(dh, "_ask", lambda _prompt: next(answers))
    plans = dh.carve_wizard(_target("none"), regions, live, expected, _silent_console())
    assert plans == []


# --- Task 6: S4 pitfall coverage ----------------------------------------------


def test_wizard_refuses_ambiguous_overlay_body(monkeypatch) -> None:
    """An overlay body that occurs >1x in live is born ambiguous — the carve is
    refused (uniqueness pre-flight), never silently written."""
    from setforge.cli import _detect_helpers as dh
    from setforge.section_detect import compute_detect_regions

    live = "# T\n\ndup\n\ndup\n"
    expected = "# T\n\ndup\n"
    regions = compute_detect_regions(live, expected)
    answers = iter(["carve", "dups", "host-local"])
    monkeypatch.setattr(dh, "_ask", lambda _prompt: next(answers))
    plans = dh.carve_wizard(_target("none"), regions, live, expected, _silent_console())
    assert plans == []


def test_wizard_refuses_divergence_without_disposition(monkeypatch) -> None:
    """A DIVERGENCE on a disposition=None file has no valid kind (pinned/forked
    would fail validate_span_disposition); the carve is refused."""
    from setforge.cli import _detect_helpers as dh

    region = _divergence_region()
    answers = iter(["carve", "nm", "host-local"])
    monkeypatch.setattr(dh, "_ask", lambda _prompt: next(answers))
    plans = dh.carve_wizard(
        _target("none"), [region], "changed\n", "old\n", _silent_console()
    )
    assert plans == []


def test_wizard_propagates_anchor_refusal(monkeypatch) -> None:
    """A divergence under a duplicate heading yields an AnchorRefusal, which the
    wizard surfaces as a skip rather than fabricating an anchor."""
    from setforge.cli import _detect_helpers as dh
    from setforge.section_detect import compute_detect_regions

    live = "## Dup\n\nA\n\n## Dup\n\nMY EDIT\n"
    expected = "## Dup\n\nA\n\n## Dup\n\nB\n"
    regions = compute_detect_regions(live, expected)
    assert any(r.kind is RegionKind.DIVERGENCE for r in regions)
    answers = iter(["carve", "nm", "host-local", "pinned"])
    monkeypatch.setattr(dh, "_ask", lambda _prompt: next(answers))
    # target has a disposition so pinned/forked are offered (and then refused
    # at the anchor stage).
    plans = dh.carve_wizard(
        _target("shared"), regions, live, expected, _silent_console()
    )
    assert plans == []


# --- Task 7 follow-up: subtract span-covered regions (idempotency) ------------


def test_covered_by_span_subtracts_carved_pinned() -> None:
    from setforge.cli import _detect_helpers as dh
    from setforge.config import Disposition, TrackedFile
    from setforge.spans import SpanEntry, SpanKind, SpanSemantics

    tf = TrackedFile(
        src=Path("x.md"),
        dst="~/x.md",
        disposition=Disposition.SHARED,
        spans=[
            SpanEntry(
                anchor="## Workflow",
                kind=SpanKind.PINNED,
                semantics=SpanSemantics.HOST_LOCAL,
            )
        ],
    )
    live = "## Workflow\n\nmy edited body\n"
    region = _divergence_region(live_start=2, live_end=3)
    assert dh.covered_by_span(region, live, tf) is True

    other = "## Other\n\nmy edit\n"
    region2 = _divergence_region(live_start=2, live_end=3)
    assert dh.covered_by_span(region2, other, tf) is False


def test_covered_by_span_false_without_spans() -> None:
    from setforge.cli import _detect_helpers as dh

    region = _divergence_region(live_start=0, live_end=1)
    assert (
        dh.covered_by_span(region, "changed\n", _target("none").tracked_file) is False
    )
