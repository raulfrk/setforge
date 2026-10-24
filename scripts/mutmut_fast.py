#!/usr/bin/env python3
"""``mutmut`` that runs fewer tests per mutant: same CLI, same verdicts.

Three changes to mutmut 3.6, none of which changes a mutant's verdict:

1. Line selection. mutmut runs every test that calls the mutated FUNCTION. A
   test that never executes the statement a mutant changes runs exactly as on
   the original code, so it cannot kill it. One coverage pass per run maps
   lines to tests; each mutant runs only the tests reaching its statement.
2. Killer first. The test that killed a mutant last time runs first (history in
   ``MUTMUT_FAST_HISTORY``, default ``mutants/mutmut-fast-history.json``).
   Same tests, so the same verdict, found after one test instead of many.
3. No repeated clean run. mutmut's stats run already ran every test unmutated
   and stops on a failure, so the clean run that repeats it is skipped.

usage: mutmut-fast run [mutant-glob ...]   (any mutmut command; or run this file)
``MUTMUT_FAST_LINES=0`` turns line selection off.

A mutant that no test reaches still runs its function's fastest test, so it
survives exactly as before instead of turning into "no tests". This file is
self-contained so a repo can vendor it (``mutkit vendor``); edit it in mutkit.
"""

from __future__ import annotations

import ast
import difflib
import fnmatch
import json
import os
import shutil
import sys
from pathlib import Path

import coverage
import mutmut
import mutmut.__main__ as mm
from mutmut.utils.file_utils import change_cwd
from mutmut.utils.format_utils import strip_prefix

_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _statement_span(func: ast.AST, line: int) -> range | None:
    """Every line of the innermost statement holding ``line`` (header only for
    compound statements). Coverage may record any line of a multi-line
    statement, so the whole span counts."""
    inner = [
        s
        for s in ast.walk(func)
        if isinstance(s, ast.stmt)
        and s is not func
        and s.lineno <= line <= s.end_lineno
    ]
    if not inner:
        return None
    st = max(inner, key=lambda s: s.lineno)
    kids = [c.lineno for c in ast.iter_child_nodes(st) if isinstance(c, ast.stmt)]
    return range(st.lineno, (min(kids) - 1 if kids else st.end_lineno) + 1)


def changed_lines(source: str) -> dict[str, set[int] | None]:
    """Map each mutant function name in a mutmut sandbox file to the lines of
    its ``__mutmut_orig`` twin whose statements it changes; ``None`` when the
    change cannot be pinned to a statement (signature, decorators)."""
    lines = source.splitlines()
    funcs = {n.name: n for n in ast.walk(ast.parse(source)) if isinstance(n, _FUNCS)}
    out: dict[str, set[int] | None] = {}
    for name, orig in funcs.items():
        if not name.endswith("__mutmut_orig"):
            continue
        base = name[: -len("orig")]
        orig_lines = lines[orig.lineno - 1 : orig.end_lineno]
        for mname, mnode in funcs.items():
            if not mname.startswith(base) or mname == name:
                continue
            mut_lines = [
                ln.replace(mname, name)
                for ln in lines[mnode.lineno - 1 : mnode.end_lineno]
            ]
            offsets = set()
            for tag, i1, i2, _, _ in difflib.SequenceMatcher(
                None, orig_lines, mut_lines, autojunk=False
            ).get_opcodes():
                if tag != "equal":
                    offsets.update(range(i1, max(i2, i1 + 1)))
            spans = [_statement_span(orig, orig.lineno + off) for off in offsets if off]
            if not offsets or 0 in offsets or None in spans:
                out[mname] = None
            else:
                out[mname] = {ln for span in spans for ln in span}
    return out


def _mutated_files() -> list[Path]:
    return [
        p
        for p in Path("mutants").rglob("*.py")
        if "# mutmut generated" in p.read_text(encoding="utf-8")
    ]


class _Context:
    """pytest plugin: one coverage context per test, named like mutmut names tests."""

    def __init__(self, cov: coverage.Coverage) -> None:
        self.cov = cov

    def pytest_runtest_logstart(self, nodeid: str, location: object) -> None:
        self.cov.switch_context(strip_prefix(nodeid, prefix="mutants/"))


def _by_line(
    cov: coverage.Coverage, files: list[Path]
) -> dict[str, dict[int, set[str]]]:
    data = cov.get_data()
    return {
        str(f): {
            ln: set(ctx) - {""}
            for ln, ctx in data.contexts_by_lineno(str(f.absolute())).items()
        }
        for f in files
    }


def _line_coverage(
    runner: mm.PytestRunner, files: list[Path], tests: list[str]
) -> dict[str, dict[int, set[str]]]:
    """Run ``tests`` once on the unmutated sandbox; return file -> line -> tests."""
    cov = coverage.Coverage(data_file=None, include=[str(f.absolute()) for f in files])
    os.environ["MUTANT_UNDER_TEST"] = ""
    args = ["-q", "-p", "no:randomly", "-p", "no:random-order", *tests]
    with change_cwd("mutants"), cov.collect():
        code = runner.execute_pytest(args, plugins=[_Context(cov)])
    if code != 0:
        sys.exit(
            f"mutmut_fast: coverage run of the unmutated sandbox failed (exit {code})"
        )
    return _by_line(cov, files)


