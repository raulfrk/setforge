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
    mutation_score,
    parse_results,
    read_allowlist,
    span_for_mutant,
    survivors_on_changed_lines,
)

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
    assert changed["setforge/scalar_merge.py"] == {11, 12, 13, 44}
    assert changed["setforge/base_store.py"] == {5}


def test_changed_lines_ignores_pure_deletions() -> None:
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


def test_parse_results_carries_status() -> None:
    survivors = parse_results(_RESULTS)
    by_name = {s.name: s.status for s in survivors}
    assert by_name["setforge.base_store.xǁStoreǁload__mutmut_2"] == "timeout"
    assert by_name["setforge.yaml_merge.x_merge__mutmut_7"] == "suspicious"


def test_survivor_module_path_and_function_plain_function() -> None:
    s = Survivor("setforge.scalar_merge.x_resolve_scalar__mutmut_4", "survived")
    assert s.module_path == "setforge/scalar_merge.py"
    assert s.function == "resolve_scalar"


def test_survivor_module_path_and_function_method() -> None:
    s = Survivor("setforge.base_store.xǁStoreǁload__mutmut_2", "timeout")
    assert s.module_path == "setforge/base_store.py"
    assert s.function == "load"


def test_survivor_dunder_method() -> None:
    s = Survivor("setforge.scalar_merge.xǁ_Absentǁ__repr____mutmut_1", "suspicious")
    assert s.function == "__repr__"


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
    assert spans["top"] == (1, 2)
    assert spans["resolve_scalar"] == (5, 8)
    assert spans["load"] == (12, 13)
    assert spans["save"] == (15, 16)


def test_span_for_mutant_resolves_via_ast() -> None:
    s = Survivor("setforge.scalar_merge.x_resolve_scalar__mutmut_4", "survived")
    assert span_for_mutant(s, _SOURCE) == (5, 8)


def test_span_for_mutant_unknown_function_returns_none() -> None:
    s = Survivor("setforge.scalar_merge.x_ghost__mutmut_1", "survived")
    assert span_for_mutant(s, _SOURCE) is None


def test_survivor_kept_when_function_span_overlaps_changed_lines() -> None:
    survivors = [
        Survivor("setforge.scalar_merge.x_resolve_scalar__mutmut_4", "survived"),
    ]
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


def test_empty_core_intersection_is_a_noop() -> None:
    survivors = [
        Survivor("setforge.scalar_merge.x_resolve_scalar__mutmut_4", "survived"),
    ]
    kept = survivors_on_changed_lines(survivors, {}, {})
    assert kept == []


def test_count_mutants_counts_every_status() -> None:
    assert count_mutants(_RESULTS) == 7


def test_count_mutants_zero_on_empty_results() -> None:
    assert count_mutants("") == 0
    assert count_mutants("Failed to run clean test\n") == 0


def test_result_parser_ignores_unindented_colon_banner() -> None:
    assert count_mutants("Mutation testing: complete\n") == 0


def test_result_parser_failclosed_on_unknown_status() -> None:
    results = "    setforge.scalar_merge.x_f__mutmut_1: newly invented"
    with pytest.raises(GateFailClosed, match="unrecognized mutant record"):
        count_mutants(results)


def test_result_parser_failclosed_on_non_mutant_data_record() -> None:
    with pytest.raises(GateFailClosed, match="unrecognized mutant record"):
        count_mutants("    summary: killed\n")


def test_result_parser_failclosed_on_deceptive_mutant_suffix() -> None:
    with pytest.raises(GateFailClosed, match="unrecognized mutant record"):
        count_mutants("    summary__mutmut_1: killed\n")


def test_result_parser_failclosed_on_indented_record_without_delimiter() -> None:
    results = "    malformed without delimiter\n    setforge.x_f__mutmut_1: killed"
    with pytest.raises(GateFailClosed, match="malformed indented record"):
        count_mutants(results)


def test_mutation_score_uses_killed_and_survived_only() -> None:
    assert mutation_score(_RESULTS, set()) == pytest.approx(1 / 3)


def test_mutation_score_excludes_allowlisted_survivors() -> None:
    allowlist = {"setforge.scalar_merge.x_resolve_scalar__mutmut_4"}
    assert mutation_score(_RESULTS, allowlist) == pytest.approx(1 / 2)


def test_catastrophic_when_nonzero_exit_with_baseline_abort_signature() -> None:
    run = MutmutRun(returncode=1, output="...\nFailed to run clean test\n")
    assert catastrophic_run(run, _RESULTS, expected=True) is True


def test_catastrophic_on_failed_to_collect_stats_signature() -> None:
    run = MutmutRun(returncode=1, output="failed to collect stats. runner ...")
    assert catastrophic_run(run, "", expected=True) is True


def test_catastrophic_when_zero_mutants_parsed_and_expected() -> None:
    run = MutmutRun(returncode=0, output="")
    assert catastrophic_run(run, "", expected=True) is True


