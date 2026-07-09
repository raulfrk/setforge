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

import scripts.mutmut_diff_gate as gate
from scripts.mutmut_diff_gate import (
    EXIT_BLOCKED,
    EXIT_CLEAN,
    EXIT_FAILCLOSED,
    GateFailClosed,
    MutmutRun,
    Survivor,
    catastrophic_run,
    changed_lines_from_diff,
    count_mutants,
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


# --- count_mutants (total, any status) ----------------------------------------


def test_count_mutants_counts_every_status() -> None:
    # 7 data lines across killed/survived/timeout/suspicious/no-tests/not-checked.
    assert count_mutants(_RESULTS) == 7


def test_count_mutants_zero_on_empty_results() -> None:
    assert count_mutants("") == 0
    # A banner-only / no-data-line output also parses to zero.
    assert count_mutants("Failed to run clean test\n") == 0


# --- catastrophic_run (pure fail-closed detector) -----------------------------


def test_catastrophic_when_nonzero_exit_with_baseline_abort_signature() -> None:
    run = MutmutRun(returncode=1, output="...\nFailed to run clean test\n")
    # Even if results happen to be non-empty, the abort signature is decisive.
    assert catastrophic_run(run, _RESULTS, expected=True) is True


def test_catastrophic_on_failed_to_collect_stats_signature() -> None:
    run = MutmutRun(returncode=1, output="failed to collect stats. runner ...")
    assert catastrophic_run(run, "", expected=True) is True


def test_catastrophic_when_zero_mutants_parsed_and_expected() -> None:
    # Clean-baseline abort: mutmut wrote no results, so `mutmut results` is empty.
    run = MutmutRun(returncode=0, output="")
    assert catastrophic_run(run, "", expected=True) is True


def test_not_catastrophic_when_zero_mutants_but_not_expected() -> None:
    # A diff-scoped no-op path never gets here, but guard the semantics anyway:
    # if no mutants were expected, an empty results file is not catastrophic.
    run = MutmutRun(returncode=0, output="")
    assert catastrophic_run(run, "", expected=False) is False


def test_not_catastrophic_on_survivors_present_nonzero() -> None:
    # `mutmut run` exits nonzero merely because survivors remain — NOT an abort.
    run = MutmutRun(returncode=2, output="Running mutation testing\n... done\n")
    assert catastrophic_run(run, _RESULTS, expected=True) is False


def test_not_catastrophic_on_clean_pass_zero_survivors() -> None:
    # N mutants, all killed -> 0 survivors but count > 0 -> a clean pass, exit 0.
    all_killed = "\n".join(
        f"    setforge.scalar_merge.x_f__mutmut_{i}: killed" for i in range(5)
    )
    run = MutmutRun(returncode=0, output="... done\n")
    assert catastrophic_run(run, all_killed, expected=True) is False
    assert parse_results(all_killed) == []  # zero survivors
    assert count_mutants(all_killed) == 5  # but N mutants parsed


# --- main() fail-closed + no-double-run (impure edge, injected seams) ----------


def _stub_edge(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mutmut_run=None,
    results: str,
    diff: str | None = "",
) -> dict[str, int]:
    """Patch the gate's impure edge with in-memory stand-ins and return a call
    counter so tests can assert e.g. that ``--full`` never invokes mutmut run.

    ``diff=None`` leaves the REAL ``_git_diff_core`` in place (so a stubbed
    ``_git_merge_base`` raise can propagate as :class:`GateFailClosed`)."""
    calls = {"run": 0}

    def fake_run_mutmut(patterns):
        calls["run"] += 1
        return mutmut_run if mutmut_run is not None else MutmutRun(0, "")

    monkeypatch.setattr(gate, "_run_mutmut", fake_run_mutmut)
    monkeypatch.setattr(gate, "_mutmut_results", lambda: results)
    monkeypatch.setattr(gate, "read_allowlist", lambda: set())
    if diff is not None:
        monkeypatch.setattr(gate, "_git_diff_core", lambda: diff)
    return calls


def test_full_mode_does_not_rerun_mutmut(monkeypatch: pytest.MonkeyPatch) -> None:
    # The nightly workflow already ran `mutmut run`; --full must only read results.
    calls = _stub_edge(monkeypatch, results=_RESULTS)
    code = gate.main(["--full"])
    assert calls["run"] == 0  # NO second whole-engine pass
    assert code == EXIT_BLOCKED  # _RESULTS carries unkilled survivors


def test_full_mode_failclosed_on_empty_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Nightly baseline abort: workflow's `|| true` hid the exit, results empty.
    _stub_edge(monkeypatch, results="")
    assert gate.main(["--full"]) == EXIT_FAILCLOSED


def test_diff_mode_failclosed_on_baseline_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A changed core file -> mutmut runs -> it aborts on a red clean baseline.
    diff = (
        "--- a/setforge/scalar_merge.py\n"
        "+++ b/setforge/scalar_merge.py\n"
        "@@ -1 +1 @@ def f():\n"
        "+x = 1\n"
    )
    calls = _stub_edge(
        monkeypatch,
        mutmut_run=MutmutRun(1, "Failed to run clean test"),
        results="",  # aborted before writing results
        diff=diff,
    )
    assert gate.main([]) == EXIT_FAILCLOSED
    assert calls["run"] == 1  # it did attempt the scoped run


def test_diff_mode_failclosed_on_missing_origin_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom() -> str:
        raise GateFailClosed("origin/main not found")

    # diff=None keeps the REAL _git_diff_core, which calls the stubbed
    # _git_merge_base and lets the GateFailClosed propagate through main().
    _stub_edge(monkeypatch, results="", diff=None)
    monkeypatch.setattr(gate, "_git_merge_base", boom)
    assert gate.main([]) == EXIT_FAILCLOSED


def test_diff_mode_noop_exits_clean_without_running_mutmut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No core file in the diff -> fast no-op, mutmut never invoked.
    calls = _stub_edge(monkeypatch, results="", diff="")
    assert gate.main([]) == EXIT_CLEAN
    assert calls["run"] == 0


def test_full_mode_failclosed_on_mutmut_results_infra_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An infra-level `mutmut results` failure (nonzero exit) must fail-closed
    # (exit 2), not traceback — same error model as a missing origin/main.
    monkeypatch.setattr(gate, "read_allowlist", lambda: set())

    def boom() -> str:
        raise GateFailClosed("`mutmut results` failed — cannot read outcomes")

    monkeypatch.setattr(gate, "_mutmut_results", boom)
    assert gate.main(["--full"]) == EXIT_FAILCLOSED


def test_mutmut_results_raises_gatefailclosed_on_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The real _mutmut_results converts a nonzero `mutmut results` exit into
    # GateFailClosed (which main maps to exit 2), never a CalledProcessError.
    import subprocess

    def fake_run(cmd, *, check):
        return subprocess.CompletedProcess(cmd, returncode=3, stdout="", stderr="boom")

    monkeypatch.setattr(gate, "_run", fake_run)
    with pytest.raises(GateFailClosed):
        gate._mutmut_results()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
