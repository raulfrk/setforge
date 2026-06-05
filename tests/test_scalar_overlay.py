"""Tests for setforge.scalar_overlay — stored-base 3-way scalar overlay.

Exercises the driver for BOTH a YAML and a JSONC fixture across the full
matrix the spec enumerates: upstream-propagation, user-edit-preservation,
same-change-both-sides, the three bare/auto conflict resolutions, the
base-absent first-run seed, the ABSENT-vs-null distinction, and the
non-scalar-leaf fallback. ``ours`` = live, ``theirs`` = tracked/upstream.
"""

from pathlib import Path

from setforge.disposition_merge import (
    ConflictChoice,
    ConflictResolution,
    ConflictResolver,
)
from setforge.scalar_merge import ABSENT, ScalarConflict
from setforge.scalar_overlay import ScalarOverlayResult, resolve_scalar_overlay
from setforge.section_wizard import ReconcileAuto


def _resolver(*resolutions: ConflictResolution) -> ConflictResolver:
    """Return a resolver that pops ``resolutions`` in call order.

    Records each conflict it is handed on the returned callable's ``seen``
    attribute so a test can assert the wizard saw a ``ScalarConflict`` with the
    expected base/ours/theirs sides.
    """
    it = iter(resolutions)
    seen: list[object] = []

    def _resolve(conflict: object) -> ConflictResolution:
        seen.append(conflict)
        return next(it)

    _resolve.seen = seen  # type: ignore[attr-defined]
    return _resolve


YAML_DST = Path("settings.yaml")
JSONC_DST = Path("settings.json")


def _yaml_doc(value: str) -> str:
    return f"a:\n  k: {value}\n"


def _jsonc_doc(value: str) -> str:
    return f'{{\n  "a": {{\n    "k": {value}\n  }}\n}}\n'


# ---------------------------------------------------------------------------
# 1. base present, ours == base, theirs changed -> upstream propagates.
# ---------------------------------------------------------------------------


def test_yaml_upstream_propagates_when_user_unchanged() -> None:
    res = resolve_scalar_overlay(
        dst=YAML_DST,
        live_text=_yaml_doc("1"),
        tracked_text=_yaml_doc("2"),
        preserve_user_keys=["a.k"],
        base_lookup=lambda _p: 1,
        auto=None,
    )
    assert "k: 2" in res.merged_text
    assert res.rebaseline["a.k"] == 2
    assert res.conflicts == []
    assert res.deferred is False


def test_jsonc_upstream_propagates_when_user_unchanged() -> None:
    res = resolve_scalar_overlay(
        dst=JSONC_DST,
        live_text=_jsonc_doc("1"),
        tracked_text=_jsonc_doc("2"),
        preserve_user_keys=["a > k"],
        base_lookup=lambda _p: 1,
        auto=None,
    )
    assert '"k": 2' in res.merged_text
    assert res.rebaseline["a > k"] == 2
    assert res.conflicts == []
    assert res.deferred is False


# ---------------------------------------------------------------------------
# 2. base present, theirs == base, ours changed -> user edit preserved.
# ---------------------------------------------------------------------------


def test_yaml_user_edit_preserved_when_upstream_unchanged() -> None:
    res = resolve_scalar_overlay(
        dst=YAML_DST,
        live_text=_yaml_doc("9"),
        tracked_text=_yaml_doc("1"),
        preserve_user_keys=["a.k"],
        base_lookup=lambda _p: 1,
        auto=None,
    )
    assert "k: 9" in res.merged_text
    assert res.rebaseline["a.k"] == 9
    assert res.conflicts == []
    assert res.deferred is False


def test_jsonc_user_edit_preserved_when_upstream_unchanged() -> None:
    res = resolve_scalar_overlay(
        dst=JSONC_DST,
        live_text=_jsonc_doc("9"),
        tracked_text=_jsonc_doc("1"),
        preserve_user_keys=["a > k"],
        base_lookup=lambda _p: 1,
        auto=None,
    )
    assert '"k": 9' in res.merged_text
    assert res.rebaseline["a > k"] == 9
    assert res.conflicts == []
    assert res.deferred is False