def test_not_catastrophic_when_zero_mutants_but_not_expected() -> None:
    run = MutmutRun(returncode=0, output="")
    assert catastrophic_run(run, "", expected=False) is False


def test_not_catastrophic_on_survivors_present_nonzero() -> None:
    # nonzero-exit-alone (survivors remaining) is not the abort signal.
    run = MutmutRun(returncode=2, output="Running mutation testing\n... done\n")
    assert catastrophic_run(run, _RESULTS, expected=True) is False


def test_not_catastrophic_on_clean_pass_zero_survivors() -> None:
    all_killed = "\n".join(
        f"    setforge.scalar_merge.x_f__mutmut_{i}: killed" for i in range(5)
    )
    run = MutmutRun(returncode=0, output="... done\n")
    assert catastrophic_run(run, all_killed, expected=True) is False
    assert parse_results(all_killed) == []
    assert count_mutants(all_killed) == 5


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
    monkeypatch.setattr(gate, "_resolve_base_ref", lambda override=None: "origin/main")
    if diff is not None:
        monkeypatch.setattr(gate, "_git_diff_core", lambda base_ref: diff)
    return calls


def test_full_mode_does_not_rerun_mutmut(monkeypatch: pytest.MonkeyPatch) -> None:
    # --full must only read results the nightly workflow already produced.
    complete_results = "\n".join(
        line for line in _RESULTS.splitlines() if not line.endswith(": not checked")
    )
    calls = _stub_edge(monkeypatch, results=complete_results)
    code = gate.main(["--full"])
    assert calls["run"] == 0
    assert code == EXIT_BLOCKED


def test_full_mode_passes_above_eighty_percent_with_survivors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    results = "\n".join(
        [f"    setforge.scalar_merge.x_f__mutmut_{i}: killed" for i in range(81)]
        + [
            f"    setforge.scalar_merge.x_f__mutmut_{i}: survived"
            for i in range(81, 100)
        ]
        + ["    setforge.scalar_merge.x_f__mutmut_100: timeout"]
    )
    _stub_edge(monkeypatch, results=results)
    assert gate.main(["--full"]) == EXIT_CLEAN
    assert (
        "Outcomes: 19 survived (0 allowlisted), 0 no tests, 1 timeout, 0 suspicious."
    ) in capsys.readouterr().out


def test_full_mode_blocks_at_exactly_eighty_percent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = "\n".join(
        [f"    setforge.scalar_merge.x_f__mutmut_{i}: killed" for i in range(80)]
        + [
            f"    setforge.scalar_merge.x_f__mutmut_{i}: survived"
            for i in range(80, 100)
        ]
    )
    _stub_edge(monkeypatch, results=results)
    assert gate.main(["--full"]) == EXIT_BLOCKED


def test_full_mode_failclosed_on_empty_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_edge(monkeypatch, results="")
    assert gate.main(["--full"]) == EXIT_FAILCLOSED


def test_full_mode_failclosed_without_a_score_denominator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = "\n".join(
        [
            "    setforge.scalar_merge.x_f__mutmut_1: no tests",
            "    setforge.scalar_merge.x_f__mutmut_2: timeout",
        ]
    )
    _stub_edge(monkeypatch, results=results)
    assert gate.main(["--full"]) == EXIT_FAILCLOSED


def test_full_mode_failclosed_on_not_checked_mutants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = "\n".join(
        [f"    setforge.scalar_merge.x_f__mutmut_{i}: killed" for i in range(81)]
        + ["    setforge.scalar_merge.x_f__mutmut_82: survived"]
        + ["    setforge.scalar_merge.x_f__mutmut_83: not checked"]
    )
    _stub_edge(monkeypatch, results=results)
    assert gate.main(["--full"]) == EXIT_FAILCLOSED


def test_diff_mode_failclosed_on_baseline_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diff = (
        "--- a/setforge/scalar_merge.py\n"
        "+++ b/setforge/scalar_merge.py\n"
        "@@ -1 +1 @@ def f():\n"
        "+x = 1\n"
    )
    calls = _stub_edge(
        monkeypatch,
        mutmut_run=MutmutRun(1, "Failed to run clean test"),
        results="",
        diff=diff,
    )
    assert gate.main([]) == EXIT_FAILCLOSED
    assert calls["run"] == 1


def test_diff_mode_failclosed_on_not_checked_changed_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diff = (
        "--- a/setforge/scalar_merge.py\n"
        "+++ b/setforge/scalar_merge.py\n"
        "@@ -1 +1 @@ def f():\n"
        "+def f():\n"
    )
    results = "    setforge.scalar_merge.x_f__mutmut_1: not checked"
    _stub_edge(
        monkeypatch, mutmut_run=MutmutRun(1, "aborted"), results=results, diff=diff
    )
    monkeypatch.setattr(
        gate,
        "_read_sources",
        lambda paths: {"setforge/scalar_merge.py": "def f():\n    pass\n"},
    )
    assert gate.main([]) == EXIT_FAILCLOSED


