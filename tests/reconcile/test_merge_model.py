"""MergeResult model: Clean / Conflict / MergeResult."""

from __future__ import annotations

import pytest

from setforge.reconcile.merge_model import Clean, Conflict, MergeResult
from setforge.reconcile.types import ABSENT


def test_clean_round_trip() -> None:
    r = MergeResult((Clean(b"x"), Clean(b"y")))
    assert r.clean is True
    assert r.merged() == b"xy"


def test_conflict_makes_unclean() -> None:
    r = MergeResult((Clean(b"a"), Conflict(b"b", b"o", b"t")))
    assert r.clean is False
    with pytest.raises(ValueError, match="unresolved conflicts"):
        r.merged()


def test_absent_result() -> None:
    r = MergeResult((), absent=True)
    assert r.clean is True
    assert r.merged() is ABSENT


def test_empty_clean_result_is_empty_bytes() -> None:
    r = MergeResult(())
    assert r.clean is True
    assert r.merged() == b""
