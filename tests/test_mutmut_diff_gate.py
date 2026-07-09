"""Unit tests for the function-granular mutmut diff gate.

Every test drives the PURE gate logic with fixture data — no real ``mutmut
run`` and no ``git`` invocation. The script under test isolates its pure core
(diff-parse, mutant-name -> function, function -> AST line span, changed-line
intersection, allowlist subtraction, exit-code decision) behind functions that
take their inputs as arguments, so this suite exercises the gate deterministically
in milliseconds.
"""

from __future__ import annotations

import textwrap

import pytest

from scripts.mutmut_diff_gate import (
    Survivor,
    changed_lines_from_diff,
    decide,
    function_spans,
    parse_results,
    read_allowlist,
    span_for_mutant,
    survivors_on_changed_lines,
)

# --- changed_lines_from_diff (unified=0 parse) ---------------------------------

_DIFF = """\
diff --git a/setforge/scalar_merge.py b/setforge/scalar_merge.py
index 1111111..2222222 100644
--- a/setforge/scalar_merge.py
+++ b/setforge/scalar_merge.py
@@ -10,0 +11,3 @@ def resolve_scalar(base, ours, theirs):
+    a = 1
+    b = 2
+    c = 3
@@ -40,2 +44 @@ def _is_scalar(v):
-    old_one
-    old_two
+    new_single
diff --git a/setforge/base_store.py b/setforge/base_store.py
index 3333333..4444444 100644
--- a/setforge/base_store.py
+++ b/setforge/base_store.py
@@ -5 +5 @@ def load(self):
-    was_this
+    now_that
"""


def test_changed_lines_parses_multi_file_hunks() -> None:
    changed = changed_lines_from_diff(_DIFF)
    # scalar_merge: +11,3 -> {11,12,13}; +44 (count 1) -> {44}
    assert changed["setforge/scalar_merge.py"] == {11, 12, 13, 44}
    # base_store: +5 (implicit count 1) -> {5}
    assert changed["setforge/base_store.py"] == {5}


def test_changed_lines_ignores_pure_deletions() -> None:
    # A hunk that adds zero new lines (`+N,0`) contributes nothing.
    diff = textwrap.dedent(
        """\
        --- a/setforge/yaml_merge.py
        +++ b/setforge/yaml_merge.py
        @@ -12,3 +11,0 @@ def f():
        -gone_one
        -gone_two
        -gone_three
        """
    )
    changed = changed_lines_from_diff(diff)
    assert changed.get("setforge/yaml_merge.py", set()) == set()


def test_changed_lines_empty_diff_is_empty_mapping() -> None:
    assert changed_lines_from_diff("") == {}


# --- parse_results (mutmut results stdout) + status filtering ------------------

_RESULTS = """\
    setforge.scalar_merge.x_resolve_scalar__mutmut_1: killed
    setforge.scalar_merge.x_resolve_scalar__mutmut_4: survived
    setforge.scalar_merge.x_resolve_scalar__mutmut_9: survived
    setforge.base_store.xǁStoreǁload__mutmut_2: timeout
    setforge.yaml_merge.x_merge__mutmut_7: suspicious
    setforge.yaml_merge.x_merge__mutmut_8: no tests
    setforge.scalar_merge.xǁ_Absentǁ__repr____mutmut_1: not checked
"""


def test_parse_results_keeps_only_survived_timeout_suspicious() -> None:
    survivors = parse_results(_RESULTS)
    names = {s.name for s in survivors}
    assert names == {
        "setforge.scalar_merge.x_resolve_scalar__mutmut_4",
        "setforge.scalar_merge.x_resolve_scalar__mutmut_9",
        "setforge.base_store.xǁStoreǁload__mutmut_2",
        "setforge.yaml_merge.x_merge__mutmut_7",
    }
    # 'killed', 'no tests', 'not checked' are excluded.


def test_parse_results_carries_status() -> None:
    survivors = parse_results(_RESULTS)
    by_name = {s.name: s.status for s in survivors}
    assert by_name["setforge.base_store.xǁStoreǁload__mutmut_2"] == "timeout"
    assert by_name["setforge.yaml_merge.x_merge__mutmut_7"] == "suspicious"


# --- mutant name -> (module path, function) -----------------------------------


def test_survivor_module_path_and_function_plain_function() -> None:
    s = Survivor("setforge.scalar_merge.x_resolve_scalar__mutmut_4", "survived")
    assert s.module_path == "setforge/scalar_merge.py"
    assert s.function == "resolve_scalar"


def test_survivor_module_path_and_function_method() -> None:
    # Class-method mangling: `xǁClassǁmethod__mutmut_N` -> target span is `method`.
    s = Survivor("setforge.base_store.xǁStoreǁload__mutmut_2", "timeout")
    assert s.module_path == "setforge/base_store.py"
    assert s.function == "load"


def test_survivor_dunder_method() -> None:
    s = Survivor("setforge.scalar_merge.xǁ_Absentǁ__repr____mutmut_1", "suspicious")
    assert s.function == "__repr__"


# --- function_spans (AST over source) -----------------------------------------

