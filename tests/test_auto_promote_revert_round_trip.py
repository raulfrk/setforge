"""auto-promote revert round-trip — promote then revert restores pre-promote bytes.

The :class:`setforge.transitions.TransitionCommand.PROMOTE` transition
records a unified diff covering the three mutated files. Standard
``setforge revert`` machinery (:func:`apply_patch_reverse`) replays
that diff in reverse, so post-revert the three files match their
pre-promote bytes byte-for-byte.

These tests do NOT shell out to ``setforge revert``; they exercise the
:func:`setforge.transitions.write_transition` +
:func:`apply_patch_reverse` primitives directly so the round-trip can
be verified inside a single pytest invocation without managing real
state-dir layout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from setforge import transitions
from setforge.secrets import SecretsScanResult
from setforge.section_promote import (
    PromotePlan,
    execute_promote_to_shared,
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

<!-- setforge:user-section start host-local rt -->
round trip body
<!-- setforge:user-section end host-local rt \
hash=4ce29fbd1a93cd8de3cab7d3e8a5f12b6c0e35c4a9fa1217d4e2a6f0a8d3e7b9 -->
"""

_LOCAL_YAML_BEFORE = """\
tracked_files:
  demo_md:
    host_local_sections:
      rt:
        anchor:
          kind: after-heading
          value: Workflow
        body: |
          round trip body
"""


def _no_secrets(_body: str) -> SecretsScanResult:
    return SecretsScanResult(findings=(), files_scanned=0)


@pytest.fixture
def scaffold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state_dir))
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
        "state_dir": state_dir,
    }


def _make_plan(s: dict[str, Path]) -> PromotePlan:
    return PromotePlan(
        section_name=HostLocalSectionName("rt"),
        local_yaml_path=s["local_yaml"],
        tracked_path=s["tracked"],
        live_path=s["live"],
        body="round trip body\n",
        anchor=AnchorAfterHeading(kind=AnchorKind.AFTER_HEADING, value="Workflow"),
        revert_command="setforge revert --profile=demo",
        secrets=_no_secrets(""),
    )


def test_promote_transition_records_files_to_patch(
    scaffold: dict[str, Path],
) -> None:
    """The PROMOTE transition records a non-empty patch covering all 3 files."""
    plan = _make_plan(scaffold)
    snapshot_paths = [plan.tracked_path, plan.live_path, plan.local_yaml_path]
    file_pre = transitions.snapshot_paths(snapshot_paths)
    execute_promote_to_shared(
        plan, tracked_file_id="demo_md", snapshot_base=scaffold["snapshot_base"]
    )
    file_post = transitions.snapshot_paths(snapshot_paths)

    target = transitions.write_transition(
        transitions.make_meta(transitions.TransitionCommand.PROMOTE, "demo"),
        file_pre,
        file_post,
        None,
    )
    patch_file = target / "changes.patch"
    assert patch_file.exists()
    patch_text = patch_file.read_text(encoding="utf-8")
    assert str(plan.tracked_path).lstrip("/") in patch_text
    assert str(plan.live_path).lstrip("/") in patch_text
    assert str(plan.local_yaml_path).lstrip("/") in patch_text

    meta_text = (target / "meta.json").read_text(encoding="utf-8")
    assert '"command": "promote"' in meta_text


def test_revert_round_trip_restores_pre_promote_bytes(
    scaffold: dict[str, Path],
) -> None:
    """Apply promote, write transition, apply_patch_reverse, assert byte-equal."""
    plan = _make_plan(scaffold)
    snapshot_paths = [plan.tracked_path, plan.live_path, plan.local_yaml_path]
    pre_tracked = scaffold["tracked"].read_text(encoding="utf-8")
    pre_live = scaffold["live"].read_text(encoding="utf-8")
    pre_local = scaffold["local_yaml"].read_text(encoding="utf-8")

    file_pre = transitions.snapshot_paths(snapshot_paths)
    execute_promote_to_shared(
        plan, tracked_file_id="demo_md", snapshot_base=scaffold["snapshot_base"]
    )
    file_post = transitions.snapshot_paths(snapshot_paths)

    target = transitions.write_transition(
        transitions.make_meta(transitions.TransitionCommand.PROMOTE, "demo"),
        file_pre,
        file_post,
        None,
    )
    transitions.apply_patch_reverse(target)

    assert scaffold["tracked"].read_text(encoding="utf-8") == pre_tracked
    assert scaffold["live"].read_text(encoding="utf-8") == pre_live
    assert scaffold["local_yaml"].read_text(encoding="utf-8") == pre_local


def test_revert_dry_run_succeeds_on_unchanged_post_state(
    scaffold: dict[str, Path],
) -> None:
    """Dry-run reverse-patch passes when post-promote state matches what we wrote."""
    plan = _make_plan(scaffold)
    snapshot_paths = [plan.tracked_path, plan.live_path, plan.local_yaml_path]
    file_pre = transitions.snapshot_paths(snapshot_paths)
    execute_promote_to_shared(
        plan, tracked_file_id="demo_md", snapshot_base=scaffold["snapshot_base"]
    )
    file_post = transitions.snapshot_paths(snapshot_paths)

    target = transitions.write_transition(
        transitions.make_meta(transitions.TransitionCommand.PROMOTE, "demo"),
        file_pre,
        file_post,
        None,
    )
    # Dry-run does not raise → returns cleanly.
    transitions.apply_patch_reverse(target, dry_run=True)
