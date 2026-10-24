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
4. Warm session (``MUTMUT_FAST_SESSION=0`` turns it off). One pytest session
   collects once; each mutant's tests run in a fork of it instead of a new
   ``pytest.main``, saving ~0.16 s of startup and collection per mutant. Its
   verdicts match a fresh pytest process per mutant; plain mutmut's children
   inherit state from its in-process stats run and miss some kills (15 of
   3,441 on setforge's core, each confirmed killed in a fresh process).

usage: mutmut-fast run [mutant-glob ...]   (any mutmut command; or run this file)
``MUTMUT_FAST_LINES=0`` turns line selection off.

A mutant that no test reaches still runs its function's fastest test, so it
survives exactly as before instead of turning into "no tests". This file is
self-contained so a repo can vendor it (``mutkit vendor``); edit it in mutkit.
"""

from __future__ import annotations

import ast
import contextlib
import difflib
import fnmatch
import inspect
import json
import os
import resource
import shutil
import signal
import socket
import sys
import tempfile
import time
from glob import escape as glob_escape
from math import ceil
from pathlib import Path

import coverage
import mutmut
import mutmut.__main__ as mm
import mutmut.mutation.trampoline as _trampoline
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
        and s.lineno <= line <= (s.end_lineno or s.lineno)
    ]
    if not inner:
        return None
    st = max(inner, key=lambda s: s.lineno)
    kids = [c.lineno for c in ast.iter_child_nodes(st) if isinstance(c, ast.stmt)]
    end = min(kids) - 1 if kids else (st.end_lineno or st.lineno)
    return range(st.lineno, end + 1)


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
            offsets: set[int] = set()
            for tag, i1, i2, _, _ in difflib.SequenceMatcher(
                None, orig_lines, mut_lines, autojunk=False
            ).get_opcodes():
                if tag != "equal":
                    offsets.update(range(i1, max(i2, i1 + 1)))
            spans = [_statement_span(orig, orig.lineno + off) for off in offsets if off]
            if not offsets or 0 in offsets or any(sp is None for sp in spans):
                out[mname] = None
            else:
                out[mname] = {ln for sp in spans if sp is not None for ln in sp}
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


_selected: dict[str, list[str]] = {}  # mutant name -> tests that reach it
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


def _selected_names(
    mutant_names: tuple[str, ...] | list[str],
) -> dict[str, tuple[str, set[int] | None]]:
    """Mutants this run will test -> (sandbox file, lines they change)."""
    out = {}
    for f in _mutated_files():
        rel = f.relative_to("mutants")
        module = ".".join(rel.with_suffix("").parts).removesuffix(".__init__")
        module = module.removeprefix("src.")  # mutmut drops a src/ layout prefix
        for mname, lines in changed_lines(f.read_text(encoding="utf-8")).items():
            name = f"{module}.{mname}".replace("__init__.", "")
            if not mutant_names or any(
                fnmatch.fnmatchcase(name, p) for p in mutant_names
            ):
                out[name] = (str(f), lines)
    return out


def _narrow(runner: mm.PytestRunner, mutant_names: tuple[str, ...] | list[str]) -> None:
    """Fill ``_selected``: for each mutant to run, the tests reaching its statement."""
    if os.environ.get("MUTMUT_FAST_LINES") == "0":
        return
    files = _mutated_files()
    changes = _selected_names(mutant_names)
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
        _stop_server()
        _save_history()


_fresh_stats: list[bool] = []  # a full stats run passed in this process


def _run_stats_collection(runner, tests=None):  # noqa: ANN001
    _orig_stats_collection(runner, tests)
    if not tests:  # mutmut exits when any test fails, so every test passed
        _fresh_stats.append(True)


def _collect_or_load_stats(runner):  # noqa: ANN001
    _orig_stats(runner)
    names = _requested[-1] if _requested else ()
    _narrow(runner, names)
    if os.environ.get("MUTMUT_FAST_SESSION") != "0":
        tests = sorted(
            set().union(*(_function_tests(n) for n in _selected_names(names)))
        )
        if tests:
            _server_tests[:] = tests  # started where mutmut forks its children


class _KillRecorder:
    def __init__(self, mutant_name: str) -> None:
        self.mutant_name, self.done = mutant_name, False

    def pytest_runtest_logreport(self, report) -> None:  # noqa: ANN001
        if report.failed and not self.done:
            self.done = True
            test = strip_prefix(report.nodeid, prefix="mutants/")
            (_KILLS / self.mutant_name).write_text(test, encoding="utf-8")


# --- Warm session: collect once, run each mutant's tests without pytest.main ---
# A server process forked after stats runs one pytest session over every test
# the run needs and, instead of running them, serves requests: per mutant it
# forks a grandchild that runs only the requested items (same order, stop at
# the first failure). Functions the collection itself calls run unmutated in
# the server, so their mutants keep the pytest.main path.
_SOCK = Path("mutants/.mutmut-fast.sock").absolute()
_READY = Path("mutants/.mutmut-fast-ready.json").absolute()
_server_pid: list[int] = []
_server_tests: list[str] = []


def _timeout_limit(mutant_name: str) -> float:
    config = mm.Config.get()
    est = sum(mutmut.duration_by_test[t] for t in _function_tests(mutant_name))
    return (est + config.timeout_constant) * config.timeout_multiplier


class _Server:
    def __init__(self, runner: mm.PytestRunner) -> None:
        self.runner, self.failed = runner, ""

    def pytest_runtest_logreport(self, report) -> None:  # noqa: ANN001
        if report.failed and not self.failed:
            self.failed = str(report.longrepr) or "failed"

    def pytest_collection_finish(self, session) -> None:  # noqa: ANN001
        os.environ["MUTANT_UNDER_TEST"] = ""  # collection ran in stats mode

    def pytest_runtestloop(self, session) -> bool:  # noqa: ANN001
        items = {strip_prefix(i.nodeid, prefix="mutants/"): i for i in session.items}
        hits = sorted(mutmut._stats)
        _READY.write_text(json.dumps({"collection_calls": hits}), encoding="utf-8")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(_SOCK))
        listener.listen(64)
        signal.signal(signal.SIGCHLD, signal.SIG_IGN)  # no zombie grandchildren
        while True:
            conn, _ = listener.accept()
            if os.fork() == 0:
                listener.close()
                # Ignored SIGCHLD makes subprocess report exit 0 for every child.
                signal.signal(signal.SIGCHLD, signal.SIG_DFL)
                code = 3
                try:
                    code = self.serve(conn, items)
                finally:
                    conn.sendall(f"{code}\n".encode())
                    if getattr(self, "basetemp", None):
                        shutil.rmtree(self.basetemp, ignore_errors=True)
                    os._exit(0)
            conn.close()

    def serve(self, conn: socket.socket, items: dict) -> int:
        request = json.loads(conn.makefile().readline())
        name, tests = request["mutant"], request["tests"]
        if any(t not in items for t in tests):
            return -1  # caller falls back to pytest.main
        os.environ["MUTANT_UNDER_TEST"] = name
        limit = request["limit"]
        resource.setrlimit(
            resource.RLIMIT_CPU, (ceil(limit * 2) + 1, ceil(limit * 2) + 2)
        )
        signal.alarm(ceil(limit) + 5)  # the caller's own timer fires first
        selected = [items[t] for t in tests]
        # Each pytest.main gets its own numbered tmp dir; forks of one session
        # would share one, so concurrent mutants running the same test collide.
        factory = selected[0].config._tmp_path_factory
        basetemp = Path(tempfile.mkdtemp(prefix="mutmut-fast-"))
        factory._given_basetemp, factory._basetemp = basetemp, None
        self.basetemp = basetemp
        for i, item in enumerate(selected):
            nxt = selected[i + 1] if i + 1 < len(selected) else None
            # The full hook, as pytest's own loop calls it: warnings, logging
            # and capture wrappers apply exactly as in a normal run.
            item.ihook.pytest_runtest_protocol(item=item, nextitem=nxt)
            if self.failed:
                (_KILLS / name).write_text(tests[i], encoding="utf-8")
                return 1
        return 0


def _start_server(runner: mm.PytestRunner, tests: list[str]) -> None:
    for f in (_SOCK, _READY):
        f.unlink(missing_ok=True)
    pid = os.fork()
    if pid:
        _server_pid.append(pid)
        return
    try:
        os.environ["MUTANT_UNDER_TEST"] = "stats"  # record calls made by collection
        mutmut._stats.clear()
        args = ["-q", "-p", "no:randomly", "-p", "no:random-order", *tests]
        with change_cwd("mutants"):
            runner.execute_pytest(args, plugins=[_Server(runner)])
    finally:
        os._exit(0)


def _stop_server() -> None:
    for pid in _server_pid:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def _ask_server(mutant_name: str, tests: list[str]) -> int | None:
    """The server's exit code for this mutant, or None to use pytest.main."""
    if not _server_pid:
        return None
    for _ in range(600):  # the server may still be collecting
        if _READY.exists() and _SOCK.exists():
            break
        time.sleep(0.5)
    else:
        return None
    calls = set(json.loads(_READY.read_text(encoding="utf-8"))["collection_calls"])
    if mm.mangled_name_from_mutant_name(mutant_name) in calls:
        return None
    request = {
        "mutant": mutant_name,
        "tests": tests,
        "limit": _timeout_limit(mutant_name),
    }
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.connect(str(_SOCK))
        conn.sendall((json.dumps(request) + "\n").encode())
        reply = conn.makefile().readline().strip()
    code = int(reply) if reply else 3
    return None if code == -1 else code


def _likely_killers(mutant_name: str) -> list[str]:
    """Last run's killer, then tests that killed this function's other mutants
    earlier in this run (most kills first). Only reorders; verdicts stay."""
    first = [_killers[mutant_name]] if mutant_name in _killers else []
    prefix = mm.mangled_name_from_mutant_name(mutant_name) + "__mutmut_"
    siblings: dict[str, int] = {}
    for f in _KILLS.glob(glob_escape(prefix) + "*"):
        test = f.read_text(encoding="utf-8")
        siblings[test] = siblings.get(test, 0) + 1
    return first + sorted(siblings, key=lambda t: -siblings[t])


def _estimate(mutant_name: str) -> float:
    """Expected seconds: the killer's time if one is known, else every test."""
    durations = mutmut.duration_by_test
    if mutant_name in _killers and _killers[mutant_name] in durations:
        return durations[_killers[mutant_name]]
    reach = _selected.get(mutant_name)
    tests = reach if reach is not None else _function_tests(mutant_name)
    return sum(durations.get(t, 0.0) for t in tests)


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
    for killer in reversed(_likely_killers(mutant_name)):
        if killer in ordered:  # likely killers first: same tests, earlier failure
            ordered.remove(killer)
            ordered.insert(0, killer)
    code = _ask_server(mutant_name, ordered)
    if code is not None:
        return code
    with change_cwd("mutants"):
        args = self._pytest_args_regular_run(ordered)
        return int(self.execute_pytest(args, plugins=[_KillRecorder(mutant_name)]))


mm._run, mm.collect_or_load_stats = _run, _collect_or_load_stats
# mutmut runs the cheapest mutants first; longest first keeps a few slow
# survivors from finishing alone at the end (only order changes).


def _longest_first(mutant_name: str) -> float:
    return -_estimate(mutant_name)


mm.estimated_worst_case_time = _longest_first
mm.run_stats_collection = _run_stats_collection
mm.PytestRunner.run_tests = _run_tests  # type: ignore[method-assign]
_orig_prepare = mm.PytestRunner.prepare_main_test_run


def _prepare_main_test_run(self):  # noqa: ANN001
    """mutmut calls this right before forking mutant children, after the forced
    fail run; the server forks here so it holds the state children would."""
    _orig_prepare(self)
    if _server_tests and not _server_pid:
        _start_server(self, _server_tests)


mm.PytestRunner.prepare_main_test_run = _prepare_main_test_run  # type: ignore[method-assign]


_source_paths: list[Path] = []


def _record_trampoline_hit(name: str) -> None:
    """mutmut 3.6's record_trampoline_hit, but source paths resolve once.

    mutmut resolves the relative source paths on every hit, so a test that
    changes directory (monkeypatch.chdir) and then calls mutated code fails the
    stats run with FileNotFoundError. The paths only matter for
    max_stack_depth; resolving them at the first hit (cwd = mutants/) keeps the
    recorded hits the same.
    """
    assert not name.startswith("src."), "Failed trampoline hit: name starts with src."
    config = mm.Config.get()
    if config.max_stack_depth != -1:
        if not _source_paths:
            _source_paths.extend(p.resolve(strict=True) for p in config.source_paths)
        f = inspect.currentframe()
        c = config.max_stack_depth
        while c and f:
            filename = f.f_code.co_filename
            f = f.f_back
            if "pytest" in filename or "hammett" in filename or "unittest" in filename:
                break
            file_path = Path(filename).resolve(strict=True)
            if any(path in file_path.parents for path in _source_paths):
                c -= 1
        if not c:
            return
    mutmut._stats.add(name)


mm.record_trampoline_hit = _record_trampoline_hit
_trampoline.record_trampoline_hit = _record_trampoline_hit


def main() -> None:
    mm.cli()


if __name__ == "__main__":
    main()
