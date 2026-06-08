"""Unit tests for the increment-3 host-local marker → overlay capture migration.

The migration reads host-local user-section marker bodies from a deployed live
file and writes them into ``local.yaml`` as at-end-of-file OVERLAY spans, before
deploy's blanket ``strip_host_local_markers`` would delete them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from setforge.errors import MarkerError
from setforge.host_local_marker_migration import (
    append_overlay_spans,
    build_overlay_span_node,
    extract_host_local_marker_bodies,
)
from setforge.overlay_inject import canonical_body
from setforge.source import load_local_tracked_file_overlays
from setforge.spans import SpanKind

_H = "a" * 64


def _pair(name: str, body: str, *, semantics: str = "host-local") -> str:
    return (
        f"<!-- setforge:user-section start {semantics} {name} -->\n"
        f"{body}"
        f"<!-- setforge:user-section end {semantics} {name} hash={_H} -->\n"
    )


# --- Task 1: extract_host_local_marker_bodies -------------------------------


def test_extracts_only_host_local_bodies_by_name() -> None:
    text = (
        "# T\n\n"
        + _pair("notes", "host notes\n")
        + "\n"
        + _pair("shared-x", "shared body\n", semantics="shared")
    )
    out = extract_host_local_marker_bodies(text)
    assert out == {"notes": "host notes\n"}  # shared region excluded


def test_empty_host_local_body_kept_as_empty_string() -> None:
    text = "# T\n\n" + _pair("blank", "")
    assert extract_host_local_marker_bodies(text) == {"blank": ""}


def test_multi_section_preserves_top_to_bottom_order() -> None:
    text = (
        "# T\n\n"
        + _pair("a", "aa\n")
        + "\n"
        + _pair("b", "bb\n")
        + "\n"
        + _pair("c", "cc\n")
    )
    assert list(extract_host_local_marker_bodies(text)) == ["a", "b", "c"]


def test_duplicate_host_local_name_refuses() -> None:
    text = _pair("dup", "a\n") + _pair("dup", "b\n")
    # MarkerError (a SetforgeError) so the CLI exits clean, not a raw traceback.
    with pytest.raises(MarkerError, match="duplicate host-local"):
        extract_host_local_marker_bodies(text)


def test_no_markers_returns_empty() -> None:
    assert extract_host_local_marker_bodies("# just text\n") == {}


# --- Task 2: build_overlay_span_node + append_overlay_spans ------------------


def test_append_writes_at_eof_overlay_span(tmp_path: Path) -> None:
    local = tmp_path / "local.yaml"
    local.write_text("# c\ntracked_files:\n  doc: {}\n", encoding="utf-8")
    n = append_overlay_spans(local, {"doc": [("notes", "host notes\n")]})
    assert n == 1
    overlay = load_local_tracked_file_overlays(local)["doc"]
    span = overlay.spans[0]
    assert span.kind is SpanKind.OVERLAY
    assert span.anchor == "notes"
    assert span.overlay is not None
    assert span.overlay.body == "host notes\n"
    assert "# c" in local.read_text(encoding="utf-8")  # comment preserved


def test_append_canonicalizes_body(tmp_path: Path) -> None:
    local = tmp_path / "local.yaml"
    local.write_text("tracked_files:\n  doc: {}\n", encoding="utf-8")
    append_overlay_spans(local, {"doc": [("notes", "no trailing newline")]})
    overlay = load_local_tracked_file_overlays(local)["doc"]
    assert overlay.spans[0].overlay is not None
    assert overlay.spans[0].overlay.body == canonical_body("no trailing newline")


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
    overlay = load_local_tracked_file_overlays(local)["doc"]
    assert overlay.spans[0].anchor == "notes"


def test_append_creates_local_yaml_when_absent(tmp_path: Path) -> None:
    """Absent local.yaml is created — never silently lose bodies to a no-op."""
    local = tmp_path / "local.yaml"
    assert not local.exists()
    assert append_overlay_spans(local, {"doc": [("notes", "b\n")]}) == 1
    assert local.exists()
    overlay = load_local_tracked_file_overlays(local)["doc"]
    assert overlay.spans[0].anchor == "notes"


def test_build_node_shape() -> None:
    node = build_overlay_span_node("notes", "body\n")
    assert node["anchor"] == "notes"
    assert node["kind"] == "overlay"
    assert node["semantics"] == "host-local"
    assert node["overlay"]["anchor"]["kind"] == "at-end-of-file"
    assert node["overlay"]["body"] == "body\n"