_selected: dict[str, list[str] | None] = {}  # mutant name -> tests that reach it
_HISTORY = Path(
    os.environ.get("MUTMUT_FAST_HISTORY", "mutants/mutmut-fast-history.json")
).absolute()
_KILLS = Path("mutants/.mutmut-fast-kills").absolute()  # one file per killed mutant


def _load_history() -> dict[str, str]:
    """Mutant name -> the test that killed it last time."""
    try:
        return json.loads(_HISTORY.read_text(encoding="utf-8"))["killers"]
    except (FileNotFoundError, KeyError, ValueError):
        return {}


_killers = _load_history()


def _save_history() -> None:
    # A stale killer only costs ordering, so old entries are kept, not pruned.
    for f in _KILLS.glob("*"):
        _killers[f.name] = f.read_text(encoding="utf-8")
    _HISTORY.parent.mkdir(parents=True, exist_ok=True)
    _HISTORY.write_text(json.dumps({"killers": _killers}, indent=1, sort_keys=True))


def _function_tests(name: str) -> set[str]:
    return set(
        mutmut.tests_by_mangled_function_name.get(
            mm.mangled_name_from_mutant_name(name), ()
        )
    )


def _narrow(runner: mm.PytestRunner, mutant_names: tuple[str, ...] | list[str]) -> None:
    """Fill ``_selected``: for each mutant to run, the tests reaching its statement."""
    if os.environ.get("MUTMUT_FAST_LINES") == "0":
        return
    files = _mutated_files()
    changes = {}  # mutant name -> (sandbox file, changed lines)
    for f in files:
        rel = f.relative_to("mutants")
        module = ".".join(rel.with_suffix("").parts).removesuffix(".__init__")
        module = module.removeprefix("src.")  # mutmut drops a src/ layout prefix
        for mname, lines in changed_lines(f.read_text(encoding="utf-8")).items():
            name = f"{module}.{mname}".replace("__init__.", "")
            if not mutant_names or any(
                fnmatch.fnmatchcase(name, p) for p in mutant_names
            ):
                changes[name] = (str(f), lines)
    # Coverage must include every test any selected mutant would run.
    tests = sorted(set().union(*(_function_tests(n) for n in changes)))
    if not tests:
        return
    cov = _line_coverage(runner, files, tests)
    for name, (f, lines) in changes.items():
        if lines is not None:
            _selected[name] = sorted(
                set().union(*(cov[f].get(ln, set()) for ln in lines))
            )
    kept = sum(len(v) for v in _selected.values())
    total = sum(len(_function_tests(n)) for n in _selected)
    print(f"    mutmut_fast: {len(_selected)} mutants, {kept}/{total} test runs kept")


_orig_run, _orig_stats = mm._run, mm.collect_or_load_stats
_orig_stats_collection = mm.run_stats_collection
_requested: list[tuple[str, ...] | list[str]] = []


def _run(mutant_names, max_children):  # noqa: ANN001 - mirrors mutmut's signature
    _requested.append(mutant_names)
    shutil.rmtree(_KILLS, ignore_errors=True)
    _KILLS.mkdir(parents=True)
    try:
        return _orig_run(mutant_names, max_children)
    finally:
        _save_history()


_fresh_stats: list[bool] = []  # a full stats run passed in this process


def _run_stats_collection(runner, tests=None):  # noqa: ANN001
    _orig_stats_collection(runner, tests)
    if not tests:  # mutmut exits when any test fails, so every test passed
        _fresh_stats.append(True)


def _collect_or_load_stats(runner):  # noqa: ANN001
    _orig_stats(runner)
    _narrow(runner, _requested[-1] if _requested else ())


class _KillRecorder:
    def __init__(self, mutant_name: str) -> None:
        self.mutant_name, self.done = mutant_name, False

    def pytest_runtest_logreport(self, report) -> None:  # noqa: ANN001
        if report.failed and not self.done:
            self.done = True
            test = strip_prefix(report.nodeid, prefix="mutants/")
            (_KILLS / self.mutant_name).write_text(test, encoding="utf-8")


def _run_tests(self, *, mutant_name, tests):  # noqa: ANN001
    if not mutant_name and _fresh_stats and os.environ["MUTANT_UNDER_TEST"] == "":
        return 0  # the clean run repeats the stats run that just passed
    if not mutant_name:
        with change_cwd("mutants"):
            return int(self.execute_pytest(self._pytest_args_regular_run(tests)))
    ordered = list(tests)
    reach = _selected.get(mutant_name)
    if reach is not None:  # _narrow covered every test of each selected mutant
        keep = set(reach)
        # Fallback keeps one test so the mutant still survives instead of "no tests".
        ordered = [t for t in ordered if t in keep] or ordered[:1]
    killer = _killers.get(mutant_name)
    if killer in ordered:  # last run's killer first: same tests, earlier failure
        ordered.remove(killer)
        ordered.insert(0, killer)
    with change_cwd("mutants"):
        args = self._pytest_args_regular_run(ordered)
        return int(self.execute_pytest(args, plugins=[_KillRecorder(mutant_name)]))


mm._run, mm.collect_or_load_stats = _run, _collect_or_load_stats
mm.run_stats_collection = _run_stats_collection
mm.PytestRunner.run_tests = _run_tests


def main() -> None:
    mm.cli()


if __name__ == "__main__":
    main()
