"""Read host-local markdown section headings back OUT of the reconcile store.

Host-local markdown sections live ONLY in the reconcile per-unit store, as
LOCAL line units carrying a stable ``reloc_anchor`` heading identity (minted in
:func:`setforge.reconcile.hunks.serialize`). This module reports which headings
a tracked file currently holds that way. Its callers are the seed-once gate of
:func:`setforge.reconcile.host_local_record.seed_section_slots_to_store` and the
fold-idempotency gate of the span-surface-retire migration; both need only the
heading names.

A store unit is a host-local section iff its persisted index row is
``cls == "local"`` AND carries a ``reloc_anchor``. The persisted rows hold no byte
spans (spans are recomputed fresh every run — see :mod:`setforge.reconcile.hunks`),
so the base->local diff is re-extracted and the engine's own
:func:`~setforge.reconcile.hunks.classify` carries the stored class +
``reloc_anchor`` onto the fresh hunks. A row with no matching hunk in that diff
does not count, so the answer follows the recorded base/local bytes, not the rows
alone.
"""

from __future__ import annotations

from setforge.reconcile import store
from setforge.reconcile.hunks import classify, extract_hunks
from setforge.reconcile.types import FileId, HunkClass

__all__ = ["host_local_headings_from_store"]


def host_local_headings_from_store(profile: str, fid: FileId) -> set[str]:
    """Return the host-local section headings recorded for ``fid`` in ``profile``.

    Empty when the file has no LOCAL+``reloc_anchor`` unit that matches a hunk of
    its recorded base->local diff, and also when the base or local leg is missing
    or recorded absent (the store is mid-write; fail soft).
    """
    entry = store.read_index(profile).files.get(str(fid))
    if entry is None:
        return set()
    stored = entry.hunks
    # Fast exit: skip the base/local read entirely when no row needs it.
    if not any(
        row.get("cls") == HunkClass.LOCAL.value and row.get("reloc_anchor") is not None
        for row in stored
    ):
        return set()
    base = store.read_base(profile, fid)
    local = store.read_local(profile, fid)
    if base is None or not isinstance(local, bytes):
        return set()
    return {
        hunk.reloc_anchor
        for hunk in classify(extract_hunks(base, local), stored)
        if hunk.cls is HunkClass.LOCAL and hunk.reloc_anchor is not None
    }
