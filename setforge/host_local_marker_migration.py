"""Capture live host-local marker bodies → ``local.yaml`` OVERLAY spans (14.17).

Increment-3 of the host-local de-marker conversion. On the first install after a
host adopts the markerless model, each host-local section's per-host body lives
ONLY inside the deployed live file's ``<!-- setforge:user-section ... host-local
NAME -->`` marker region (preserved across installs by ``preserve_user_sections``
section-merge, never stored in ``local.yaml``). Increment 2 made ``deploy``
blanket-strip every host-local marker pair, so unless those bodies are first
captured into ``local.yaml`` OVERLAY spans they are permanently deleted.

This module performs the READ + the on-disk ``local.yaml`` write. It never
touches the live file (``deploy`` strips it) and never mutates the in-memory
config (the install re-resolves the overlay from the rewritten ``local.yaml``).
"""

from __future__ import annotations

from pathlib import Path

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from setforge.migrations._yaml_ops import atomic_write_yaml, yaml_rt
from setforge.overlay_inject import canonical_body
from setforge.sections import (
    SectionSemantics,
    _BodyLine,
    _EndMarker,
    _StartMarker,
    _walk_markers,
)


def extract_host_local_marker_bodies(text: str) -> dict[str, str]:
    """Return ``{name: body}`` for every HOST_LOCAL marker region in ``text``.

    Body is the exact bytes between the markers (trailing newline included, up
    to but not including the end-marker line), matching
    :func:`setforge.sections.extract_sections`. Shared regions are ignored.

    Raises :class:`ValueError` on a duplicate host-local name — silently
    dropping one (the dict last-wins behavior of ``extract_sections``) would
    lose a per-host body before it could be captured.
    """
    bodies: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    for event in _walk_markers(text, allow_legacy=True):
        match event:
            case _StartMarker(semantics=SectionSemantics.HOST_LOCAL, name=name):
                current = name
                lines = []
            case _EndMarker(semantics=SectionSemantics.HOST_LOCAL, key=key):
                if key in bodies:
                    raise ValueError(f"duplicate host-local section name {key!r}")
                bodies[key] = "".join(lines)
                current = None
            case _BodyLine(line=line) if current is not None:
                lines.append(line)
            case _:
                pass
    return bodies


def build_overlay_span_node(name: str, body: str) -> CommentedMap:
    """Build an at-end-of-file host-local OVERLAY span YAML node.

    Mirrors the shape :func:`setforge.overlay_migration._build_overlay_span`
    produces: identity ``anchor`` (the section name), ``kind: overlay``,
    ``semantics: host-local``, and the nested ``overlay`` payload carrying the
    structured ``at-end-of-file`` splice anchor + the canonicalized body. The
    body is canonicalized (:func:`setforge.overlay_inject.canonical_body`) so
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
    preserving, comments / key order intact).
    """
    if not additions or not local_path.exists():
        return 0
    yaml = yaml_rt()
    with local_path.open("r", encoding="utf-8") as fh:
        data = yaml.load(fh)
    if not isinstance(data, CommentedMap):
        return 0
    tracked_files = data.get("tracked_files")
    if not isinstance(tracked_files, CommentedMap):
        return 0
    written = 0
    for file_id, entries in additions.items():
        tracked_file = tracked_files.get(file_id)
        if not isinstance(tracked_file, CommentedMap):
            tracked_file = CommentedMap()
            tracked_files[file_id] = tracked_file
        existing = _existing_overlay_anchors(tracked_file)
        spans = tracked_file.get("spans")
        if not isinstance(spans, CommentedSeq):
            spans = CommentedSeq()
            tracked_file["spans"] = spans
        for name, body in entries:
            if name in existing:
                continue
            spans.append(build_overlay_span_node(name, body))
            written += 1
    if written == 0:
        return 0
    atomic_write_yaml(local_path, data)
    return written