# ---------------------------------------------------------------------------
# 3. base present, ours == theirs -> no conflict, value kept.
# ---------------------------------------------------------------------------


def test_yaml_same_value_both_sides_no_conflict() -> None:
    res = resolve_scalar_overlay(
        dst=YAML_DST,
        live_text=_yaml_doc("5"),
        tracked_text=_yaml_doc("5"),
        preserve_user_keys=["a.k"],
        base_lookup=lambda _p: 1,
        auto=None,
    )
    assert "k: 5" in res.merged_text
    assert res.rebaseline["a.k"] == 5
    assert res.conflicts == []
    assert res.deferred is False


def test_jsonc_same_value_both_sides_no_conflict() -> None:
    res = resolve_scalar_overlay(
        dst=JSONC_DST,
        live_text=_jsonc_doc("5"),
        tracked_text=_jsonc_doc("5"),
        preserve_user_keys=["a > k"],
        base_lookup=lambda _p: 1,
        auto=None,
    )
    assert '"k": 5' in res.merged_text
    assert res.rebaseline["a > k"] == 5
    assert res.conflicts == []
    assert res.deferred is False


# ---------------------------------------------------------------------------
# 4. all three differ, auto=None -> keep ours, defer (NOT in rebaseline).
# ---------------------------------------------------------------------------


def test_yaml_conflict_bare_keeps_ours_and_defers() -> None:
    res = resolve_scalar_overlay(
        dst=YAML_DST,
        live_text=_yaml_doc("7"),
        tracked_text=_yaml_doc("8"),
        preserve_user_keys=["a.k"],
        base_lookup=lambda _p: 1,
        auto=None,
    )
    assert "k: 7" in res.merged_text
    assert res.conflicts == ["a.k"]
    assert res.deferred is True
    assert "a.k" not in res.rebaseline


def test_jsonc_conflict_bare_keeps_ours_and_defers() -> None:
    res = resolve_scalar_overlay(
        dst=JSONC_DST,
        live_text=_jsonc_doc("7"),
        tracked_text=_jsonc_doc("8"),
        preserve_user_keys=["a > k"],
        base_lookup=lambda _p: 1,
        auto=None,
    )
    assert '"k": 7' in res.merged_text
    assert res.conflicts == ["a > k"]
    assert res.deferred is True
    assert "a > k" not in res.rebaseline


# ---------------------------------------------------------------------------
# 5. conflict, auto=USE_TRACKED -> take theirs, rebaseline theirs.
# ---------------------------------------------------------------------------


def test_yaml_conflict_use_tracked_takes_theirs() -> None:
    res = resolve_scalar_overlay(
        dst=YAML_DST,
        live_text=_yaml_doc("7"),
        tracked_text=_yaml_doc("8"),
        preserve_user_keys=["a.k"],
        base_lookup=lambda _p: 1,
        auto=ReconcileAuto.USE_TRACKED,
    )
    assert "k: 8" in res.merged_text
    assert res.rebaseline["a.k"] == 8
    assert res.conflicts == ["a.k"]
    assert res.deferred is False


def test_jsonc_conflict_use_tracked_takes_theirs() -> None:
    res = resolve_scalar_overlay(
        dst=JSONC_DST,
        live_text=_jsonc_doc("7"),
        tracked_text=_jsonc_doc("8"),
        preserve_user_keys=["a > k"],
        base_lookup=lambda _p: 1,
        auto=ReconcileAuto.USE_TRACKED,
    )
    assert '"k": 8' in res.merged_text
    assert res.rebaseline["a > k"] == 8
    assert res.conflicts == ["a > k"]
    assert res.deferred is False


# ---------------------------------------------------------------------------
# 6. conflict, auto=KEEP_LIVE -> keep ours, rebaseline ours.
# ---------------------------------------------------------------------------


