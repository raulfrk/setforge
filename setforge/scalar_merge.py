"""Pure, format-agnostic scalar 3-way merge resolver.

This module is intentionally I/O-free and format-agnostic: it operates on
already-parsed Python scalars (str, int, float, bool, None) plus the
:data:`ABSENT` field-absence sentinel. Callers in the YAML/JSON merge paths
map their parsed-value-or-missing states onto these operands and translate the
returned :class:`ScalarResolution` back into edits.

The resolver implements standard 3-way logic generalized to treat field
absence as a first-class operand:

* if ``ours == base`` -> take ``theirs``
* if ``theirs == base`` -> take ``ours``
* if ``ours == theirs`` -> take it
* otherwise -> CONFLICT

A "take ABSENT" result maps to :attr:`ScalarOutcome.DELETE`.
"""

from dataclasses import dataclass
from enum import Enum, StrEnum

from setforge.errors import MergeTypeMismatch


class _Absent(Enum):
    """One-member enum providing the :data:`ABSENT` field-absence sentinel.

    PEP 661 style: a single-member enum gives the sentinel a stable identity,
    a clean ``repr`` (``<ABSENT>``), and pickle-by-name semantics. Compare the
    sentinel ONLY via ``is`` / ``is not`` — never ``==`` — because a present
    value may carry a permissive ``__eq__`` that would spuriously equal it.
    """

    ABSENT = "ABSENT"

    def __repr__(self) -> str:
        return "<ABSENT>"


ABSENT = _Absent.ABSENT
"""Module-level sentinel for field-absence, DISTINCT from ``None``/``null``."""


class ScalarOutcome(StrEnum):
    """The three resolution outcomes of a scalar 3-way merge."""

    TAKE = "take"
    DELETE = "delete"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class ScalarResolution:
    """Result of :func:`resolve_scalar`.

    ``value`` is meaningful only when ``outcome`` is :attr:`ScalarOutcome.TAKE`;
    it is ``None`` (and ignored) for DELETE and CONFLICT.
    """

    outcome: ScalarOutcome
    value: object = None


@dataclass(frozen=True, slots=True)
class ScalarConflict:
    """A single ``preserve_user_keys`` scalar path whose three sides diverge.

    Mirrors :class:`setforge.structural_merge.PathConflict` for the SCALAR
    overlay: ``base`` / ``ours`` (live) / ``theirs`` (tracked) are plain-python
    scalars (or the :data:`ABSENT` sentinel for a side where the key is
    missing), so the record is comparable and printable. The interactive
    conflict wizard (:mod:`setforge.conflict_wizard`) renders these three sides
    and hands one to the injected resolver per conflicting path when
    ``--auto`` is not set.
    """

    path: str
    base: object
    ours: object
    theirs: object


# Scalar operand allowlist. ``bool`` is included despite subclassing ``int``;
# the type-aware equality helper keeps ``True`` distinct from ``1``.
_SCALAR_TYPES: tuple[type, ...] = (str, int, float, bool, type(None))


def _is_scalar(operand: object) -> bool:
    """Return whether ``operand`` is an allowlisted scalar or the sentinel."""
    if operand is ABSENT:
        return True
    # Exact-type membership (not isinstance) keeps the guard tight: an
    # arbitrary subclass of a scalar type is not silently admitted.
    return type(operand) in _SCALAR_TYPES


def _scalar_eq(a: object, b: object) -> bool:
    """Return whether ``a`` and ``b`` are equal-for-merge, type-aware.

    Compares ``type(a) is type(b)`` FIRST, then value. Using ``isinstance``
    would be wrong because ``bool`` subclasses ``int`` (``True == 1``). This
    yields: ``True != 1``, ``False != 0``, ``1 != 1.0``, ``"1" != 1``.

    NaN policy: two SAME-TYPE NaNs are treated as equal-for-merge (a raw
    ``nan == nan`` is ``False``, which would make an all-NaN merge a false
    CONFLICT). We special-case ``a != a and b != b`` -> equal so all-NaN
    resolves cleanly to TAKE rather than CONFLICT.

    The sentinel :data:`ABSENT` participates here too: same identity -> equal,
    otherwise unequal against any present value.
    """
    if a is ABSENT or b is ABSENT:
        return a is b
    if type(a) is not type(b):
        return False
    # Same-type NaN: equal-for-merge per the documented policy above.
    if a != a and b != b:
        return True
    return a == b


def resolve_scalar(base: object, ours: object, theirs: object) -> ScalarResolution:
    """Resolve a scalar 3-way merge of ``base`` / ``ours`` / ``theirs``.

    Each operand is either an allowlisted scalar (str, int, float, bool, None)
    or the :data:`ABSENT` field-absence sentinel. Non-scalar operands
    (list/dict/set/unhashable/arbitrary objects) raise
    :class:`~setforge.errors.MergeTypeMismatch` before any comparison.

    Returns a :class:`ScalarResolution` whose ``outcome`` is TAKE (with the
    chosen ``value``), DELETE (the field should be absent in the result), or
    CONFLICT (the two sides diverge irreconcilably).
    """
    for label, operand in (("base", base), ("ours", ours), ("theirs", theirs)):
        if not _is_scalar(operand):
            raise MergeTypeMismatch(
                f"non-scalar operand for {label}: {type(operand).__name__}"
            )

    def _take_or_delete(value: object) -> ScalarResolution:
        if value is ABSENT:
            return ScalarResolution(ScalarOutcome.DELETE)
        return ScalarResolution(ScalarOutcome.TAKE, value)

    ours_unchanged = _scalar_eq(ours, base)
    theirs_unchanged = _scalar_eq(theirs, base)

    if ours_unchanged:
        # Only theirs (may have) changed -> take theirs.
        return _take_or_delete(theirs)
    if theirs_unchanged:
        # Only ours changed -> take ours.
        return _take_or_delete(ours)
    # Both sides changed away from base.
    if _scalar_eq(ours, theirs):
        # Same change on both sides -> take it.
        return _take_or_delete(ours)
    return ScalarResolution(ScalarOutcome.CONFLICT)
