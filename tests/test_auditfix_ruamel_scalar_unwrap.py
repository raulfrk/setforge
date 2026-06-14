"""Regression tests for ruamel round-trip scalar unwrapping in the 3-way merge.

ruamel round-trip mode does NOT keep builtin types for formatted scalars:
``1.5``/``1e3`` -> ``ScalarFloat``, ``0xFF`` -> ``HexCapsInt``,
``1_000`` -> ``ScalarInt``, ``2020-01-01`` -> ``datetime.date``,
``!!str 5`` -> ``TaggedScalar``. Before the fix, :func:`_to_plain` passed these
subclass leaves through unchanged, so the scalar resolver's EXACT-type guard
rejected them and ``merge_structural`` crashed with ``MergeTypeMismatch`` on
ANY YAML model containing such a scalar. These tests PARSE real ruamel text
(never plain dicts) so the formatted-scalar leaves are exercised.
"""

import io
from typing import Any

import pytest
from ruamel.yaml import YAML

from setforge.structural_merge import (
    _to_plain,
    merge_structural,
)


def _yload(text: str) -> object:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    return y.load(io.StringIO(text))


# Each entry: a YAML scalar literal whose round-trip type is NOT a builtin.
_FORMATTED_SCALARS = [
    "1.5",  # ScalarFloat
    "1e3",  # ScalarFloat (scientific)
    "0xFF",  # HexCapsInt
    "1_000",  # ScalarInt (underscore-formatted)
    "2020-01-01",  # datetime.date
    "!!str 5",  # TaggedScalar
]


@pytest.mark.parametrize("scalar", _FORMATTED_SCALARS)
def test_merge_clean_with_formatted_scalar_present(scalar: str) -> None:
    """A formatted scalar untouched on all sides no longer crashes the merge;
    the unrelated upstream edit lands cleanly (old: MergeTypeMismatch)."""
    base = _yload(f"a: {scalar}\nname: old")
    ours = _yload(f"a: {scalar}\nname: old")
    theirs = _yload(f"a: {scalar}\nname: NEW")

    result = merge_structural(base, ours, theirs)

    assert result.clean
    assert result.conflicts == []
    merged: Any = result.merged_model
    assert merged["name"] == "NEW"  # upstream edit landed


def test_float_take_theirs_value() -> None:
    """ours==base, theirs edits the float -> take theirs' float value."""
    base = _yload("a: 1.5")
    ours = _yload("a: 1.5")
    theirs = _yload("a: 2.5")

    result = merge_structural(base, ours, theirs)

    assert result.clean
    merged: Any = result.merged_model
    assert float(merged["a"]) == 2.5


def test_float_both_edit_differently_conflicts() -> None:
    """Both sides edit the float differently -> a PathConflict with plain
    floats (not ruamel wrappers) on every side."""
    base = _yload("a: 1.5")
    ours = _yload("a: 2.5")
    theirs = _yload("a: 3.5")

    result = merge_structural(base, ours, theirs)

    assert not result.clean
    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict.path == "a"
    assert (conflict.base, conflict.ours, conflict.theirs) == (1.5, 2.5, 3.5)
    assert all(
        type(v) is float for v in (conflict.base, conflict.ours, conflict.theirs)
    )


def test_int_float_distinction_preserved_after_unwrap() -> None:
    """Unwrapping a ScalarFloat must keep ``1 != 1.0``: base int 1, ours
    float 1.0, theirs int 1 -> ours changed, take ours' float."""
    base = _yload("a: 1")
    ours = _yload("a: 1.0")
    theirs = _yload("a: 1")

    result = merge_structural(base, ours, theirs)

    assert result.clean
    merged: Any = result.merged_model
    assert float(merged["a"]) == 1.0


def test_date_edit_takes_theirs() -> None:
    """A date untouched on ours but changed on theirs takes theirs' date,
    and a sibling edit on a different key lands too."""
    base = _yload("d: 2020-01-01\nname: old")
    ours = _yload("d: 2020-01-01\nname: old")
    theirs = _yload("d: 2021-06-15\nname: NEW")

    result = merge_structural(base, ours, theirs)

    assert result.clean
    merged: Any = result.merged_model
    assert merged["name"] == "NEW"
    assert str(merged["d"]) == "2021-06-15"


def test_to_plain_unwraps_formatted_scalars_to_builtins() -> None:
    """Unit-level: every formatted ruamel scalar collapses to a builtin (or a
    comparable str for date/tagged) so the divergence test sees plain types."""
    model = _yload(
        "f: 1.5\ns: 1e3\nh: 0xFF\nu: 1_000\nd: 2020-01-01\nt: !!str 5\ni: 1\nb: true"
    )
    plain: Any = _to_plain(model)

    assert type(plain["f"]) is float
    assert plain["f"] == 1.5
    assert type(plain["s"]) is float
    assert plain["s"] == 1000.0
    assert type(plain["h"]) is int
    assert plain["h"] == 255
    assert type(plain["u"]) is int
    assert plain["u"] == 1000
    assert type(plain["d"]) is str
    assert plain["d"] == "2020-01-01"
    assert type(plain["t"]) is str
    assert plain["t"] == "5"
    # Plain builtins pass through with their type intact (the 1 != 1.0 / True != 1
    # distinctions the resolver relies on).
    assert type(plain["i"]) is int
    assert plain["i"] == 1
    assert type(plain["b"]) is bool
    assert plain["b"] is True
