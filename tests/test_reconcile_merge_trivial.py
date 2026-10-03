"""Trivial three-way cases resolve cleanly whatever the size or content."""

import pytest

from setforge.reconcile.merge import _MAX_BYTES, _MAX_LINES, merge
from setforge.reconcile.merge_model import Clean

_BIG_LINES = b"x\n" * (_MAX_LINES + 1)
_BIG_BYTES = b"y" * (_MAX_BYTES + 1)
_CASES = [
    pytest.param(b"a\x00b\n", id="nul"),
    pytest.param(_BIG_LINES, id="too-many-lines"),
    pytest.param(_BIG_BYTES, id="too-many-bytes"),
]


@pytest.mark.parametrize("base", _CASES)
def test_only_theirs_changed_takes_theirs(base: bytes) -> None:
    theirs = base + b"new\n"
    result = merge(base, base, theirs)
    assert result.clean
    assert result.segments == (Clean(theirs),)


@pytest.mark.parametrize("base", _CASES)
def test_only_ours_changed_keeps_ours(base: bytes) -> None:
    ours = base + b"mine\n"
    result = merge(base, ours, base)
    assert result.clean
    assert result.segments == (Clean(ours),)


@pytest.mark.parametrize("base", _CASES)
def test_both_sides_equal_is_clean(base: bytes) -> None:
    same = base + b"same\n"
    result = merge(base, same, same)
    assert result.clean
    assert result.segments == (Clean(same),)


@pytest.mark.parametrize("base", _CASES)
def test_divergent_edits_still_conflict(base: bytes) -> None:
    result = merge(base, base + b"mine\n", base + b"theirs\n")
    assert not result.clean
