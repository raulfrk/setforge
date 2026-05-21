"""dg2a rollback tests — partial-failure path restores every file.

The :class:`setforge.wizard.Snapshot` context manager wraps the three
file writes; if any mutation raises, every file is restored from the
pre-write snapshot before the exception propagates. These tests force
a failure at each of the three write stages and assert the post-failure
file contents are byte-identical to the pre-promote state.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from setforge import section_promote
from setforge.errors import SetforgeError
from setforge.secrets import SecretsScanResult
from setforge.section_promote import PromotePlan, execute_promote_to_shared
from setforge.source import (
    AnchorAfterHeading,
    AnchorKind,
    HostLocalSectionName,
)

_TRACKED_BEFORE = """\
# Demo

## Workflow

workflow body
"""

_LIVE_BEFORE = """\
# Demo

<!-- setforge:user-section start host-local sect -->
live body
<!-- setforge:user-section end host-local sect hash=4ce29fbd1a93cd8de3cab7d3e8a5f12b6c0e35c4a9fa1217d4e2a6f0a8d3e7b9 -->
"""  # noqa: E501 — the literal hash segment cannot be wrapped without altering bytes

_LOCAL_YAML_BEFORE = """\
tracked_files:
  demo_md:
    host_local_sections:
      sect:
        anchor:
          kind: after-heading
          value: Workflow
        body: |
          live body
"""


def _no_secrets(_body: str) -> SecretsScanResult:
    return SecretsScanResult(findings=(), files_scanned=0)


@pytest.fixture
def scaffold(tmp_path: Path) -> dict[str, Path]:
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


def _make_plan(s: dict[str, Path]) -> PromotePlan:
    return PromotePlan(
        section_name=HostLocalSectionName("sect"),
        local_yaml_path=s["local_yaml"],
        tracked_path=s["tracked"],
        live_path=s["live"],
        body="live body\n",
        anchor=AnchorAfterHeading(kind=AnchorKind.AFTER_HEADING, value="Workflow"),
        revert_command="setforge revert --profile=demo",
        secrets=_no_secrets(""),
    )


def _files_unchanged(s: dict[str, Path]) -> bool:
    return (
        s["tracked"].read_text(encoding="utf-8") == _TRACKED_BEFORE
        and s["live"].read_text(encoding="utf-8") == _LIVE_BEFORE
        and s["local_yaml"].read_text(encoding="utf-8") == _LOCAL_YAML_BEFORE
    )


def test_rollback_when_tracked_write_fails(
    scaffold: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the tracked-file write raises, every file stays at pre-promote state."""
    real_write = section_promote._atomic_write_text
    call_count = {"n": 0}

    def fail_on_tracked(path: Path, content: str) -> None:
        call_count["n"] += 1
        if path == scaffold["tracked"]:
            raise OSError("simulated tracked-write failure")
        real_write(path, content)

    monkeypatch.setattr(section_promote, "_atomic_write_text", fail_on_tracked)
    plan = _make_plan(scaffold)
    with pytest.raises(OSError, match="simulated tracked-write failure"):
        execute_promote_to_shared(
            plan, tracked_file_id="demo_md", snapshot_base=scaffold["snapshot_base"]
        )
    assert _files_unchanged(scaffold)


def test_rollback_when_live_write_fails(
    scaffold: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the live-file write raises, every file is restored."""
    real_write = section_promote._atomic_write_text
    seen_paths: list[Path] = []

    def fail_on_live(path: Path, content: str) -> None:
        seen_paths.append(path)
        if path == scaffold["live"]:
            raise OSError("simulated live-write failure")
        real_write(path, content)

    monkeypatch.setattr(section_promote, "_atomic_write_text", fail_on_live)
    plan = _make_plan(scaffold)
    with pytest.raises(OSError, match="simulated live-write failure"):
        execute_promote_to_shared(
            plan, tracked_file_id="demo_md", snapshot_base=scaffold["snapshot_base"]
        )
    assert _files_unchanged(scaffold)
    # We confirm tracked was written first (then rolled back), then live failed.
    assert scaffold["tracked"] in seen_paths
    assert scaffold["live"] in seen_paths


def test_rollback_when_local_yaml_drop_fails(
    scaffold: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the local.yaml drop raises (e.g. missing key), every file is restored."""

    def fail_yaml(*_args: object, **_kwargs: object) -> None:
        raise SetforgeError("simulated local.yaml mutation failure")

    monkeypatch.setattr(section_promote, "_drop_host_local_section_entry", fail_yaml)
    plan = _make_plan(scaffold)
    with pytest.raises(SetforgeError, match=r"simulated local\.yaml mutation failure"):
        execute_promote_to_shared(
            plan, tracked_file_id="demo_md", snapshot_base=scaffold["snapshot_base"]
        )
    assert _files_unchanged(scaffold)


def test_rewrite_live_markers_raises_on_missing_pair() -> None:
    """rewrite_live_markers_to_shared raises when no host-local pair matches."""
    text = "no markers here\n"
    with pytest.raises(SetforgeError, match="expected exactly one"):
        section_promote.rewrite_live_markers_to_shared(text, HostLocalSectionName("x"))


def test_rewrite_live_markers_raises_on_duplicate_pair() -> None:
    """rewrite_live_markers_to_shared raises when two host-local pairs match."""
    text = (
        "<!-- setforge:user-section start host-local dup -->\nfoo\n"
        "<!-- setforge:user-section end host-local dup -->\n"
        "<!-- setforge:user-section start host-local dup -->\nbar\n"
        "<!-- setforge:user-section end host-local dup -->\n"
    )
    with pytest.raises(SetforgeError, match="expected exactly one"):
        section_promote.rewrite_live_markers_to_shared(
            text, HostLocalSectionName("dup")
        )
