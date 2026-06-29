"""Per-hunk staging model for A5 (RFC §9.3): extract base↔live diff hunks,
classify them by a stable identity, reconstruct the shared-promotion of a file,
and assert stage fidelity (INV-8).

A **leaf module** like :mod:`setforge.reconcile.merge`: it does NOT import the
store (the caller wires all I/O). A "hunk" is one contiguous base↔live diff
region — the host's local delta over the recorded merge base. Two principles:

* **Classification, not bytes.** The index persists each hunk's *class* keyed by
  a content+context identity (:func:`serialize`). The byte spans are recomputed
  fresh on every run from a 2-way diff, so a stored classification can never
  assert bytes that are no longer there — the stored list is advisory memory,
  never the reconstruction substrate (that is the verbatim ``local/`` store).
* **Reconstruct, never patch.** :func:`reconstruct` rebuilds the tracked content
  as ``base`` with only the SHARED regions promoted to their live bytes; every
  LOCAL/PENDING region keeps its base bytes. A SHARED→LOCAL *demote* therefore
  un-captures automatically (the region reverts to base), which is what makes
  INV-8 ("tracked holds exactly the shared set") hold.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass

from patiencediff import PatienceSequenceMatcher

from setforge.errors import InvariantViolation
from setforge.reconcile.merge import split_lines  # canonical engine line splitter
from setforge.reconcile.types import HunkClass

#: Base-context lines hashed on each side of a hunk to anchor its identity to
#: surrounding base content (so an edit elsewhere in the file does not re-mint it).
_CONTEXT_LINES = 3

#: Max characters in a derived hunk label before truncation.
_LABEL_MAX = 60


@dataclass(frozen=True, slots=True)
class Hunk:
    """One base↔live diff region.

    ``cls`` is the staging classification; ``label`` is the human handle shown in
    the ``stage`` walk; ``live_hash`` is the sha256 of the EOL-normalised live
    side and ``anchor`` the sha256 of the surrounding base context — together the
    stable identity. ``draft_hash`` is the sha256 of the shareable draft bytes,
    set **only** for a ``SHARED_DRAFTED`` hunk (the draft bytes themselves live in
    the ``drafts/`` store, never here — the index stays pure metadata); it is the
    draft's content identity, consulted by :func:`serialize` / ``store.verify``.
    (A blessed ``live != tracked`` divergence avoids re-flagging because
    :func:`classify` matches a drafted hunk by ``anchor`` with ``changed=False``,
    not via ``draft_hash``.) ``changed`` is a transient display flag: an
    anchor-stable hunk whose live bytes changed since it was classified (keeps its
    class but is surfaced for re-confirm). ``base_span`` / ``live_span`` are
    transient line ranges, recomputed every run and never persisted.
    """

    cls: HunkClass
    label: str
    live_hash: str
    anchor: str
    base_span: tuple[int, int]
    live_span: tuple[int, int]
    changed: bool = False
    draft_hash: str | None = None


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _norm(lines: list[bytes]) -> bytes:
    """EOL/trailing-whitespace-normalised join of ``lines`` for the identity hash.

    Each line is right-stripped (dropping ``\\r``, ``\\n``, and trailing spaces/
    tabs) and rejoined on ``\\n``, so a pure CRLF or trailing-whitespace edit does
    not re-mint a hunk's identity. The store keeps the verbatim bytes faithful;
    only the hash is normalised.
    """
    return b"\n".join(line.rstrip() for line in lines)


def _truncate(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace").strip()
    return text if len(text) <= _LABEL_MAX else text[: _LABEL_MAX - 1] + "…"


def _label(
    base_lines: list[bytes], live_lines: list[bytes], i1: int, i2: int, j1: int, j2: int
) -> str:
    """Derive a stable human label for a hunk.

    Preference order: (1) a structural marker (``#``-led heading or comment) in the
    changed content itself; (2) the nearest preceding structural marker in base;
    (3) the first non-blank changed line, truncated.
    """
    changed = live_lines[j1:j2] or base_lines[i1:i2]
    for line in changed:
        if line.strip().startswith(b"#"):
            return _truncate(line)
    for line in reversed(base_lines[:i1]):
        if line.strip().startswith(b"#"):
            return _truncate(line)
    for line in changed:
        if line.strip():
            return _truncate(line)
    return "(blank)"


def extract_hunks(base: bytes, live: bytes) -> list[Hunk]:
    """Extract the base↔live diff hunks (each unclassified → PENDING).

    A 2-way line diff (``PatienceSequenceMatcher`` over the engine's
    :func:`split_lines`); every non-``equal`` opcode becomes one :class:`Hunk`.
    An empty result means live equals base (nothing to stage).
    """
    base_lines = split_lines(base)
    live_lines = split_lines(live)
    matcher = PatienceSequenceMatcher(None, base_lines, live_lines)
    hunks: list[Hunk] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        anchor = _sha(
            _norm(base_lines[max(0, i1 - _CONTEXT_LINES) : i1])
            + b"\x00"
            + _norm(base_lines[i2 : i2 + _CONTEXT_LINES])
        )
        hunks.append(
            Hunk(
                cls=HunkClass.PENDING,
                label=_label(base_lines, live_lines, i1, i2, j1, j2),
                live_hash=_sha(_norm(live_lines[j1:j2])),
                anchor=anchor,
                base_span=(i1, i2),
                live_span=(j1, j2),
            )
        )
    return hunks


def identity(hunk: Hunk) -> tuple[str, str]:
    """The stable identity of a hunk: ``(live_hash, anchor)``."""
    return (hunk.live_hash, hunk.anchor)


def classify(fresh: list[Hunk], stored: list[dict[str, object]]) -> list[Hunk]:
    """Carry stored classifications onto freshly-extracted hunks by identity.

    A hash-stable hunk (identity matches a stored row) inherits its class. A hunk
    whose ``anchor`` matches but whose ``live_hash`` changed keeps the stored
    class but is flagged ``changed=True`` (surfaced for re-confirm, never silently
    reset). Anything unmatched stays PENDING.

    A ``SHARED_DRAFTED`` row is the exception: a hunk whose ``anchor`` is **unique**
    among the fresh set matches by **anchor alone** (``changed=False``,
    live-independent). Its tracked bytes come from the draft store, not live, so the
    live side is allowed to be anything — the host's original (keep-mine-local) OR
    the adopted draft — without re-flagging the hunk or dropping the draft, keeping
    a blessed divergence stable across live edits. The uniqueness guard matters:
    on an anchor COLLISION (two fresh hunks sharing one base context) the
    by-anchor match would splice the single draft into BOTH regions, so a colliding
    drafted anchor instead falls through to the exact/moved logic below — which
    fails safe (an exact ``live_hash`` match still carries it; otherwise it is held
    at base via ``changed=True``, never duplicated).
    """
    fresh_anchor_counts = Counter(h.anchor for h in fresh)
    drafted_by_anchor = {
        str(r["anchor"]): r
        for r in stored
        if r.get("cls") == HunkClass.SHARED_DRAFTED.value
    }
    by_identity = {(str(r["live_hash"]), str(r["anchor"])): r for r in stored}
    by_anchor = {str(r["anchor"]): r for r in stored}
    out: list[Hunk] = []
    for hunk in fresh:
        drafted = drafted_by_anchor.get(hunk.anchor)
        if drafted is not None and fresh_anchor_counts[hunk.anchor] == 1:
            out.append(
                _with(
                    hunk,
                    cls=HunkClass.SHARED_DRAFTED,
                    changed=False,
                    draft_hash=_row_draft_hash(drafted),
                )
            )
            continue
        exact = by_identity.get(identity(hunk))
        if exact is not None:
            out.append(
                _with(
                    hunk,
                    cls=HunkClass(str(exact["cls"])),
                    changed=False,
                    draft_hash=_row_draft_hash(exact),
                )
            )
            continue
        moved = by_anchor.get(hunk.anchor)
        if moved is not None:
            out.append(
                _with(
                    hunk,
                    cls=HunkClass(str(moved["cls"])),
                    changed=True,
                    draft_hash=_row_draft_hash(moved),
                )
            )
            continue
        out.append(hunk)  # unmatched → PENDING (the extract default)
    return out


def _row_draft_hash(row: dict[str, object]) -> str | None:
    """The row's ``draft_hash`` (set on a ``SHARED_DRAFTED`` row), else ``None``."""
    value = row.get("draft_hash")
    return str(value) if isinstance(value, str) else None


def _with(hunk: Hunk, *, cls: HunkClass, changed: bool, draft_hash: str | None) -> Hunk:
    return Hunk(
        cls=cls,
        label=hunk.label,
        live_hash=hunk.live_hash,
        anchor=hunk.anchor,
        base_span=hunk.base_span,
        live_span=hunk.live_span,
        changed=changed,
        draft_hash=draft_hash,
    )


def reconstruct(
    base: bytes, live: bytes, hunks: list[Hunk], drafts: dict[str, bytes]
) -> bytes:
    """Rebuild the tracked content from ``base`` with each promoted region spliced.

    ``hunks`` must be freshly extracted against this ``base``/``live`` pair (their
    spans index the current line arrays). Per region:

    * a non-``changed`` ``SHARED`` hunk takes its **live** bytes (see
      :func:`_promotes`);
    * a non-``changed`` ``SHARED_DRAFTED`` hunk takes its **draft** bytes from
      ``drafts`` keyed by the hunk's ``anchor`` — NOT live (which stays
      host-specific) and NOT base. A ``SHARED_DRAFTED`` hunk whose draft is
      missing from ``drafts`` raises :class:`~setforge.errors.InvariantViolation`
      (fail-closed: a dangling draft pointer never silently falls back to
      live/base, which would leak host bytes upstream or drop the shared draft);
    * every LOCAL / PENDING / ``changed`` region keeps its **base** bytes.

    EQUAL regions (not in ``hunks``) always pass through from base. ``drafts`` is
    a required argument with no default — so no call path can vacuously skip the
    draft splice (see :func:`assert_stage_fidelity`).
    """
    base_lines = split_lines(base)
    live_lines = split_lines(live)
    out: list[bytes] = []
    cursor = 0
    for hunk in sorted(hunks, key=lambda h: h.base_span[0]):
        i1, i2 = hunk.base_span
        j1, j2 = hunk.live_span
        out.extend(base_lines[cursor:i1])  # unchanged region before this hunk
        if hunk.cls is HunkClass.SHARED_DRAFTED and not hunk.changed:
            try:
                out.append(drafts[hunk.anchor])  # shareable draft, verbatim bytes
            except KeyError as err:
                raise InvariantViolation(
                    f"SHARED_DRAFTED hunk {hunk.anchor} has no draft in the store"
                ) from err
        elif _promotes(hunk):
            out.extend(live_lines[j1:j2])
        else:
            out.extend(base_lines[i1:i2])
        cursor = i2
    out.extend(base_lines[cursor:])
    return b"".join(out)


def _promotes(hunk: Hunk) -> bool:
    """Whether a hunk's **live** bytes are promoted into tracked on capture.

    Only an EXACT-identity ``SHARED`` hunk promotes its live bytes. A
    ``SHARED_DRAFTED`` hunk does NOT promote live — its draft is spliced in
    :func:`reconstruct` directly. A ``changed`` hunk (its anchor
    matched a stored row but its content differs — a value edit since it was
    staged, OR an anchor collision with a different region) is NOT promoted: it is
    held at base bytes until the host re-confirms it in ``setforge stage``. This is
    what stops a never-staged, anchor-colliding region from silently promoting its
    bytes upstream.
    """
    return hunk.cls is HunkClass.SHARED and not hunk.changed


def assert_stage_fidelity(
    base: bytes,
    live: bytes,
    tracked: bytes,
    hunks: list[Hunk],
    drafts: dict[str, bytes],
) -> None:
    """INV-8: tracked must equal ``base`` with only the promoted set spliced in.

    Raises :class:`~setforge.errors.InvariantViolation` when ``tracked`` carries
    any LOCAL/PENDING hunk's bytes, is missing a SHARED one, or does not carry a
    SHARED_DRAFTED hunk's draft — i.e. the git-committed tree does not hold
    exactly the shared/drafted set. ``drafts`` is required (no default) so this
    assertion can never pass vacuously by forgetting the draft splice.
    """
    expected = reconstruct(base, live, hunks, drafts)
    if tracked != expected:
        raise InvariantViolation(
            "INV-8: tracked content is not exactly the shared hunk set "
            "(reconstruct(base, live, hunks, drafts) != tracked)"
        )


def serialize(hunks: list[Hunk]) -> list[dict[str, object]]:
    """Project hunks to their persisted index rows (class + identity, no spans).

    A ``SHARED_DRAFTED`` hunk additionally carries its ``draft_hash`` — the
    identity of its shareable draft (the draft *bytes* live in the ``drafts/``
    store, never the index). Other classes omit the key so existing rows stay
    byte-stable.
    """
    rows: list[dict[str, object]] = []
    for hunk in hunks:
        row: dict[str, object] = {
            "cls": hunk.cls.value,
            "label": hunk.label,
            "live_hash": hunk.live_hash,
            "anchor": hunk.anchor,
        }
        if hunk.cls is HunkClass.SHARED_DRAFTED and hunk.draft_hash is not None:
            row["draft_hash"] = hunk.draft_hash
        rows.append(row)
    return rows