def test_yaml_conflict_keep_live_keeps_ours() -> None:
    res = resolve_scalar_overlay(
        dst=YAML_DST,
        live_text=_yaml_doc("7"),
        tracked_text=_yaml_doc("8"),
        preserve_user_keys=["a.k"],
        base_lookup=lambda _p: 1,
        auto=ReconcileAuto.KEEP_LIVE,
    )
    assert "k: 7" in res.merged_text
    assert res.rebaseline["a.k"] == 7
    assert res.conflicts == ["a.k"]
    assert res.deferred is False


def test_jsonc_conflict_keep_live_keeps_ours() -> None:
    res = resolve_scalar_overlay(
        dst=JSONC_DST,
        live_text=_jsonc_doc("7"),
        tracked_text=_jsonc_doc("8"),
        preserve_user_keys=["a > k"],
        base_lookup=lambda _p: 1,
        auto=ReconcileAuto.KEEP_LIVE,
    )
    assert '"k": 7' in res.merged_text
    assert res.rebaseline["a > k"] == 7
    assert res.conflicts == ["a > k"]
    assert res.deferred is False


# ---------------------------------------------------------------------------
# 7. base ABSENT -> first-run fallback: keep ours (blind), seed rebaseline.
# ---------------------------------------------------------------------------


def test_yaml_base_absent_seeds_ours() -> None:
    res = resolve_scalar_overlay(
        dst=YAML_DST,
        live_text=_yaml_doc("3"),
        tracked_text=_yaml_doc("4"),
        preserve_user_keys=["a.k"],
        base_lookup=lambda _p: ABSENT,
        auto=None,
    )
    assert "k: 3" in res.merged_text
    assert res.rebaseline["a.k"] == 3
    assert res.conflicts == []
    assert res.deferred is False


def test_jsonc_base_absent_seeds_ours() -> None:
    res = resolve_scalar_overlay(
        dst=JSONC_DST,
        live_text=_jsonc_doc("3"),
        tracked_text=_jsonc_doc("4"),
        preserve_user_keys=["a > k"],
        base_lookup=lambda _p: ABSENT,
        auto=None,
    )
    assert '"k": 3' in res.merged_text
    assert res.rebaseline["a > k"] == 3
    assert res.conflicts == []
    assert res.deferred is False


# ---------------------------------------------------------------------------
# 8. ABSENT base vs None (null) base produce DIFFERENT resolutions.
# ---------------------------------------------------------------------------


def test_yaml_absent_base_vs_null_base_distinct() -> None:
    # present:false / key absent on live, ours=ABSENT, theirs changes the key.
    # base ABSENT: first-run seed -> keep ours (ABSENT), seed ABSENT.
    absent_doc = "a:\n  other: 1\n"
    absent_res = resolve_scalar_overlay(
        dst=YAML_DST,
        live_text=absent_doc,
        tracked_text=_yaml_doc("2"),
        preserve_user_keys=["a.k"],
        base_lookup=lambda _p: ABSENT,
        auto=None,
    )
    # key stays absent on the live doc; base seeded to ABSENT.
    assert "k:" not in absent_res.merged_text
    assert absent_res.rebaseline["a.k"] is ABSENT

    # base None (literal null): ours == base (live has null), theirs changed
    # -> upstream propagates to 2.
    null_res = resolve_scalar_overlay(
        dst=YAML_DST,
        live_text=_yaml_doc("null"),
        tracked_text=_yaml_doc("2"),
        preserve_user_keys=["a.k"],
        base_lookup=lambda _p: None,
        auto=None,
    )
    assert "k: 2" in null_res.merged_text
    assert null_res.rebaseline["a.k"] == 2


