"""Install-side hash + idempotency unit tests for host-local injection.

Exercises :func:`setforge.deploy.copy_atomic` with the ``host_local_sections``
parameter against on-disk markdown fixtures. Asserts the post-install
hash invariant (``extract_marker_hashes(text) == hash_sections(text)``)
and the re-install idempotency contract (body update; no duplicate pair).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from setforge.deploy import DeployAction, copy_atomic
from setforge.errors import AnchorNotFoundError
from setforge.sections import extract_marker_hashes, extract_sections, hash_sections
from setforge.source import (
    AnchorAfterHeading,
    AnchorAtEndOfFile,
    HostLocalSection,
    HostLocalSectionName,
)


def _write_tracked(tmp_path: Path, content: str) -> Path:
    src = tmp_path / "src.md"
    src.write_text(content, encoding="utf-8")
    return src


def test_install_after_heading_invariant_holds(tmp_path: Path) -> None:
    src = _write_tracked(tmp_path, "# Title\n\n## Workflow\n\nbody\n")
    dst = tmp_path / "dst.md"
    host_local = {
        HostLocalSectionName("work-overrides"): HostLocalSection(
            anchor=AnchorAfterHeading(value="Workflow"),
            body="WORK OVERRIDES CONTENT",
        )
    }
    result = copy_atomic(
        src, dst, preserve_user_sections=True, host_local_sections=host_local
    )
    assert result.action is DeployAction.CREATED
    text = dst.read_text(encoding="utf-8")
    assert "WORK OVERRIDES CONTENT" in text
    assert extract_marker_hashes(text) == hash_sections(text)


def test_install_idempotent_re_run_no_duplication(tmp_path: Path) -> None:
    src = _write_tracked(tmp_path, "# Title\n\n## Workflow\n\nbody\n")
    dst = tmp_path / "dst.md"
    host_local = {
        HostLocalSectionName("section-a"): HostLocalSection(
            anchor=AnchorAfterHeading(value="Workflow"), body="initial body"
        )
    }
    copy_atomic(src, dst, preserve_user_sections=True, host_local_sections=host_local)
    copy_atomic(src, dst, preserve_user_sections=True, host_local_sections=host_local)
    text = dst.read_text(encoding="utf-8")
    assert text.count("start host-local section-a") == 1
    assert extract_marker_hashes(text) == hash_sections(text)


def test_install_idempotent_body_update_replaces_in_place(tmp_path: Path) -> None:
    src = _write_tracked(tmp_path, "# T\n\n## Workflow\n\n")
    dst = tmp_path / "dst.md"
    initial = {
        HostLocalSectionName("s"): HostLocalSection(
            anchor=AnchorAfterHeading(value="Workflow"), body="version-1"
        )
    }
    copy_atomic(src, dst, preserve_user_sections=True, host_local_sections=initial)
    updated = {
        HostLocalSectionName("s"): HostLocalSection(
            anchor=AnchorAfterHeading(value="Workflow"), body="version-2"
        )
    }
    copy_atomic(src, dst, preserve_user_sections=True, host_local_sections=updated)
    text = dst.read_text(encoding="utf-8")
    assert "version-2" in text
    assert "version-1" not in text
    assert text.count("start host-local s") == 1


def test_install_anchor_not_found_raises_no_file_modified(tmp_path: Path) -> None:
    src = _write_tracked(tmp_path, "# Title\n\nNo workflow heading.\n")
    dst = tmp_path / "dst.md"
    host_local = {
        HostLocalSectionName("ghost"): HostLocalSection(
            anchor=AnchorAfterHeading(value="Workflow"), body="will not land"
        )
    }
    with pytest.raises(AnchorNotFoundError):
        copy_atomic(
            src, dst, preserve_user_sections=True, host_local_sections=host_local
        )
    # Hard-fail contract: no live file written on the failed install.
    assert not dst.exists()


def test_install_at_end_of_file_appends_marker_pair(tmp_path: Path) -> None:
    src = _write_tracked(tmp_path, "# T\n")
    dst = tmp_path / "dst.md"
    host_local = {
        HostLocalSectionName("tail"): HostLocalSection(
            anchor=AnchorAtEndOfFile(), body="tail body"
        )
    }
    copy_atomic(src, dst, preserve_user_sections=True, host_local_sections=host_local)
    text = dst.read_text(encoding="utf-8")
    assert "tail body" in text
    assert extract_marker_hashes(text) == hash_sections(text)


def test_install_section_keyed_in_extract_sections(tmp_path: Path) -> None:
    src = _write_tracked(tmp_path, "# T\n\n## Workflow\n\n")
    dst = tmp_path / "dst.md"
    host_local = {
        HostLocalSectionName("named-section"): HostLocalSection(
            anchor=AnchorAfterHeading(value="Workflow"), body="body content\n"
        )
    }
    copy_atomic(src, dst, preserve_user_sections=True, host_local_sections=host_local)
    text = dst.read_text(encoding="utf-8")
    sections = extract_sections(text)
    assert "named-section" in sections
    assert "body content" in sections["named-section"]
