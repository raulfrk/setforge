"""Unit tests for the increment-3 host-local marker → overlay capture migration.

The migration reads host-local user-section marker bodies from a deployed live
file and writes them into ``local.yaml`` as at-end-of-file OVERLAY spans, before
deploy's blanket ``strip_host_local_markers`` would delete them.
"""

from __future__ import annotations

from pathlib import Path

from setforge.body_canon import canonical_body
from setforge.host_local_marker_migration import (
    append_overlay_spans,
    build_overlay_span_node,
)
from setforge.source import HostLocalSectionName, load_local_host_local_sections

# --- Task 2: build_overlay_span_node + append_overlay_spans ------------------


def test_append_writes_at_eof_overlay_span(tmp_path: Path) -> None:
    local = tmp_path / "local.yaml"
    local.write_text("# c\ntracked_files:\n  doc: {}\n", encoding="utf-8")
    n = append_overlay_spans(local, {"doc": [("notes", "host notes\n")]})
    assert n == 1
    sections = load_local_host_local_sections(local)["doc"]
    assert set(sections) == {"notes"}
    assert sections[HostLocalSectionName("notes")].body == "host notes\n"
    assert "# c" in local.read_text(encoding="utf-8")  # comment preserved


def test_append_canonicalizes_body(tmp_path: Path) -> None:
    local = tmp_path / "local.yaml"
    local.write_text("tracked_files:\n  doc: {}\n", encoding="utf-8")
    append_overlay_spans(local, {"doc": [("notes", "no trailing newline")]})
    sections = load_local_host_local_sections(local)["doc"]
    assert sections[HostLocalSectionName("notes")].body == canonical_body(
        "no trailing newline"
    )


def test_append_presence_check_idempotent(tmp_path: Path) -> None:
    local = tmp_path / "local.yaml"
    local.write_text("tracked_files:\n  doc: {}\n", encoding="utf-8")
    assert append_overlay_spans(local, {"doc": [("notes", "b\n")]}) == 1
    before = local.read_bytes()
    # Re-append the same name: skipped, no write.
    assert append_overlay_spans(local, {"doc": [("notes", "b\n")]}) == 0
    assert local.read_bytes() == before


def test_append_nothing_is_noop(tmp_path: Path) -> None:
    local = tmp_path / "local.yaml"
    local.write_text("tracked_files:\n  doc: {}\n", encoding="utf-8")
    assert append_overlay_spans(local, {}) == 0


def test_append_creates_overlay_block_when_absent(tmp_path: Path) -> None:
    local = tmp_path / "local.yaml"
    local.write_text("tracked_files: {}\n", encoding="utf-8")
    assert append_overlay_spans(local, {"doc": [("notes", "b\n")]}) == 1
    assert set(load_local_host_local_sections(local)["doc"]) == {"notes"}


def test_append_creates_local_yaml_when_absent(tmp_path: Path) -> None:
    """Absent local.yaml is created — never silently lose bodies to a no-op."""
    local = tmp_path / "local.yaml"
    assert not local.exists()
    assert append_overlay_spans(local, {"doc": [("notes", "b\n")]}) == 1
    assert local.exists()
    assert set(load_local_host_local_sections(local)["doc"]) == {"notes"}


def test_build_node_shape() -> None:
    node = build_overlay_span_node("notes", "body\n")
    assert node["anchor"] == "notes"
    assert node["kind"] == "overlay"
    assert node["semantics"] == "host-local"
    assert node["overlay"]["anchor"]["kind"] == "at-end-of-file"
    assert node["overlay"]["body"] == "body\n"