_SOURCE = textwrap.dedent(
    """\
    def top():
        return 1


    def resolve_scalar(base, ours, theirs):
        x = 1
        y = 2
        return x + y


    class Store:
        def load(self):
            return "a"

        def save(self):
            return "b"
    """
)


def test_function_spans_top_level_and_methods() -> None:
    spans = function_spans(_SOURCE)
    # `top` occupies lines 1-2.
    assert spans["top"] == (1, 2)
    # `resolve_scalar` occupies 5-8.
    assert spans["resolve_scalar"] == (5, 8)
    # method `load` occupies 12-13; `save` 15-16.
    assert spans["load"] == (12, 13)
    assert spans["save"] == (15, 16)


def test_span_for_mutant_resolves_via_ast() -> None:
    s = Survivor("setforge.scalar_merge.x_resolve_scalar__mutmut_4", "survived")
    assert span_for_mutant(s, _SOURCE) == (5, 8)


def test_span_for_mutant_unknown_function_returns_none() -> None:
    s = Survivor("setforge.scalar_merge.x_ghost__mutmut_1", "survived")
    assert span_for_mutant(s, _SOURCE) is None


# --- survivors_on_changed_lines (function span x changed lines) ---------------


def test_survivor_kept_when_function_span_overlaps_changed_lines() -> None:
    survivors = [
        Survivor("setforge.scalar_merge.x_resolve_scalar__mutmut_4", "survived"),
    ]
    # resolve_scalar is lines 5-8; a change on line 6 overlaps.
    changed = {"setforge/scalar_merge.py": {6}}
    sources = {"setforge/scalar_merge.py": _SOURCE}
    kept = survivors_on_changed_lines(survivors, changed, sources)
    assert [s.name for s in kept] == [
        "setforge.scalar_merge.x_resolve_scalar__mutmut_4"
    ]


def test_survivor_dropped_when_function_span_misses_changed_lines() -> None:
    survivors = [
        Survivor("setforge.scalar_merge.x_resolve_scalar__mutmut_4", "survived"),
    ]
    # Change on line 2 (inside `top`), not inside resolve_scalar (5-8).
    changed = {"setforge/scalar_merge.py": {2}}
    sources = {"setforge/scalar_merge.py": _SOURCE}
    assert survivors_on_changed_lines(survivors, changed, sources) == []


def test_survivor_dropped_when_file_not_in_changed_set() -> None:
    survivors = [
        Survivor("setforge.base_store.xǁStoreǁload__mutmut_2", "timeout"),
    ]
    changed = {"setforge/scalar_merge.py": {6}}
    sources = {"setforge/base_store.py": _SOURCE}
    assert survivors_on_changed_lines(survivors, changed, sources) == []


# --- read_allowlist ------------------------------------------------------------


def test_read_allowlist_strips_comments_and_blanks(tmp_path) -> None:
    p = tmp_path / "allow.txt"
    p.write_text(
        textwrap.dedent(
            """\
            # header comment
            setforge.scalar_merge.x_resolve_scalar__mutmut_4  # equivalent mutant

            setforge.yaml_merge.x_merge__mutmut_7
            # trailing comment
            """
        ),
        encoding="utf-8",
    )
    allow = read_allowlist(p)
    assert allow == {
        "setforge.scalar_merge.x_resolve_scalar__mutmut_4",
        "setforge.yaml_merge.x_merge__mutmut_7",
    }


def test_read_allowlist_missing_file_is_empty(tmp_path) -> None:
    assert read_allowlist(tmp_path / "does-not-exist.txt") == set()


# --- decide (allowlist subtraction + exit code) -------------------------------


def test_decide_all_allowlisted_exits_zero() -> None:
    survivors = [
        Survivor("setforge.scalar_merge.x_resolve_scalar__mutmut_4", "survived"),
    ]
    allow = {"setforge.scalar_merge.x_resolve_scalar__mutmut_4"}
    remaining, code = decide(survivors, allow)
    assert remaining == []
    assert code == 0


def test_decide_remaining_survivor_exits_one() -> None:
    survivors = [
        Survivor("setforge.scalar_merge.x_resolve_scalar__mutmut_4", "survived"),
        Survivor("setforge.yaml_merge.x_merge__mutmut_7", "suspicious"),
    ]
    allow = {"setforge.scalar_merge.x_resolve_scalar__mutmut_4"}
    remaining, code = decide(survivors, allow)
    assert [s.name for s in remaining] == ["setforge.yaml_merge.x_merge__mutmut_7"]
    assert code == 1


def test_decide_no_survivors_exits_zero() -> None:
    remaining, code = decide([], set())
    assert remaining == []
    assert code == 0


# --- empty-diff no-op ----------------------------------------------------------


def test_empty_core_intersection_is_a_noop() -> None:
    # No core file changed -> the changed-line mapping restricted to core is
    # empty, and no survivor can overlap it.
    survivors = [
        Survivor("setforge.scalar_merge.x_resolve_scalar__mutmut_4", "survived"),
    ]
    kept = survivors_on_changed_lines(survivors, {}, {})
    assert kept == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
