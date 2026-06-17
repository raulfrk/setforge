"""Unit tests for ``setforge section detect`` (S4/S5) — the CLI orchestration
layer in :mod:`setforge.cli._detect_helpers`.

The pure detect engine (``compute_detect_regions`` / ``propose_anchor``) is
covered by :mod:`tests.test_section_detect`; these tests cover the config/IO
plumbing, KIND gating, anchor reconstruction, atomic carve writes, the wizard
loop, and S5 overlay re-capture.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import app
from setforge.cli._detect_helpers import DetectTarget
from setforge.section_detect import DetectRegion, RegionKind

_CFG = "tests/fixtures/e2e/setforge.test.yaml"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


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


def test_disposition_file_expected_is_live_independent() -> None:
    """For a disposition file, ``live_text=""`` keeps expected pristine so a
    live divergence still surfaces (plan P1/P3 — the pinned-carve target)."""
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
