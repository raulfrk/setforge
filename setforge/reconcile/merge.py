"""Pure line-level 3-way merge over base / ours / theirs (RFC §9.1).

``merge(base, ours, theirs) -> MergeResult`` is git-style: a region only one side
changed applies silently + byte-for-byte (INV-6); a region both sides changed
becomes a :class:`~setforge.reconcile.merge_model.Conflict` for the wizard. No
reflow / unwrap / word-merge — that is claude-merge's job. The engine is:

* **bytes-native** — content is split on ``b"\n"`` keeping terminators and never
  decoded, so CRLF / lone-CR / no-final-newline / binary all round-trip byte-exact
  (INV-2 carryover);
* **total on content** — binary (NUL), oversize, or a pathological (recursion-blown)
  input degrades to a single whole-file conflict, never an exception;
* **fail-closed** — before returning, a verify pass asserts no user edit was
  silently lost (INV-1), raising :class:`~setforge.errors.MergeInvariantError` on
  a defect rather than shipping a lossy merge.

It is a leaf module: it does NOT import the store (the caller wires I/O) and must
not import any legacy merge module (the reconcile legacy-import ban).
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, cast

import merge3
from patiencediff import PatienceSequenceMatcher

try:
    # The pure-Python matcher raises this (an ``Exception`` subclass, NOT a
    # ``RecursionError``) on a pathological unique-anchor structure; the C/Rust
    # backend doesn't, so it lives at a private path with no public alias.
    from patiencediff._patiencediff_py import MaxRecursionDepth as _MaxRecursionDepth
except ImportError:  # pragma: no cover - layout differs across patiencediff builds
    _MaxRecursionDepth = RecursionError  # type: ignore[assignment,misc]

from setforge.errors import MergeError, MergeInvariantError
from setforge.reconcile.merge_model import (
    Clean,
    Conflict,
    MergeInput,
    MergeResult,
    Segment,
)
from setforge.reconcile.types import ABSENT

# Degrade ceilings: above these a line merge is worthless (and risks O(n²) /
# memory blowup), so we emit a whole-file conflict instead. Config files are KB;
# these are orders of magnitude above any real tracked file. Constants, not knobs
# (pathology protection, not a user preference).
_MAX_BYTES = 5 * 1024 * 1024
_MAX_LINES = 100_000

_LINE_RE = re.compile(rb"[^\n]*\n|[^\n]+")


def _split_lines(data: bytes) -> list[bytes]:
    """Split ``data`` on ``b"\\n"`` KEEPING terminators; ``b""`` → ``[]``.

    The exact inverse of ``b"".join``. Unlike :meth:`bytes.splitlines` it does NOT
    split on a lone ``\\r`` (which would over-split and change merge alignment),
    and unlike ``data.split(b"\\n")`` it does not drop terminators.
    """
    return [] if not data else _LINE_RE.findall(data)


def split_lines(data: bytes) -> list[bytes]:
    """Public alias for the engine line splitter (terminator-keeping).

    Shared by the 3-way merge here and the A5 hunk layer (:mod:`hunks`) so both
    align on the exact same line model; exported rather than reached into as a
    private name.
    """
    return _split_lines(data)


def _whole_file_conflict(
    base: MergeInput, ours: MergeInput, theirs: MergeInput
) -> MergeResult:
    """One :class:`Conflict` over the whole file; an ABSENT side renders as ``b""``."""
    return MergeResult(
        (
            Conflict(
                base=b"" if base is ABSENT else base,
                ours=b"" if ours is ABSENT else ours,
                theirs=b"" if theirs is ABSENT else theirs,
            ),
        ),
        ours_absent=ours is ABSENT,
        theirs_absent=theirs is ABSENT,
    )


def _resolve_absence(
    base: MergeInput, ours: MergeInput, theirs: MergeInput
) -> MergeResult | None:
    """Resolve the file-level 3-way when any side is ABSENT.

    Returns the outcome, or ``None`` to fall through to the body merge (only when
    all three sides are present bytes). A clean deletion yields
    ``MergeResult((), absent=True)`` — never an empty ``Clean`` (ABSENT ≠ ``b""``).
    Policy (rev 3): an upstream deletion is a conflict ONLY when the local side has
    edits to lose (``ours != base``); otherwise it applies silently.
    """
    if (
        isinstance(base, bytes)
        and isinstance(ours, bytes)
        and isinstance(theirs, bytes)
    ):
        return None  # all present → body merge

    absent = MergeResult((), absent=True)
    o = ours if isinstance(ours, bytes) else None
    t = theirs if isinstance(theirs, bytes) else None

    if base is ABSENT:
        if o is not None and t is not None:
            # add/add: identical → clean, else conflict
            return (
                MergeResult((Clean(o),))
                if o == t
                else _whole_file_conflict(base, ours, theirs)
            )
        if o is not None:
            return MergeResult((Clean(o),))  # local-add only
        if t is not None:
            return MergeResult((Clean(t),))  # upstream-add only
        return absent  # nothing anywhere

    # base is present bytes; at least one of ours/theirs is ABSENT.
    if o is None and t is None:
        return absent  # both-delete
    if o is None:  # ours deleted, theirs present
        return absent if t == base else _whole_file_conflict(base, ours, theirs)
    # theirs deleted, ours present
    return absent if o == base else _whole_file_conflict(base, ours, theirs)


def _groups_to_segments(groups: list[tuple[Any, ...]]) -> list[Segment]:
    """Map merge3 ``merge_groups()`` tuples to the public segment stream.

    Clean tags (``unchanged``/``a``/``b``/``same``) coalesce into one :class:`Clean`
    run; ``conflict`` flushes the run and emits a :class:`Conflict`. Any other tag
    is a defect (a future merge3 could add one) — raise, never silently drop it.
    """
    segments: list[Segment] = []
    clean_run: list[bytes] = []

    def _flush() -> None:
        if clean_run:
            segments.append(Clean(b"".join(clean_run)))
            clean_run.clear()

    for group in groups:
        tag = group[0]
        if tag in ("unchanged", "a", "b", "same"):
            clean_run.extend(group[1])
        elif tag == "conflict":
            _flush()
            segments.append(
                Conflict(b"".join(group[1]), b"".join(group[2]), b"".join(group[3]))
            )
        else:
            raise MergeError(f"unexpected merge3 group tag {tag!r}")
    _flush()
    return segments


def _body_merge(base: bytes, ours: bytes, theirs: bytes) -> MergeResult:
    """Line-level 3-way of three present bodies; degrades on binary/oversize."""
    for side in (base, ours, theirs):
        if b"\x00" in side or len(side) > _MAX_BYTES:
            return _whole_file_conflict(base, ours, theirs)

    b_lines, o_lines, t_lines = (
        _split_lines(base),
        _split_lines(ours),
        _split_lines(theirs),
    )
    if max(len(b_lines), len(o_lines), len(t_lines)) > _MAX_LINES:
        return _whole_file_conflict(base, ours, theirs)

    try:
        groups: list[tuple[Any, ...]] = list(
            merge3.Merge3(
                b_lines,
                o_lines,
                t_lines,
                sequence_matcher=PatienceSequenceMatcher,  # type: ignore[arg-type]
            ).merge_groups()
        )
    except (RecursionError, _MaxRecursionDepth):
        # A pathological unique-anchor structure blows the matcher's recursion
        # (RecursionError from the C backend, MaxRecursionDepth from pure-Python);
        # degrade to a whole-file conflict rather than crash (totality).
        return _whole_file_conflict(base, ours, theirs)
    except Exception as err:
        raise MergeError(f"merge3 failed: {err}") from err

    return MergeResult(tuple(_groups_to_segments(groups)))


def _verify(
    base: MergeInput, ours: MergeInput, theirs: MergeInput, result: MergeResult
) -> None:
    """Fail-closed INV-1 guard: no user *edit* silently dropped.

    Every line a side added/changed relative to ``base`` (its line-multiset minus
    base's) must appear somewhere in the result — a clean run or any conflict
    side. This is a one-directional drop check (a missing edit line raises); it is
    deliberately NOT a positional 3-way reconstruction (impossible from the lossy
    public segment stream — a one-sided clean region discards the other sides'
    pre-image) and does NOT detect duplication or reordering. Positional / INV-6
    correctness is pinned by the identity-law + two-sided property tests; this
    O(n) runtime check is the cheap loss-guard that backs the engine's
    no-silent-data-loss contract.
    """
    base_c = Counter(_split_lines(base)) if isinstance(base, bytes) else Counter()
    present: Counter[bytes] = Counter()
    for seg in result.segments:
        if isinstance(seg, Clean):
            present.update(_split_lines(seg.bytes_))
        else:
            present.update(_split_lines(seg.base))
            present.update(_split_lines(seg.ours))
            present.update(_split_lines(seg.theirs))

    for label, side in (("ours", ours), ("theirs", theirs)):
        if not isinstance(side, bytes):
            continue
        edits = Counter(_split_lines(side)) - base_c  # lines this side has beyond base
        if edits - present:  # any edit line not covered by the result
            raise MergeInvariantError(
                f"merge dropped a {label} edit (INV-1): lost {dict(edits - present)!r}"
            )


def merge(base: MergeInput, ours: MergeInput, theirs: MergeInput) -> MergeResult:
    """Three-way merge of ``base`` / ``ours`` / ``theirs`` (each bytes or ABSENT).

    Returns an ordered :class:`MergeResult`; ``.clean`` is true when there are no
    conflicts (then ``.merged()`` is the byte-exact result, or :data:`ABSENT` for a
    clean deletion). Total on content — never raises on input shape; only a
    fail-closed :class:`~setforge.errors.MergeInvariantError` / wrapped
    :class:`~setforge.errors.MergeError` can surface, both indicating a defect.
    """
    resolved = _resolve_absence(base, ours, theirs)
    if resolved is not None:
        result = resolved
    else:
        # _resolve_absence returns None only when all three sides are present bytes.
        result = _body_merge(
            cast("bytes", base), cast("bytes", ours), cast("bytes", theirs)
        )
    _verify(base, ours, theirs, result)
    return result