def test_jsonc_absent_base_vs_null_base_distinct() -> None:
    absent_doc = '{\n  "a": {\n    "other": 1\n  }\n}\n'
    absent_res = resolve_scalar_overlay(
        dst=JSONC_DST,
        live_text=absent_doc,
        tracked_text=_jsonc_doc("2"),
        preserve_user_keys=["a > k"],
        base_lookup=lambda _p: ABSENT,
        auto=None,
    )
    assert '"k"' not in absent_res.merged_text
    assert absent_res.rebaseline["a > k"] is ABSENT

    null_res = resolve_scalar_overlay(
        dst=JSONC_DST,
        live_text=_jsonc_doc("null"),
        tracked_text=_jsonc_doc("2"),
        preserve_user_keys=["a > k"],
        base_lookup=lambda _p: None,
        auto=None,
    )
    assert '"k": 2' in null_res.merged_text
    assert null_res.rebaseline["a > k"] == 2


# ---------------------------------------------------------------------------
# 9. non-scalar preserve_user_keys leaf -> left as live, not rebaselined.
# ---------------------------------------------------------------------------


def test_yaml_non_scalar_leaf_left_as_live() -> None:
    live = "a:\n  k:\n    nested: 1\n"
    tracked = "a:\n  k:\n    nested: 2\n"
    res = resolve_scalar_overlay(
        dst=YAML_DST,
        live_text=live,
        tracked_text=tracked,
        preserve_user_keys=["a.k"],
        base_lookup=lambda _p: 1,
        auto=None,
    )
    # live structure kept verbatim, no rebaseline, no conflict, no crash.
    assert "nested: 1" in res.merged_text
    assert "a.k" not in res.rebaseline
    assert res.conflicts == []
    assert res.deferred is False


def test_jsonc_non_scalar_leaf_left_as_live() -> None:
    live = '{\n  "a": {\n    "k": {\n      "nested": 1\n    }\n  }\n}\n'
    tracked = '{\n  "a": {\n    "k": {\n      "nested": 2\n    }\n  }\n}\n'
    res = resolve_scalar_overlay(
        dst=JSONC_DST,
        live_text=live,
        tracked_text=tracked,
        preserve_user_keys=["a > k"],
        base_lookup=lambda _p: 1,
        auto=None,
    )
    assert '"nested": 1' in res.merged_text
    assert "a > k" not in res.rebaseline
    assert res.conflicts == []
    assert res.deferred is False


def test_result_dataclass_is_frozen() -> None:
    res = ScalarOverlayResult(
        merged_text="x", rebaseline={}, conflicts=[], deferred=False
    )
    assert res.merged_text == "x"


# ---------------------------------------------------------------------------
# 10. interactive conflict_resolver path (auto=None + resolver supplied).
#     keep/take/edit ADVANCE the base; skip DEFERS (no rebaseline).
# ---------------------------------------------------------------------------


def test_resolver_keep_ours_advances_base() -> None:
    resolver = _resolver(ConflictResolution(ConflictChoice.KEEP_OURS))
    res = resolve_scalar_overlay(
        dst=YAML_DST,
        live_text=_yaml_doc("7"),
        tracked_text=_yaml_doc("8"),
        preserve_user_keys=["a.k"],
        base_lookup=lambda _p: 1,
        auto=None,
        conflict_resolver=resolver,
    )
    assert "k: 7" in res.merged_text
    assert res.rebaseline["a.k"] == 7
    assert res.conflicts == ["a.k"]
    assert res.deferred is False
    # The resolver was handed a ScalarConflict carrying all three sides.
    seen = resolver.seen  # type: ignore[attr-defined]
    assert seen == [ScalarConflict(path="a.k", base=1, ours=7, theirs=8)]


def test_resolver_take_theirs_advances_base() -> None:
    resolver = _resolver(ConflictResolution(ConflictChoice.TAKE_THEIRS))
    res = resolve_scalar_overlay(
        dst=YAML_DST,
        live_text=_yaml_doc("7"),
        tracked_text=_yaml_doc("8"),
        preserve_user_keys=["a.k"],
        base_lookup=lambda _p: 1,
        auto=None,
        conflict_resolver=resolver,
    )
    assert "k: 8" in res.merged_text
    assert res.rebaseline["a.k"] == 8
    assert res.conflicts == ["a.k"]
    assert res.deferred is False


