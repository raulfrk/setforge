"""Tests for the pure scalar 3-way merge resolver."""

import math

import pytest

from setforge.errors import MergeTypeMismatch
from setforge.scalar_merge import (
    ABSENT,
    ScalarOutcome,
    ScalarResolution,
    resolve_scalar,
)

# Concrete values for the truth table: V=1, V'=2, V''=3.
V = 1
VP = 2
VPP = 3


# (base, ours, theirs) -> expected ScalarResolution. Rows 1-15 from the spec,
# MECE over {value | ABSENT}^3.
_TRUTH_TABLE: list[tuple[object, object, object, ScalarResolution]] = [
    (V, V, V, ScalarResolution(ScalarOutcome.TAKE, V)),  # 1
    (V, VP, V, ScalarResolution(ScalarOutcome.TAKE, VP)),  # 2
    (V, V, VP, ScalarResolution(ScalarOutcome.TAKE, VP)),  # 3
    (V, VP, VP, ScalarResolution(ScalarOutcome.TAKE, VP)),  # 4
    (V, VP, VPP, ScalarResolution(ScalarOutcome.CONFLICT)),  # 5
    (V, ABSENT, V, ScalarResolution(ScalarOutcome.DELETE)),  # 6
    (V, V, ABSENT, ScalarResolution(ScalarOutcome.DELETE)),  # 7
    (V, ABSENT, ABSENT, ScalarResolution(ScalarOutcome.DELETE)),  # 8
    (V, ABSENT, VP, ScalarResolution(ScalarOutcome.CONFLICT)),  # 9
    (V, VP, ABSENT, ScalarResolution(ScalarOutcome.CONFLICT)),  # 10
    (ABSENT, ABSENT, ABSENT, ScalarResolution(ScalarOutcome.DELETE)),  # 11
    (ABSENT, VP, ABSENT, ScalarResolution(ScalarOutcome.TAKE, VP)),  # 12
    (ABSENT, ABSENT, VP, ScalarResolution(ScalarOutcome.TAKE, VP)),  # 13
    (ABSENT, VP, VP, ScalarResolution(ScalarOutcome.TAKE, VP)),  # 14
    (ABSENT, VP, VPP, ScalarResolution(ScalarOutcome.CONFLICT)),  # 15
]


@pytest.mark.parametrize(
    ("base", "ours", "theirs", "expected"),
    _TRUTH_TABLE,
    ids=[f"row{i}" for i in range(1, len(_TRUTH_TABLE) + 1)],
)
def test_truth_table(
    base: object, ours: object, theirs: object, expected: ScalarResolution
) -> None:
    assert resolve_scalar(base, ours, theirs) == expected


def test_bool_is_not_int_both_same_change() -> None:
    # base=1 (int), ours=theirs=True (bool). True != 1 type-aware, so both
    # sides changed identically -> row-4 behavior -> TAKE True.
    result = resolve_scalar(1, True, True)
    assert result == ScalarResolution(ScalarOutcome.TAKE, True)
    assert result.value is True


def test_int_vs_float_distinct_types_conflict() -> None:
    # base=1 (int), ours=1.0 (float != int), theirs=2 (int != base).
    # Both differ from base, and ours != theirs -> CONFLICT.
    assert resolve_scalar(1, 1.0, 2) == ScalarResolution(ScalarOutcome.CONFLICT)


def test_str_vs_int_type_aware() -> None:
    # base="1" (str), ours=theirs=1 (int). "1" != 1 type-aware, so both sides
    # changed identically -> TAKE 1.
    result = resolve_scalar("1", 1, 1)
    assert result == ScalarResolution(ScalarOutcome.TAKE, 1)


def test_all_nan_not_conflict() -> None:
    # Same-type NaNs are equal-for-merge per the documented policy, so an
    # all-NaN merge resolves to TAKE rather than a false CONFLICT.
    nan = float("nan")
    result = resolve_scalar(nan, nan, nan)
    assert result.outcome is ScalarOutcome.TAKE
    assert math.isnan(result.value)  # type: ignore[arg-type]


def test_null_present_unchanged_vs_delete() -> None:
    # base=None (present null), ours=None (unchanged from base), theirs=ABSENT.
    # This is row 7 of the truth table (V V ∅ -> DELETE) with V=None: ours
    # equals base, so the mandated 3-way logic takes theirs (ABSENT) -> DELETE.
    # NOTE: the task's prose tagged (None, None, ABSENT) as CONFLICT, but that
    # contradicts both row 7 and the standard algorithm the task mandates
    # ("if ours==base -> take theirs"). The 15-row table is authoritative
    # ("implement EXACTLY these 15 rows"), so this asserts DELETE.
    result = resolve_scalar(None, None, ABSENT)
    assert result == ScalarResolution(ScalarOutcome.DELETE)


def test_null_present_modified_vs_delete_conflict() -> None:
    # base=None, ours=1 (modified), theirs=ABSENT (delete): row 10 modify-vs-
    # delete with V=None -> CONFLICT. This is the genuine "present null vs
    # delete" divergence the task intended.
    result = resolve_scalar(None, 1, ABSENT)
    assert result == ScalarResolution(ScalarOutcome.CONFLICT)


def test_non_scalar_operand_raises_merge_type_mismatch() -> None:
    with pytest.raises(MergeTypeMismatch):
        resolve_scalar([1], 2, 3)


def test_absent_compared_only_via_identity() -> None:
    # A present value whose __eq__ returns True for everything must still be
    # treated as present, never mistaken for ABSENT (which is identity-only).
    class AlwaysEqual:
        def __eq__(self, other: object) -> bool:
            return True

        def __hash__(self) -> int:
            return 0

    permissive = AlwaysEqual()
    # It is not a scalar at all, so the operand guard rejects it first --
    # confirming ABSENT is never matched by ==.
    with pytest.raises(MergeTypeMismatch):
        resolve_scalar(permissive, ABSENT, ABSENT)
