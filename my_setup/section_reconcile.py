"""Install-side three-way reconciliation for `shared` user-sections.

Closes the gap surfaced by dotfiles-23k: when tracked content INSIDE a
``<!-- my-setup:user-section start shared X -->`` region is updated
(e.g., a new bullet in the Workflow rules), today's install preserves
the live body unconditionally and the new tracked content never lands.
This module supplies the per-section state classifier and the install
hash-maintenance write helper that bring tracked-side updates into the
fold without surprising the user on bare ``my-setup install`` runs.

The classifier is pure: given (tracked text, live text), it returns a
deterministic state per shared section, derived from
:func:`my_setup.sections.hash_sections` (actual body) and
:func:`my_setup.sections.extract_marker_hashes` (recorded baseline).
The CLI consumes that classification to decide warn / prompt / silent.

Host-local sections always silently keep the live body, regardless of
embedded-hash state — they exist precisely to opt out of tracked-side
updates.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from my_setup.sections import (
    SectionSemantics,
    extract_marker_hashes,
    extract_sections,
    hash_sections,
    section_semantics,
    set_marker_hashes,
)

__all__ = [
    "SectionDrift",
    "SectionDriftState",
    "classify_section_drift",
    "maintain_marker_hashes",
    "stamp_tracked_baseline",
]


class SectionDriftState(StrEnum):
    """Closed set of per-section drift states for the install reconciler.

    - ``NO_DRIFT`` — tracked and live bodies match (``A_T == A_L``);
      no-op. Default + ``--reconcile-user-sections`` both silent.
    - ``LEGACY`` — one or both sides have no embedded hash (``E_T``
      or ``E_L`` is ``None``); cannot run three-way logic, fall back
      to two-way keep-live. Default: warn + keep-live;
      ``--reconcile-user-sections``: prompt with no baseline info.
    - ``PENDING_TRACKED`` — live is pristine relative to its embedded
      hash (``A_L == E_L``) AND tracked has new updates
      (``A_T != E_T``). The intended "deliver tracked-side rule
      updates" path. Default: warn + keep-live;
      ``--reconcile-user-sections``: prompt.
    - ``LIVE_EDITED`` — user has edited live since install
      (``A_L != E_L``) AND tracked is at its last-known baseline
      (``A_T == E_T``). Default: silent keep-live;
      ``--reconcile-user-sections``: prompt.
    - ``CONFLICT`` — both sides have moved (``A_L != E_L`` AND
      ``A_T != E_T``). Genuine three-way conflict. Default: warn
      (LOUDER) + keep-live; ``--reconcile-user-sections``: prompt.
    - ``INCONSISTENT`` — ``A_L == E_L`` AND ``A_T == E_T`` AND
      ``A_T != A_L``. Shouldn't happen (hashes agree on both sides
      but bodies differ). Treat as :attr:`CONFLICT` — warn + keep-live.
    """

    NO_DRIFT = "no_drift"
    LEGACY = "legacy"
    PENDING_TRACKED = "pending_tracked"
    LIVE_EDITED = "live_edited"
    CONFLICT = "conflict"
    INCONSISTENT = "inconsistent"


@dataclass(frozen=True, slots=True)
class SectionDrift:
    """One section's classification record.

    ``name`` matches the section's start-marker name (or ``"0"`` /
    ``"1"`` / ... for unnamed sections). ``semantics`` mirrors the
    ``host-local`` / ``shared`` keyword. ``state`` is the per-section
    decision. ``tracked_body`` and ``live_body`` are the raw bodies
    pulled from :func:`extract_sections` so the wizard can render
    diffs without re-parsing.
    """

    name: str
    semantics: SectionSemantics
    state: SectionDriftState
    tracked_body: str
    live_body: str


def classify_section_drift(
    tracked_text: str, live_text: str
) -> dict[str, SectionDrift]:
    """Return one :class:`SectionDrift` per section present in both texts.

    Iteration order matches ``extract_sections(tracked_text)`` insertion
    order — deterministic across runs, the contract the wizard relies on
    when it asks "next section?". Sections that exist in tracked but not
    in live (or vice versa) are silently skipped here; the deploy path
    handles those via :func:`my_setup.sections.merge_sections`'s
    placeholder behaviour. ``set_marker_hashes`` callers are likewise
    expected to operate on the post-merge content.

    For ``host-local`` sections the state is always
    :attr:`SectionDriftState.NO_DRIFT` from the *reconciler*'s point of
    view — they're not subject to the three-way logic. The caller
    chooses behaviour based on ``semantics``; this function reports the
    state honestly so a debugging dump shows "host-local + bodies
    differ" rather than synthesizing a fake :attr:`LIVE_EDITED`.

    Raises :class:`my_setup.errors.MarkerError` via the section
    primitives on malformed markers.
    """
    tracked_bodies = extract_sections(tracked_text)
    live_bodies = extract_sections(live_text)
    tracked_hashes = hash_sections(tracked_text)
    live_hashes = hash_sections(live_text)
    tracked_embedded = extract_marker_hashes(tracked_text)
    live_embedded = extract_marker_hashes(live_text)
    semantics_map = section_semantics(tracked_text)

    out: dict[str, SectionDrift] = {}
    for name in tracked_bodies:
        if name not in live_bodies:
            continue
        a_t = tracked_hashes[name]
        a_l = live_hashes[name]
        e_t = tracked_embedded.get(name)
        e_l = live_embedded.get(name)
        state = _classify_one(a_t=a_t, a_l=a_l, e_t=e_t, e_l=e_l)
        out[name] = SectionDrift(
            name=name,
            semantics=SectionSemantics(semantics_map[name]),
            state=state,
            tracked_body=tracked_bodies[name],
            live_body=live_bodies[name],
        )
    return out


def _classify_one(
    *, a_t: str, a_l: str, e_t: str | None, e_l: str | None
) -> SectionDriftState:
    """Map one (A_T, A_L, E_T, E_L) tuple to a :class:`SectionDriftState`.

    Order of checks matches the design table in the bd contract:

    1. Bodies identical → no drift.
    2. Either embedded hash missing → legacy fallback.
    3. Live pristine + tracked moved → pending tracked update.
    4. Live moved + tracked pristine → live-side edits.
    5. Both moved → conflict.
    6. Both report pristine but bodies differ → inconsistent.
    """
    if a_t == a_l:
        return SectionDriftState.NO_DRIFT
    if e_t is None or e_l is None:
        return SectionDriftState.LEGACY
    live_pristine = a_l == e_l
    tracked_pristine = a_t == e_t
    if live_pristine and not tracked_pristine:
        return SectionDriftState.PENDING_TRACKED
    if not live_pristine and tracked_pristine:
        return SectionDriftState.LIVE_EDITED
    if not live_pristine and not tracked_pristine:
        return SectionDriftState.CONFLICT
    return SectionDriftState.INCONSISTENT


def maintain_marker_hashes(text: str) -> str:
    """Rewrite every end-marker's ``hash=<...>`` to match its body content.

    Composition of :func:`my_setup.sections.hash_sections` and
    :func:`my_setup.sections.set_marker_hashes`. Idempotent: applying it
    twice yields the same output as applying it once (set_marker_hashes
    is byte-preserving outside the end-marker line, and the body it
    hashes is unchanged).

    Called by the install path after computing the final live content so
    every post-install live file satisfies the invariant
    ``extract_marker_hashes(text) == hash_sections(text)`` (modulo
    ``None`` entries — there shouldn't be any post-install).

    Raises :class:`my_setup.errors.MarkerError` on malformed markers.
    """
    return set_marker_hashes(text, hash_sections(text))


def stamp_tracked_baseline(tracked_path: Path) -> bool:
    """Rewrite ``tracked_path`` so every end marker carries ``hash=A_T``.

    The three-way classifier needs an embedded baseline hash on BOTH
    tracked and live to discriminate ``PENDING_TRACKED`` /
    ``LIVE_EDITED`` / ``CONFLICT``. The live side is stamped on every
    successful ``copy_atomic`` via :func:`maintain_marker_hashes`; this
    helper does the symmetric job for the tracked side so the next
    install can reason about drift.

    Behavior:

    - If every section's embedded hash already matches its body
      (``extract_marker_hashes(text) == hash_sections(text)`` and no
      ``None`` entries), returns ``False`` and performs no write —
      avoids spurious ``git diff`` noise in CI on already-aligned
      tracked files.
    - Otherwise, rewrites ``tracked_path`` with
      ``set_marker_hashes(text, hash_sections(text))`` so post-install
      ``E_T == A_T`` for every section, and returns ``True``.

    Install MUTATES tracked content here, but only the ``hash=`` metadata
    in end markers — the section BODY and all other content stay
    byte-for-byte identical. ``my-setup compare`` stays fully read-only
    on tracked (it does NOT call this); compare may therefore report
    ``LEGACY`` for sections without a prior baseline. The next ``install``
    fixes that.

    Raises :class:`my_setup.errors.MarkerError` on malformed markers.
    """
    text = tracked_path.read_text(encoding="utf-8")
    actual = hash_sections(text)
    embedded = extract_marker_hashes(text)
    if all(embedded.get(name) == digest for name, digest in actual.items()):
        return False
    new_text = set_marker_hashes(text, actual)
    tracked_path.write_text(new_text, encoding="utf-8")
    return True


def has_shared_drift(drifts: Mapping[str, SectionDrift]) -> bool:
    """True iff at least one ``shared`` section has a non-:attr:`NO_DRIFT` state.

    Convenience for the install path's warning gate: a single boolean
    answer to "is there anything the bare install user needs to know
    about?". host-local sections are excluded — they never need a
    bare-install warning because they're contractually opted out of
    tracked-side updates.
    """
    return any(
        d.semantics is SectionSemantics.SHARED
        and d.state is not SectionDriftState.NO_DRIFT
        for d in drifts.values()
    )
