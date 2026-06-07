"""Physical ``local.yaml`` rewrite: retire ``host_local_sections`` → OVERLAY spans.

The legacy host-local mechanism stored each host-local body under
``tracked_files.<id>.host_local_sections.<name>`` as ``{anchor, body|body_file}``.
The unified span model represents the same intent as an OVERLAY ``spans`` entry::

    tracked_files:
      <id>:
        spans:
          - anchor: <name>           # the span IDENTITY (the legacy section name)
            kind: overlay
            semantics: host-local
            anchor: <structured>     # the 5-kind splice point, copied verbatim
            body: ...                # (or body_file:) copied verbatim

This module performs the ON-DISK rewrite so the canonical representation matches
the new model. The parse-time OVERLAY path stays the runtime contract; the
existing ``host_local_sections`` loader remains a back-compat shim for hosts that
have not yet been rewritten.

Design points (see the bug list in the 10.2 spec):

- **ruamel round-trip.** The rewrite goes through
  :func:`setforge.migrations._yaml_ops.atomic_write_yaml`, which preserves
  comments, key order, quoting, and the destination's file mode. The legacy
  ``anchor`` / ``body`` / ``body_file`` sub-nodes are MOVED (not re-serialized
  from a parsed model) so their comments and scalar styles survive.
- **Idempotent.** :func:`migrate_local_yaml_overlay_spans` is a presence-check —
  a file with no ``host_local_sections`` blocks (already migrated, or never had
  any) is left byte-for-byte untouched and reports ``migrated=False``.
- **Mode + bytes preserved.** ``atomic_write_yaml`` copies the existing mode onto
  the rewritten file; the install transition snapshots the pre-migration
  ``local.yaml`` bytes so ``revert`` restores the exact prior content.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from setforge.migrations._yaml_ops import atomic_write_yaml, yaml_rt

__all__ = ["OverlayMigrationResult", "migrate_local_yaml_overlay_spans"]


@dataclass(slots=True, frozen=True)
class OverlayMigrationResult:
    """Outcome of a :func:`migrate_local_yaml_overlay_spans` call.

    ``migrated`` is ``True`` only when at least one
    ``host_local_sections`` block was rewritten into ``spans`` (i.e. the
    file was physically changed). ``section_count`` is the number of
    individual host-local sections moved across every tracked_file. Both
    are ``False`` / ``0`` on the idempotent no-op path so the install hook
    can warn exactly once.
    """

    migrated: bool
    section_count: int


def _build_overlay_span(name: str, section: CommentedMap) -> CommentedMap:
    """Return a new OVERLAY ``spans`` entry built from a legacy section node.

    ``name`` becomes the span's top-level ``anchor`` IDENTITY (the
    anchor-keyed sidecar key); the legacy section map (``anchor`` /
    ``body`` / ``body_file`` plus its attached comments) is REUSED VERBATIM
    as the nested ``overlay:`` payload. Reusing the original
    :class:`CommentedMap` — rather than copying values into a fresh map —
    keeps every comment token attached, including a section-leading comment
    stored on the map's own ``ca.comment`` (which a per-key value copy would
    orphan).
    """
    entry = CommentedMap()
    # Insertion order mirrors the documented OVERLAY span shape: identity
    # anchor, kind, semantics, then the nested overlay payload (the original
    # section map, comments and scalar styles intact).
    entry["anchor"] = name
    entry["kind"] = "overlay"
    entry["semantics"] = "host-local"
    entry["overlay"] = section
    return entry


def _migrate_tracked_file(tracked_file: CommentedMap) -> int:
    """Rewrite one tracked_file's ``host_local_sections`` into ``spans``.

    Returns the number of sections moved (0 when the tracked_file declares
    no ``host_local_sections``). Mutates ``tracked_file`` in place: the
    new OVERLAY entries are APPENDED to any existing ``spans`` sequence
    (created if absent), and the ``host_local_sections`` key is removed.
    """
    sections = tracked_file.get("host_local_sections")
    if not isinstance(sections, CommentedMap) or not sections:
        return 0
    spans = tracked_file.get("spans")
    if not isinstance(spans, CommentedSeq):
        spans = CommentedSeq()
        tracked_file["spans"] = spans
    moved = 0
    for name, section in sections.items():
        if not isinstance(section, CommentedMap):
            # A malformed entry (non-mapping section) is left for the
            # schema validator to reject; never silently drop it.
            continue
        spans.append(_build_overlay_span(str(name), section))
        moved += 1
    if moved:
        del tracked_file["host_local_sections"]
    return moved


def migrate_local_yaml_overlay_spans(
    path: Path,
) -> OverlayMigrationResult:
    """Rewrite ``path`` in place, retiring ``host_local_sections`` → OVERLAY spans.

    Walks every ``tracked_files.<id>`` block; for each that declares
    ``host_local_sections``, moves each ``<name>: {anchor, body|body_file}``
    section into a ``spans`` OVERLAY entry (``{anchor: <name>, kind: overlay,
    semantics: host-local, overlay: {anchor, body|body_file}}``) and drops the
    legacy block. The write goes through
    :func:`setforge.migrations._yaml_ops.atomic_write_yaml` (ruamel round-trip,
    mode + comment + order preserving, fsynced).

    Idempotent: a file with no ``host_local_sections`` (already migrated, never
    had any, absent, or empty) is left byte-for-byte untouched and reports
    ``migrated=False`` — NO write occurs, so re-running converges.

    Returns an :class:`OverlayMigrationResult` so the caller can warn once on a
    real migration and stay silent on the steady-state read.
    """
    if not path.exists():
        return OverlayMigrationResult(migrated=False, section_count=0)
    yaml = yaml_rt()
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.load(fh)
    if not isinstance(data, CommentedMap):
        # An empty / non-mapping local.yaml has no tracked_files to migrate.
        return OverlayMigrationResult(migrated=False, section_count=0)
    tracked_files = data.get("tracked_files")
    if not isinstance(tracked_files, CommentedMap):
        return OverlayMigrationResult(migrated=False, section_count=0)
    total = 0
    for tracked_file in tracked_files.values():
        if isinstance(tracked_file, CommentedMap):
            total += _migrate_tracked_file(tracked_file)
    if total == 0:
        # No-op: never rewrite a file that needs no migration (preserves
        # byte-for-byte identity for the idempotent / already-migrated case).
        return OverlayMigrationResult(migrated=False, section_count=0)
    atomic_write_yaml(path, data)
    return OverlayMigrationResult(migrated=True, section_count=total)
