"""Read host-local markdown section headings back OUT of the reconcile store.

Host-local markdown sections live ONLY in the reconcile per-unit store, as
LOCAL line units carrying a stable ``reloc_anchor`` heading identity (minted in
:func:`setforge.reconcile.hunks.serialize`). This module reports which headings
a tracked file currently holds that way. Two readers, both needing only the
heading names: :func:`host_local_headings_from_store` (the fold-idempotency gate
of the span-surface-retire migration) and :func:`seeded_headings_from_rows` (the
seed-once gate of
:func:`setforge.reconcile.host_local_record.seed_section_slots_to_store`).

A store unit is a host-local section iff its persisted index row is
``cls == "local"`` AND carries a ``reloc_anchor``. The persisted rows hold no byte
spans (spans are recomputed fresh every run — see :mod:`setforge.reconcile.hunks`),
so the base->local diff is re-extracted and the engine's own
:func:`~setforge.reconcile.hunks.classify` carries the stored class +
``reloc_anchor`` onto the fresh hunks. A row with no matching hunk in that diff
does not count, so the answer follows the recorded base/local bytes, not the rows
alone.

:func:`seeded_headings_from_rows` reads the rows alone, so a row stays counted
after the section it describes is gone from the recorded base/local bytes.
"""

from __future__ import annotations

from setforge.reconcile import store
from setforge.reconcile.hunks import classify, extract_hunks
from setforge.reconcile.types import FileId, HunkClass

__all__ = ["host_local_headings_from_store", "seeded_headings_from_rows"]


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


def seeded_headings_from_rows(profile: str, fid: FileId) -> set[str]:
    """Return the headings of ``fid``'s LOCAL+``reloc_anchor`` index rows.

    Reads the rows only, with no base/local re-diff: a seeded section the user
    later deleted keeps its row, so it still counts as seeded.
    """
    entry = store.read_index(profile).files.get(str(fid))
    if entry is None:
        return set()
    return {
        anchor
        for row in entry.hunks
        if row.get("cls") == HunkClass.LOCAL.value
        and isinstance(anchor := row.get("reloc_anchor"), str)
    }