def test_diff_mode_failclosed_on_missing_origin_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(base_ref: str) -> str:
        raise GateFailClosed("origin/main not found")

    _stub_edge(monkeypatch, results="", diff=None)
    monkeypatch.setattr(gate, "_git_merge_base", boom)
    assert gate.main([]) == EXIT_FAILCLOSED


def test_diff_mode_noop_exits_clean_without_running_mutmut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_edge(monkeypatch, results="", diff="")
    assert gate.main([]) == EXIT_CLEAN
    assert calls["run"] == 0


def test_full_mode_failclosed_on_mutmut_results_infra_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gate, "read_allowlist", lambda: set())

    def boom() -> str:
        raise GateFailClosed("`mutmut results` failed — cannot read outcomes")

    monkeypatch.setattr(gate, "_mutmut_results", boom)
    assert gate.main(["--full"]) == EXIT_FAILCLOSED


def test_mutmut_results_raises_gatefailclosed_on_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    def fake_run(cmd, *, check):
        return subprocess.CompletedProcess(cmd, returncode=3, stdout="", stderr="boom")

    monkeypatch.setattr(gate, "_run", fake_run)
    with pytest.raises(GateFailClosed):
        gate._mutmut_results()


def test_mutmut_results_requests_every_status(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    recorded: list[str] = []

    def fake_run(cmd, *, check):
        recorded.extend(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gate, "_run", fake_run)
    gate._mutmut_results()
    assert recorded == ["uv", "run", "mutmut", "results", "--all", "true"]


def _stub_git_base(
    monkeypatch: pytest.MonkeyPatch,
    *,
    local_main_exists: bool,
    origin_is_ancestor_of_main: bool,
) -> list[list[str]]:
    import subprocess

    recorded: list[list[str]] = []

    def fake_run(cmd, *, check):
        recorded.append(cmd)
        if cmd[:2] == ["git", "rev-parse"]:
            rc = 0 if local_main_exists else 128
            return subprocess.CompletedProcess(cmd, returncode=rc, stdout="", stderr="")
        if cmd[:2] == ["git", "merge-base"] and "--is-ancestor" in cmd:
            rc = 0 if origin_is_ancestor_of_main else 1
            return subprocess.CompletedProcess(cmd, returncode=rc, stdout="", stderr="")
        raise AssertionError(f"unexpected git call: {cmd}")

    monkeypatch.setattr(gate, "_run", fake_run)
    return recorded


def test_base_ref_prefers_local_main_when_origin_is_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_git_base(monkeypatch, local_main_exists=True, origin_is_ancestor_of_main=True)
    assert gate._resolve_base_ref() == "main"


def test_base_ref_ci_equivalent_origin_equals_main_keeps_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    same_sha = "deadbeef" * 5

    def fake_run(cmd, *, check):
        if cmd[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
        if cmd[:2] == ["git", "merge-base"] and "--is-ancestor" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
        if cmd[:2] == ["git", "merge-base"] and cmd[-1] == "HEAD":
            return subprocess.CompletedProcess(
                cmd, returncode=0, stdout=f"{same_sha}\n", stderr=""
            )
        raise AssertionError(f"unexpected git call: {cmd}")

    monkeypatch.setattr(gate, "_run", fake_run)
    assert gate._resolve_base_ref() == "main"
    assert gate._git_merge_base("main") == same_sha
    assert gate._git_merge_base("origin/main") == same_sha


def test_base_ref_keeps_origin_when_origin_is_ahead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_git_base(
        monkeypatch, local_main_exists=True, origin_is_ancestor_of_main=False
    )
    assert gate._resolve_base_ref() == "origin/main"


def test_base_ref_keeps_origin_when_no_local_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _stub_git_base(
        monkeypatch, local_main_exists=False, origin_is_ancestor_of_main=False
    )
    assert gate._resolve_base_ref() == "origin/main"
    assert not any("--is-ancestor" in cmd for cmd in recorded)


def test_git_merge_base_uses_supplied_base_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    seen: list[list[str]] = []

    def fake_run(cmd, *, check):
        seen.append(cmd)
        return subprocess.CompletedProcess(
            cmd, returncode=0, stdout="abc123\n", stderr=""
        )

    monkeypatch.setattr(gate, "_run", fake_run)
    assert gate._git_merge_base("main") == "abc123"
    assert seen == [["git", "merge-base", "main", "HEAD"]]


def test_base_override_bypasses_auto_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(cmd, *, check):
        raise AssertionError(f"resolver probed git despite --base override: {cmd}")

    monkeypatch.setattr(gate, "_run", boom)
    assert gate._resolve_base_ref(override="feature-x") == "feature-x"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
