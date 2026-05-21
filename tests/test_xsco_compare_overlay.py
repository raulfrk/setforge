"""Compare-overlay unit tests for host-local injection (setforge-xsco).

Asserts that :func:`setforge.compare.diff_file` masks injected host-local
sections so an already-deployed live file does NOT report drift on the
next ``setforge compare``.
"""

from __future__ import annotations

from pathlib import Path

from setforge.compare import diff_file
from setforge.deploy import copy_atomic
from setforge.source import AnchorAfterHeading, HostLocalSection


def _write_src(tmp_path: Path, content: str) -> Path:
    src = tmp_path / "src.md"
    src.write_text(content, encoding="utf-8")
    return src


def test_compare_no_drift_after_host_local_install(tmp_path: Path) -> None:
    src = _write_src(tmp_path, "# Title\n\n## Workflow\n\nbody\n")
    dst = tmp_path / "dst.md"
    host_local = {
        "work-overrides": HostLocalSection(
            anchor=AnchorAfterHeading(value="Workflow"), body="WORK OVERRIDES"
        )
    }
    copy_atomic(src, dst, preserve_user_sections=True, host_local_sections=host_local)
    # With host_local_sections passed to diff_file, rendered src ==
    # post-install live, so no diff body.
    diff = diff_file(
        src,
        dst,
        preserve_user_sections=True,
        host_local_sections=host_local,
    )
    assert diff == ""


def test_compare_without_host_local_arg_shows_drift_for_injected_marker(
    tmp_path: Path,
) -> None:
    """Sanity-check: without the host-local arg, a deployed live file
    DOES surface drift — confirms the overlay-aware path actively masks
    it (rather than masking unconditionally).
    """
    src = _write_src(tmp_path, "# Title\n\n## Workflow\n\nbody\n")
    dst = tmp_path / "dst.md"
    host_local = {
        "work-overrides": HostLocalSection(
            anchor=AnchorAfterHeading(value="Workflow"), body="WORK OVERRIDES"
        )
    }
    copy_atomic(src, dst, preserve_user_sections=True, host_local_sections=host_local)
    diff_no_arg = diff_file(src, dst, preserve_user_sections=True)
    assert diff_no_arg != ""
    assert "work-overrides" in diff_no_arg


def test_compare_with_extra_tracked_drift_still_reports(tmp_path: Path) -> None:
    """When the tracked content drifts (independent of host-local), compare
    still reports the drift body — the host-local mask must be SCOPED to
    injected sections, not a blanket diff suppression.
    """
    src = _write_src(tmp_path, "# Title\n\n## Workflow\n\nbody\n")
    dst = tmp_path / "dst.md"
    host_local = {
        "work-overrides": HostLocalSection(
            anchor=AnchorAfterHeading(value="Workflow"), body="WORK"
        )
    }
    copy_atomic(src, dst, preserve_user_sections=True, host_local_sections=host_local)
    # Modify the live file's NON-injected content to introduce real drift.
    text = dst.read_text(encoding="utf-8")
    drift_text = text.replace("body\n", "MUTATED BODY\n")
    dst.write_text(drift_text, encoding="utf-8")
    diff = diff_file(
        src, dst, preserve_user_sections=True, host_local_sections=host_local
    )
    assert diff != ""
    assert "MUTATED" in diff
