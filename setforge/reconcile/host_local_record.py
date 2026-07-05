"""Record host-local additive sections INTO the reconcile store as LOCAL units.

The write-in counterpart to :mod:`setforge.reconcile.host_local_view` (which
reads host-local sections back OUT). Two call sites inject additive markdown
sections at end-of-file and then persist them as LOCAL units carrying a stable
``reloc_anchor`` heading identity:

- the install-time template seed
  (``setforge.section_templates.seed_section_slots_to_store``), and
- the 3.0 -> 4.0 span-surface-retire migration fold
  (``setforge.migrations._span_surface_retire._fold_sections``).

Both share the SAME two operations — deriving a body's heading identity and the
re-classify/mark-LOCAL/serialize/record dance — which MUST agree byte-for-byte
with the ``reloc_anchor`` :func:`setforge.reconcile.hunks.serialize` mints, so
the seed-once gate, the fold-idempotency gate, and the projection back out all
line up. Housing both here keeps that guarantee in one place instead of two
verbatim copies that can silently diverge.
"""

from __future__ import annotations

from dataclasses import replace

from setforge.reconcile import store
from setforge.reconcile.hunks import (
    _section_heading,
    classify,
    extract_hunks,
    serialize,
)
from setforge.reconcile.types import FileId, HunkClass

__all__ = ["record_local_reloc_sections", "section_heading_of_body"]


def section_heading_of_body(body: bytes) -> str | None:
    """The heading identity a canonical section body will mint, or ``None``.

    Runs the SAME derivation the store uses when it persists a LOCAL host-local
    unit — :func:`setforge.reconcile.hunks._section_heading` over the hunk
    :func:`~setforge.reconcile.hunks.extract_hunks` produces for a pure insert of
    the body against an empty base — so the seed-once gate, the fold-idempotency
    comparison, and the LOCAL marking all agree byte-for-byte with the
    ``reloc_anchor`` :func:`~setforge.reconcile.hunks.serialize` mints. Reads the
    body's OWN first heading only (empty base ⇒ no neighbour heading to borrow),
    so a headingless body returns ``None`` and is refused up front rather than
    silently minting a neighbour's heading.
    """
    hunks = extract_hunks(b"", body)
    return _section_heading(hunks[0]) if hunks else None


def record_local_reloc_sections(
    profile: str,
    fid: FileId,
    *,
    base: bytes,
    new_local: bytes,
    existing_hunks: list[dict[str, object]],
    residual_headings: set[str],
) -> None:
    """Re-classify ``base -> new_local`` and record the injected sections LOCAL.

    Call inside the profile lock, AFTER ``new_local`` has the residual section
    bodies injected. Re-extracts the ``base -> new_local`` hunks and
    :func:`~setforge.reconcile.hunks.classify` them against ``existing_hunks`` so
    every prior classification (SHARED / LOCAL / SHARED_DRAFTED, INV-8 stage
    fidelity) is carried forward untouched; marks ONLY the freshly-injected
    section hunks LOCAL — a still-PENDING hunk whose own heading is one of
    ``residual_headings`` — so :func:`~setforge.reconcile.hunks.serialize` mints
    their ``reloc_anchor``; then :func:`~setforge.reconcile.store.record` s the
    merged base+local+hunks (drafts preserved: ``record`` leaves the on-disk
    drafts manifest intact when passed no ``drafts=``).
    """
    classified = classify(extract_hunks(base, new_local), existing_hunks)
    rows = serialize(
        [
            replace(hunk, cls=HunkClass.LOCAL)
            if hunk.cls is HunkClass.PENDING
            and _section_heading(hunk) in residual_headings
            else hunk
            for hunk in classified
        ]
    )
    store.record(profile, fid, base=base, local=new_local, hunks=rows)
