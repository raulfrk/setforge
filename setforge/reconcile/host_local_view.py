"""Project host-local markdown sections back OUT of the reconcile store.

STAGE B retires the ``local.yaml`` ``host_local_sections`` / ``spans``
declaration of host-local markdown sections. Afterwards host-local content lives
ONLY in the reconcile per-unit store, as LOCAL line units carrying a stable
``reloc_anchor`` heading identity (minted in
:func:`setforge.reconcile.hunks.serialize`). This module is the REPLACEMENT
projection for :func:`setforge.source.load_local_host_local_sections`: it reads
those units back into the SAME
``{tracked_file_id: {section_name: HostLocalSection}}`` shape, so the legacy
consumers (compare drift-mask, validate anchor-legality, capture strip, install
seed, dry-run display) repoint here without changing their downstream code.

A store unit is a host-local section iff its persisted index row is
``cls == "local"`` AND carries a ``reloc_anchor``. The persisted rows hold no byte
spans (spans are recomputed fresh every run — see :mod:`setforge.reconcile.hunks`),
so the section body is recovered by re-extracting the base->local diff, running the
engine's own :func:`~setforge.reconcile.hunks.classify` to carry the stored
class + ``reloc_anchor`` onto the fresh (spanned) hunks, then slicing the
LOCAL+reloc region out of the verbatim ``local`` bytes. Reusing ``classify`` keeps
the identity match byte-for-byte aligned with how the staging layer wrote the row.
"""

from __future__ import annotations

from setforge.anchors import AnchorAfterHeading
from setforge.reconcile import store
from setforge.reconcile.hunks import classify, extract_hunks
from setforge.reconcile.merge import split_lines
from setforge.reconcile.types import FileId, HunkClass, file_id
from setforge.source import HostLocalSection, HostLocalSectionName

__all__ = ["host_local_sections_from_store"]


def host_local_sections_from_store(
    profile: str, fid: FileId | None = None
) -> dict[str, dict[HostLocalSectionName, HostLocalSection]]:
    """Return the host-local sections represented in ``profile``'s reconcile store.

    Shape-interchangeable with
    :func:`setforge.source.load_local_host_local_sections`:
    ``{tracked_file_id: {section_name: HostLocalSection}}``, with the inner map
    keyed by the provenance-marked :data:`HostLocalSectionName` and any file that
    projects to no section dropped (so ``tf_id in result`` means "has at least one
    host-local section"). Pass ``fid`` to scope the projection to a single tracked
    file; ``None`` (the default) scans every indexed file for the profile.
    """
    index = store.read_index(profile)
    keys = [str(fid)] if fid is not None else list(index.files)
    result: dict[str, dict[HostLocalSectionName, HostLocalSection]] = {}
    for key in keys:
        entry = index.files.get(key)
        if entry is None:
            continue
        sections = _sections_for_file(profile, file_id(key), entry.hunks)
        if sections:
            result[key] = sections
    return result


def _sections_for_file(
    profile: str, fid: FileId, stored: list[dict[str, object]]
) -> dict[HostLocalSectionName, HostLocalSection]:
    """Project one file's LOCAL+reloc_anchor units to :class:`HostLocalSection` s."""
    # Fast exit: no LOCAL row carries a reloc_anchor -> no host-local section here,
    # so skip the base/local read + re-extract entirely.
    if not any(
        row.get("cls") == HunkClass.LOCAL.value and row.get("reloc_anchor") is not None
        for row in stored
    ):
        return {}
    base = store.read_base(profile, fid)
    local = store.read_local(profile, fid)
    # A reloc row implies base + local were recorded together (store.record writes
    # both before the index). A missing/absent side means the store is mid-write or
    # inconsistent, so there is nothing to reconstruct — fail soft, project nothing.
    if base is None or not isinstance(local, bytes):
        return {}
    local_lines = split_lines(local)
    sections: dict[HostLocalSectionName, HostLocalSection] = {}
    for hunk in classify(extract_hunks(base, local), stored):
        if hunk.cls is not HunkClass.LOCAL or hunk.reloc_anchor is None:
            continue
        j1, j2 = hunk.live_span
        body = b"".join(local_lines[j1:j2]).decode("utf-8")
        sections[HostLocalSectionName(hunk.reloc_anchor)] = HostLocalSection(
            anchor=AnchorAfterHeading(value=hunk.reloc_anchor),
            body=body,
        )
    return sections