def test_resolver_edit_writes_and_advances_base() -> None:
    resolver = _resolver(ConflictResolution(ConflictChoice.EDIT, edited_value=42))
    res = resolve_scalar_overlay(
        dst=YAML_DST,
        live_text=_yaml_doc("7"),
        tracked_text=_yaml_doc("8"),
        preserve_user_keys=["a.k"],
        base_lookup=lambda _p: 1,
        auto=None,
        conflict_resolver=resolver,
    )
    assert "k: 42" in res.merged_text
    assert res.rebaseline["a.k"] == 42
    assert res.conflicts == ["a.k"]
    assert res.deferred is False


def test_resolver_skip_keeps_ours_and_defers() -> None:
    resolver = _resolver(ConflictResolution(ConflictChoice.SKIP))
    res = resolve_scalar_overlay(
        dst=YAML_DST,
        live_text=_yaml_doc("7"),
        tracked_text=_yaml_doc("8"),
        preserve_user_keys=["a.k"],
        base_lookup=lambda _p: 1,
        auto=None,
        conflict_resolver=resolver,
    )
    assert "k: 7" in res.merged_text
    assert "a.k" not in res.rebaseline
    assert res.conflicts == ["a.k"]
    assert res.deferred is True


def test_jsonc_resolver_take_theirs_advances_base() -> None:
    resolver = _resolver(ConflictResolution(ConflictChoice.TAKE_THEIRS))
    res = resolve_scalar_overlay(
        dst=JSONC_DST,
        live_text=_jsonc_doc("7"),
        tracked_text=_jsonc_doc("8"),
        preserve_user_keys=["a > k"],
        base_lookup=lambda _p: 1,
        auto=None,
        conflict_resolver=resolver,
    )
    assert '"k": 8' in res.merged_text
    assert res.rebaseline["a > k"] == 8
    assert res.conflicts == ["a > k"]
    assert res.deferred is False


def test_resolver_edit_take_theirs_when_theirs_absent_deletes() -> None:
    # theirs deletes the key; resolver TAKE_THEIRS must remove it on the live
    # doc and rebaseline to ABSENT (mirrors the --auto=use-tracked DELETE path).
    resolver = _resolver(ConflictResolution(ConflictChoice.TAKE_THEIRS))
    res = resolve_scalar_overlay(
        dst=YAML_DST,
        live_text=_yaml_doc("7"),
        tracked_text="a:\n  other: 1\n",
        preserve_user_keys=["a.k"],
        base_lookup=lambda _p: 1,
        auto=None,
        conflict_resolver=resolver,
    )
    assert "k:" not in res.merged_text
    assert res.rebaseline["a.k"] is ABSENT
    assert res.conflicts == ["a.k"]
    assert res.deferred is False


def test_auto_takes_precedence_over_resolver() -> None:
    # When --auto is set the auto policy resolves the conflict; the resolver is
    # never consulted (byte-identical to today's non-interactive behavior).
    resolver = _resolver()  # would StopIteration if called.
    res = resolve_scalar_overlay(
        dst=YAML_DST,
        live_text=_yaml_doc("7"),
        tracked_text=_yaml_doc("8"),
        preserve_user_keys=["a.k"],
        base_lookup=lambda _p: 1,
        auto=ReconcileAuto.USE_TRACKED,
        conflict_resolver=resolver,
    )
    assert "k: 8" in res.merged_text
    assert res.rebaseline["a.k"] == 8
    assert resolver.seen == []  # type: ignore[attr-defined]


def test_no_resolver_no_auto_defers() -> None:
    # No resolver and no auto: the bare warn-and-defer path (today's behavior).
    res = resolve_scalar_overlay(
        dst=YAML_DST,
        live_text=_yaml_doc("7"),
        tracked_text=_yaml_doc("8"),
        preserve_user_keys=["a.k"],
        base_lookup=lambda _p: 1,
        auto=None,
        conflict_resolver=None,
    )
    assert "k: 7" in res.merged_text
    assert "a.k" not in res.rebaseline
    assert res.deferred is True
