"""Result types for the line-level 3-way merge.

A :class:`MergeResult` is an ordered stream of :class:`Clean` and
:class:`Conflict` segments in document order — a lossless partition of the three
inputs (every input byte is in a clean run or a conflict side), which is what
makes INV-1 (no silent data loss) checkable. A fully-clean merge has only
``Clean`` segments and its merged bytes are the join of them; a clean deletion is
the empty stream with ``absent=True``.
"""

from __future__ import annotations

from dataclasses import dataclass

from setforge.reconcile.types import ABSENT, Absent

MergeInput = bytes | Absent
"""One side of a merge: present bytes (incl. ``b""``) or the :data:`ABSENT` sentinel."""


@dataclass(frozen=True, slots=True)
class Clean:
    """A non-conflicting run, byte-identical to its winning source (INV-6)."""

    bytes_: bytes


@dataclass(frozen=True, slots=True)
class Conflict:
    """A conflicting region carrying all three sides (joined bytes; any may be ``b""``).

    The wizard (A3) splits these for display; claude-merge (A4) needs ``base`` to
    show what upstream intended. An absent side is rendered as ``b""``.
    """

    base: bytes
    ours: bytes
    theirs: bytes


Segment = Clean | Conflict


@dataclass(frozen=True, slots=True)
class MergeResult:
    """An ordered segment stream plus an explicit clean-deletion flag.

    ``absent`` marks a clean merge whose result is "the file is absent" (a
    deletion that applied without conflict) — distinct from an empty file
    (``segments=()``, ``absent=False`` → ``merged() == b""``).
    """

    segments: tuple[Segment, ...]
    absent: bool = False

    @property
    def clean(self) -> bool:
        """True when no segment is a :class:`Conflict`."""
        return all(isinstance(s, Clean) for s in self.segments)

    def merged(self) -> bytes | Absent:
        """The merged bytes (or :data:`ABSENT`). Defined only for a clean result.

        Raises :class:`ValueError` if the result has unresolved conflicts — the
        caller must resolve them (via the wizard) before a merged file exists.
        """
        if not self.clean:
            raise ValueError("merged() on a result with unresolved conflicts")
        if self.absent:
            return ABSENT
        return b"".join(s.bytes_ for s in self.segments if isinstance(s, Clean))
