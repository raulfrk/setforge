"""Property tests for the line-level 3-way merge (INV-1/2/6 + totality)."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from setforge.reconcile.merge import merge
from setforge.reconcile.merge_model import Clean, Conflict
from setforge.reconcile.types import ABSENT, Absent

# A line = small body (for frequent collisions) + a varied terminator (incl. none).
_TERMS = [b"\n", b"\r\n", b"\r", b""]
_BODIES = [b"x", b"y", b"z", b""]


@st.composite
def _text(draw: st.DrawFn) -> bytes:
    n = draw(st.integers(min_value=0, max_value=6))
    return b"".join(
        draw(st.sampled_from(_BODIES)) + draw(st.sampled_from(_TERMS)) for _ in range(n)
    )


@st.composite
def _bytes_or_nul(draw: st.DrawFn) -> bytes:
    # like _text but may inject a NUL → exercises the binary degrade path
    n = draw(st.integers(min_value=0, max_value=6))
    bodies = [*_BODIES, b"\x00q"]
    return b"".join(
        draw(st.sampled_from(bodies)) + draw(st.sampled_from(_TERMS)) for _ in range(n)
    )


def _side(strategy: st.SearchStrategy[bytes]) -> st.SearchStrategy[bytes | Absent]:
    return st.one_of(st.just(ABSENT), strategy)


_io = settings(max_examples=300)


@_io
@given(x=_text())
def test_self_merge_identity(x: bytes) -> None:
    assert merge(x, x, x).merged() == x


@_io
@given(b=_text(), t=_text())
def test_one_sided_theirs(b: bytes, t: bytes) -> None:
    # ours unchanged → theirs applies cleanly and exactly
    assert merge(b, b, t).merged() == t


@_io
@given(b=_text(), o=_text())
def test_one_sided_ours(b: bytes, o: bytes) -> None:
    # theirs unchanged → ours is kept exactly
    assert merge(b, o, b).merged() == o


@_io
@given(b=_side(_bytes_or_nul()), o=_side(_bytes_or_nul()), t=_side(_bytes_or_nul()))
def test_total_never_raises_and_edits_preserved(b, o, t) -> None:
    # merge() runs the fail-closed INV-1 verify internally; returning without
    # raising IS the edit-preservation guarantee over arbitrary bytes/ABSENT.
    r = merge(b, o, t)
    # sanity: a clean result's merged() is well-defined; an unclean one has a Conflict.
    if r.clean:
        assert r.merged() is ABSENT or isinstance(r.merged(), bytes)
    else:
        assert any(isinstance(s, Conflict) for s in r.segments)


@_io
@given(b=_text(), o=_text(), t=_text())
def test_clean_segments_are_bytes(b: bytes, o: bytes, t: bytes) -> None:
    # every Clean segment is real bytes (no None / marker leakage)
    for seg in merge(b, o, t).segments:
        if isinstance(seg, Clean):
            assert isinstance(seg.bytes_, bytes)


def test_patiencediff_independent_edits_merge_clean() -> None:
    # two edits on different lines, separated by a unique anchor → clean merge
    # (unique-line anchoring keeps them apart; a naive matcher can false-conflict).
    base = b"head\nold1\nMIDDLE\nold2\ntail\n"
    ours = b"head\nNEW1\nMIDDLE\nold2\ntail\n"
    theirs = b"head\nold1\nMIDDLE\nNEW2\ntail\n"
    r = merge(base, ours, theirs)
    assert r.clean is True
    assert r.merged() == b"head\nNEW1\nMIDDLE\nNEW2\ntail\n"
