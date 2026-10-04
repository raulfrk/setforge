"""Capture live host-local marker bodies → ``local.yaml`` OVERLAY spans.

Increment-3 of the host-local de-marker conversion. On the first install after a
host adopts the markerless model, each host-local section's per-host body lives
ONLY inside the deployed live file's ``<!-- setforge:user-section ... host-local
NAME -->`` marker region (preserved across installs by ``preserve_user_sections``
section-merge, never stored in ``local.yaml``). Increment 2 made ``deploy``
blanket-strip every host-local marker pair, so unless those bodies are first
captured into ``local.yaml`` OVERLAY spans they are permanently deleted.

This module performs the on-disk ``local.yaml`` write; the old-schema
migrations read the bodies. It never touches the live file and never mutates
the in-memory config.
"""

from __future__ import annotations

from pathlib import Path

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from setforge.body_canon import canonical_body
from setforge.migrations._yaml_ops import atomic_write_yaml, yaml_rt


def build_overlay_span_node(name: str, body: str) -> CommentedMap:
    """Build an at-end-of-file host-local OVERLAY span YAML node.

    Mirrors the shape :func:`setforge.migrations._local_yaml._build_overlay_span`
    produces: identity ``anchor`` (the section name), ``kind: overlay``,
    ``semantics: host-local``, and the nested ``overlay`` payload carrying the
    structured ``at-end-of-file`` splice anchor + the canonicalized body. The
    body is canonicalized (:func:`setforge.body_canon.canonical_body`) so
    deploy-inject ↔ capture-excise round-trip byte-exact.
    """
    anchor = CommentedMap()
    anchor["kind"] = "at-end-of-file"
    payload = CommentedMap()
    payload["anchor"] = anchor
    payload["body"] = canonical_body(body)
    entry = CommentedMap()
    entry["anchor"] = name
    entry["kind"] = "overlay"
    entry["semantics"] = "host-local"
    entry["overlay"] = payload
    return entry


def _existing_overlay_anchors(tracked_file: CommentedMap) -> set[str]:
    """Return the set of OVERLAY span anchor names already on ``tracked_file``."""
    spans = tracked_file.get("spans")
    if not isinstance(spans, CommentedSeq):
        return set()
    return {
        str(span.get("anchor"))
        for span in spans
        if isinstance(span, CommentedMap) and span.get("kind") == "overlay"
    }


def _load_or_init_local_doc(local_path: Path) -> CommentedMap:
    """Load ``local.yaml`` as a ruamel doc, or a fresh map when absent/malformed."""
    if not local_path.exists():
        return CommentedMap()
    with local_path.open("r", encoding="utf-8") as fh:
        data = yaml_rt().load(fh)
    return data if isinstance(data, CommentedMap) else CommentedMap()


def _tracked_files_block(data: CommentedMap) -> CommentedMap:
    """Return ``data['tracked_files']`` as a CommentedMap, creating it if absent."""
    tracked_files = data.get("tracked_files")
    if not isinstance(tracked_files, CommentedMap):
        tracked_files = CommentedMap()
        data["tracked_files"] = tracked_files
    return tracked_files


def _spans_seq(tracked_file: CommentedMap) -> CommentedSeq:
    """Return ``tracked_file['spans']`` as a CommentedSeq, creating it if absent."""
    spans = tracked_file.get("spans")
    if not isinstance(spans, CommentedSeq):
        spans = CommentedSeq()
        tracked_file["spans"] = spans
    return spans


def append_overlay_spans(
    local_path: Path, additions: dict[str, list[tuple[str, str]]]
) -> int:
    """Append at-end-of-file host-local OVERLAY spans to ``local.yaml``.

    ``additions`` maps ``tracked_file_id -> [(section_name, body), ...]``.
    Returns the number of spans actually written. A name already present as an
    OVERLAY span anchor on that tracked_file is SKIPPED (crash-resume /
    idempotency — re-running converges without duplicating spans). Writes
    nothing (returns 0) when there is nothing new to add or the file is
    absent / malformed.

    The write is a single ruamel round-trip via
    :func:`setforge.migrations._yaml_ops.atomic_write_yaml` (fsync + file-mode
    preserving, comments / key order intact). When ``local.yaml`` is absent it
    is CREATED — never silently no-op while there are bodies to capture, which
    would let the following deploy delete them.
    """
    if not additions:
        return 0
    data = _load_or_init_local_doc(local_path)
    tracked_files = _tracked_files_block(data)
    written = 0
    for file_id, entries in additions.items():
        tracked_file = tracked_files.get(file_id)
        if not isinstance(tracked_file, CommentedMap):
            tracked_file = CommentedMap()
            tracked_files[file_id] = tracked_file
        existing = _existing_overlay_anchors(tracked_file)
        spans = _spans_seq(tracked_file)
        for name, body in entries:
            if name in existing:
                continue
            spans.append(build_overlay_span_node(name, body))
            written += 1
    if written == 0:
        return 0
    atomic_write_yaml(local_path, data)
    return written
