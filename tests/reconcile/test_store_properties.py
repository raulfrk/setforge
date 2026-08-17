"""Property tests for the store's byte-exact round-trip (INV-2) + index codec."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from setforge import locking
from setforge.reconcile import store
from setforge.reconcile.index_model import FileEntry, Index, dumps, loads
from setforge.reconcile.types import ABSENT, file_id

pytestmark = pytest.mark.slow

# These properties do real fsync-backed atomic writes per example, so per-example
# latency is I/O-bound and variable — disable Hypothesis's wall-clock deadline
# (correctness, not speed, is what they assert).
_io_bound = settings(deadline=None)


@contextmanager
def _isolated_state() -> Iterator[None]:
    """Point the state dir at a FRESH temp tree for one Hypothesis example.

    A per-example dir (not a shared function-scoped fixture) avoids cross-example
    path-prefix collisions — e.g. file-id ``a`` (a file) vs ``a/b`` (needs ``a``
    to be a dir), an inherent constraint of the verbatim path-mirror store.
    """
    prev = os.environ.get("SETFORGE_STATE_DIR")
    with tempfile.TemporaryDirectory() as d:
        os.environ["SETFORGE_STATE_DIR"] = d
        try:
            yield
        finally:
            if prev is None:
                os.environ.pop("SETFORGE_STATE_DIR", None)
            else:
                os.environ["SETFORGE_STATE_DIR"] = prev


# A safe file-id strategy: non-empty path parts of safe chars, no '.' / '..'.
_part = st.text(
    alphabet=st.characters(
        min_codepoint=33, max_codepoint=122, blacklist_characters="/\\"
    ),
    min_size=1,
    max_size=8,
).filter(lambda s: s not in (".", ".."))
_fid = st.lists(_part, min_size=1, max_size=3).map("/".join)


@_io_bound
@given(fid=_fid, data=st.binary(max_size=256))
def test_local_round_trip_byte_exact(fid: str, data: bytes) -> None:
    with _isolated_state():
        f = file_id(fid)
        store.write_local("prof", f, data)
        assert store.read_local("prof", f) == data
        assert store.reconstruct("prof", f) == data


@_io_bound
@given(fid=_fid)
def test_absence_round_trip(fid: str) -> None:
    with _isolated_state():
        f = file_id(fid)
        store.write_local("prof", f, ABSENT)
        assert store.read_local("prof", f) is ABSENT


@_io_bound
@given(fid=_fid, data=st.binary(max_size=256))
def test_record_then_verify(fid: str, data: bytes) -> None:
    with _isolated_state():
        f = file_id(fid)
        with locking.profile_lock("prof"):
            store.record("prof", f, base=b"base", local=data)
        store.verify("prof", f)  # must not raise


@_io_bound
@given(
    files=st.dictionaries(
        _fid,
        st.tuples(st.booleans(), st.one_of(st.none(), st.just("sha256:00"))),
        max_size=5,
    )
)
def test_index_codec_round_trip(files: dict[str, tuple[bool, str | None]]) -> None:
    idx = Index(
        files={
            k: FileEntry(present=p, local_hash=h, hunks=[])
            for k, (p, h) in files.items()
        }
    )
    assert loads(dumps(idx)) == idx
    assert dumps(idx) == dumps(idx)  # byte-stable
