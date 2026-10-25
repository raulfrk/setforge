#!/usr/bin/env python3
"""``mutmut`` that runs fewer tests per mutant: same CLI, no kill hidden.

Changes to mutmut 3.6. Items 1-3 keep every verdict; item 4 keeps every
kill and finds some that plain mutmut misses:

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
   ``pytest.main``, saving ~0.16 s of startup and collection per mutant. On
   setforge's core it finds 15 kills plain mutmut misses (its children
   inherit state from its in-process stats run; each kill was confirmed in a
   fresh pytest process). Like plain mutmut it still misses mutants of
   functions that only run at import time (e.g. ``TABLE = build()``).
5. Shared prep. ``prep OUT`` generates mutants, runs the stats run and the
   coverage pass once and writes their results to OUT; a run with
   ``MUTMUT_FAST_PREP=OUT`` loads them instead of repeating both, when they
   were made on the same commit and sandbox (else it runs them itself).

usage: mutmut-fast run [mutant-glob ...]   (any mutmut command; or run this file)
       mutmut-fast prep OUT_DIR
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
import hashlib
import inspect
import json
import os
import resource
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from glob import escape as glob_escape
from math import ceil
from pathlib import Path

import click
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
_REUSED = Path("mutants/.mutmut-fast-reused").absolute()  # one file per reused verdict


def _load_history(key: str) -> dict:
    """``killers``: mutant name -> the test that killed it last time.
    ``verdicts``: mutant name -> [fingerprint, exit code] of its last run."""
    try:
        return json.loads(_HISTORY.read_text(encoding="utf-8")).get(key, {})
    except (OSError, ValueError, AttributeError):
        return {}


_killers: dict[str, str] = _load_history("killers")
_verdicts: dict[str, list] = _load_history("verdicts")
_fingerprints: dict[str, str] = {}  # this run's fingerprint per mutant
_FULL = os.environ.get("MUTMUT_FAST_FULL") == "1"  # test every mutant again


def _save_history() -> None:
    # A stale killer only costs ordering, so old entries are kept, not pruned.
    for f in _KILLS.glob("*"):
        _killers[f.name] = f.read_text(encoding="utf-8")
    codes: dict[str, int] = {}
    seconds: dict[str, float] = {}
    for meta in Path("mutants").rglob("*.meta"):
        with contextlib.suppress(ValueError, KeyError):
            data = json.loads(meta.read_text(encoding="utf-8"))
            codes.update(data["exit_code_by_key"])
            seconds.update(data.get("durations_by_key", {}))
    for name, fingerprint in _fingerprints.items():
        if codes.get(name) is None:
            continue
        # a reused verdict keeps the time its real run took, for shard balancing
        previous = _verdicts.get(name, [])
        took = previous[2] if (_REUSED / name).exists() and len(previous) > 2 else None
        _verdicts[name] = [fingerprint, codes[name], took or seconds.get(name, 0.0)]
    _HISTORY.parent.mkdir(parents=True, exist_ok=True)
    history = {"killers": _killers, "verdicts": _verdicts}
    tmp = _HISTORY.with_suffix(".tmp")  # a pack may read it at any moment
    tmp.write_text(json.dumps(history, indent=1, sort_keys=True))
    tmp.replace(_HISTORY)


_INTERNAL = (".meta", ".pyc")  # mutmut's and Python's own files in the sandbox
# Tool caches that test runs rewrite in the sandbox (Hypothesis writes its
# example database there): no test input, so a run must not change the hash.
_CACHES = {"__pycache__", ".pytest_cache", ".hypothesis", ".mypy_cache", ".ruff_cache"}


def _test_support_hash() -> str:
    """One hash over every file of the copied test trees that is no test module:
    helpers, fixtures, conftests and data. A change to any of them re-tests every
    mutant. Test modules (test_*.py) are hashed per mutant instead.

    Each file is read from the project, not the sandbox: files that test runs
    write or change in the sandbox (logs, reports, leftovers in cwd) are no
    test input and must not block reuse."""
    sandbox = Path("mutants")
    sources = {Path(p).parts[0] for p in mm.Config.get().source_paths}
    h = hashlib.sha256()
    for path in sorted(sandbox.rglob("*")):
        rel = path.relative_to(sandbox)
        if (
            not path.is_file()
            or rel.parts[0] in sources
            or rel.name.startswith((".mutmut", "mutmut-"))
            or path.suffix in _INTERNAL
            or _CACHES & set(rel.parts)
            or (rel.name.startswith("test_") and rel.suffix == ".py")
            or not rel.is_file()  # written by a test run, not part of the project
        ):
            continue
        h.update(str(rel).encode() + b"\0" + rel.read_bytes())
    return h.hexdigest()


def _local_test_imports(path: Path) -> set[Path]:
    """Test modules a test module imports, absolute (``from tests.test_x import
    f``) or relative (``from .test_x import f``, ``from . import test_x``)."""
    out = set()
    with contextlib.suppress(SyntaxError, OSError, ValueError):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            bases: list[Path] = []
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    root = path.parent
                    for _ in range(node.level - 1):
                        root = root.parent
                else:
                    root = Path("mutants")
                module = (
                    root.joinpath(*(node.module or "").split("."))
                    if node.module
                    else root
                )
                bases = [module, *(module / a.name for a in node.names)]
            elif isinstance(node, ast.Import):
                bases = [Path("mutants", *a.name.split(".")) for a in node.names]
            for base in bases:
                target = base.with_suffix(".py")
                if target.name.startswith("test_") and target.is_file():
                    out.add(target)
    return out


def _mutant_sources() -> dict[str, str]:
    """Mutant (and ``__mutmut_orig``) name -> its function's source text."""
    sources: dict[str, str] = {}
    for f in _mutated_files():
        rel = f.relative_to("mutants")
        module = ".".join(rel.with_suffix("").parts).removesuffix(".__init__")
        module = module.removeprefix("src.").replace("__init__.", "")
        text = f.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, _FUNCS) and "__mutmut_" in node.name:
                sources[f"{module}.{node.name}"] = (
                    ast.get_source_segment(text, node) or ""
                )
    return sources


def _compute_fingerprints(names: tuple[str, ...] | list[str]) -> dict[str, str]:
    """Per mutant, a hash of what its verdict is assumed to depend on: its own
    and its original function's source, the ids of the tests it runs, their test
    modules and the test modules those import, every other file of the copied
    test trees (helpers, conftests, data), the mutmut config, the lock file and
    this wrapper.

    shortcut: source files outside the mutated function are not hashed (code it
    calls, module-level code, and test helpers kept inside the source package,
    e.g. ``pkg/testing.py``), so a change there can alter a reused verdict; a
    periodic --full run (MUTMUT_FAST_FULL=1) catches it.
    """
    base = hashlib.sha256(Path(__file__).read_bytes())
    for config in ("pyproject.toml", "uv.lock"):
        if Path(config).exists():
            base.update(Path(config).read_bytes())
    base.update(_test_support_hash().encode())
    sources = _mutant_sources()
    module_hashes: dict[Path, str] = {}

    def module_hash(path: Path) -> str:
        """The test module and, transitively, the test modules it imports."""
        if path not in module_hashes:
            module_hashes[path] = ""  # an import cycle stops here
            h = hashlib.sha256(path.read_bytes() if path.is_file() else b"-")
            for imported in sorted(_local_test_imports(path)):
                h.update(module_hash(imported).encode())
            module_hashes[path] = h.hexdigest()
        return module_hashes[path]

    out = {}
    for name in _selected_names(names):
        h = base.copy()
        orig = name.rpartition("__mutmut_")[0] + "__mutmut_orig"
        h.update(sources.get(name, "").encode())
        h.update(sources.get(orig, "").encode())
        reach = _selected.get(name)
        for test in sorted(reach if reach is not None else _function_tests(name)):
            h.update(test.encode())
            h.update(module_hash(Path("mutants") / test.split("::")[0]).encode())
        out[name] = h.hexdigest()
    return out


def _reusable(mutant_name: str) -> int | None:
    """Last run's killed/survived code when nothing it depends on changed."""
    previous = _verdicts.get(mutant_name)
    fingerprint = _fingerprints.get(mutant_name)
    if _FULL or not previous or fingerprint is None or previous[0] != fingerprint:
        return None
    return previous[1] if previous[1] in (0, 1) else None


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


def _narrow(
    runner: mm.PytestRunner, mutant_names: tuple[str, ...] | list[str]
) -> dict[str, dict[int, set[str]]]:
    """Fill ``_selected``: for each mutant to run, the tests reaching its statement.
    Returns the line map it used (empty when line selection is off)."""
    if os.environ.get("MUTMUT_FAST_LINES") == "0":
        return {}
    files = _mutated_files()
    changes = _selected_names(mutant_names)
    # Coverage must include every test any selected mutant would run.
    tests = sorted(set().union(*(_function_tests(n) for n in changes)))
    if not tests:
        return {}
    # A test's lines do not depend on which other tests run (the premise of
    # line selection), so prep's map over all tests serves any subset.
    cov = _prep_lines or _line_coverage(runner, files, tests)
    for name, (f, lines) in changes.items():
        if lines is not None:
            _selected[name] = sorted(
                set().union(*(cov[f].get(ln, set()) for ln in lines))
            )
    kept = sum(len(v) for v in _selected.values())
    total = sum(len(_function_tests(n)) for n in _selected)
    print(f"    mutmut_fast: {len(_selected)} mutants, {kept}/{total} test runs kept")
    return cov


# --- Shared prep: one job runs stats and coverage, every shard loads them ---
_PREP_STATS, _PREP_LINES = "mutmut-stats.json", "mutmut-fast-lines.json"
_prep_lines: dict[str, dict[int, set[str]]] = {}  # loaded from MUTMUT_FAST_PREP
_prep_out: list[Path] = []  # set by the prep command: stop after coverage
_STARTED = time.time()


def _commit() -> str:
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    return out.stdout.strip() if out.returncode == 0 else ""


def _sandbox_hashes() -> dict[str, str]:
    """Each mutated sandbox file's sha256: the line map's line numbers are its."""
    return {
        str(f): hashlib.sha256(f.read_bytes()).hexdigest()
        for f in sorted(_mutated_files())
    }


def _write_prep(out: Path, cov: dict[str, dict[int, set[str]]]) -> None:
    commit = _commit()
    if not commit:
        sys.exit("mutmut_fast: prep needs a git commit to tie its results to")
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path("mutants") / _PREP_STATS, out / _PREP_STATS)
    tests = sorted(set().union(*(t for lines in cov.values() for t in lines.values())))
    index = {t: i for i, t in enumerate(tests)}
    lines = {
        f: {ln: sorted(index[t] for t in ts) for ln, ts in by_line.items() if ts}
        for f, by_line in cov.items()
    }
    meta = {"commit": commit, "sandbox": _sandbox_hashes(), "tests": tests}
    (out / _PREP_LINES).write_text(json.dumps({**meta, "lines": lines}))
    print(f"    mutmut_fast: prep for {commit[:8]} written to {out}")


def _load_prep() -> bool:
    """Use ``MUTMUT_FAST_PREP``'s stats and line map if made on this commit and
    sandbox; anything else keeps the full setup, so a stale prep costs time only."""
    prep = os.environ.get("MUTMUT_FAST_PREP")
    if not prep:
        return False
    try:
        meta = json.loads((Path(prep) / _PREP_LINES).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        print(f"    mutmut_fast: no readable prep in {prep}, running stats here")
        return False
    if not meta["commit"] or meta["commit"] != _commit():
        print(f"    mutmut_fast: prep is for commit {meta['commit'][:8]}, not HEAD")
        return False
    if meta["sandbox"] != _sandbox_hashes():
        print("    mutmut_fast: prep's mutants differ from this sandbox's")
        return False
    shutil.copy2(Path(prep) / _PREP_STATS, Path("mutants") / _PREP_STATS)
    tests = meta["tests"]
    _prep_lines.update(
        {
            f: {int(ln): {tests[i] for i in idx} for ln, idx in by_line.items()}
            for f, by_line in meta["lines"].items()
        }
    )
    # mutmut's stats run sets this for every later pytest run in the process.
    os.environ["PY_IGNORE_IMPORTMISMATCH"] = "1"
    _fresh_stats.append(True)  # prep's stats run passed every test on this commit
    print(f"    mutmut_fast: loaded prep for {meta['commit'][:8]}")
    return True


@mm.cli.command()
@click.option("--max-children", type=int)
@click.argument("out", type=click.Path(path_type=Path))
def prep(out: Path, max_children: int | None) -> None:
    """Generate mutants, run stats and the coverage pass; write both to OUT."""
    _prep_out.append(out)
    mm._run((), max_children)  # _collect_or_load_stats exits after writing OUT


_orig_run, _orig_stats = mm._run, mm.collect_or_load_stats
_orig_stats_collection = mm.run_stats_collection
_requested: list[tuple[str, ...] | list[str]] = []


def _run(mutant_names, max_children):  # noqa: ANN001 - mirrors mutmut's signature
    _requested.append(mutant_names)
    for d in (_KILLS, _REUSED):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
    main_pid = os.getpid()

    def on_term(signum: int, frame: object) -> None:
        if os.getpid() == main_pid:  # mutmut stops its children on Ctrl-C
            raise KeyboardInterrupt
        os._exit(128 + signum)

    signal.signal(signal.SIGTERM, on_term)
    saver = threading.Thread(target=_save_periodically, args=(main_pid,), daemon=True)
    saver.start()
    try:
        return _orig_run(mutant_names, max_children)
    finally:
        _stop_server()
        with _save_lock:
            _save_history()


_save_lock = threading.Lock()


def _save_periodically(main_pid: int) -> None:
    """Save killers and verdicts every MUTMUT_FAST_SAVE_S seconds (default 600),
    so a shard stopped early keeps what it finished."""
    every = float(os.environ.get("MUTMUT_FAST_SAVE_S", "600"))
    while True:
        time.sleep(every)
        if os.getpid() != main_pid:
            return
        with _save_lock, contextlib.suppress(OSError, ValueError):
            _save_history()


_fresh_stats: list[bool] = []  # a full stats run passed in this process


_orig_popen_init = subprocess.Popen.__init__


def _popen_without_stats(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
    """A process a test starts during the stats run gets MUTANT_UNDER_TEST
    cleared. Its trampolines would otherwise call mutmut's hit recorder, which
    loads mutmut's config from the child's cwd and crashes (e.g. a git filter
    running setforge in a target repo). The child's hits never reached mutmut
    anyway: they land in the child's own memory."""
    env = kwargs.get("env")
    source = os.environ if env is None else env
    if source.get("MUTANT_UNDER_TEST") == "stats":
        kwargs["env"] = {**source, "MUTANT_UNDER_TEST": ""}
    _orig_popen_init(self, *args, **kwargs)


def _run_stats_collection(runner, tests=None):  # noqa: ANN001
    subprocess.Popen.__init__ = _popen_without_stats  # type: ignore[method-assign]
    try:
        _orig_stats_collection(runner, tests)
    finally:
        subprocess.Popen.__init__ = _orig_popen_init  # type: ignore[method-assign]
    if not tests:  # mutmut exits when any test fails, so every test passed
        _fresh_stats.append(True)


def _collect_or_load_stats(runner):  # noqa: ANN001
    # Loaded stats take mutmut's own path: list all tests, stats for new ones
    # only. On prep's commit it finds none, and its collection imports the tests
    # outside "fail" mode first, as the stats run would have.
    _load_prep()
    _orig_stats(runner)
    if _prep_out:
        _write_prep(_prep_out[0], _narrow(runner, ()))
        sys.exit(0)
    names = _requested[-1] if _requested else ()
    _narrow(runner, names)
    _fingerprints.update(_compute_fingerprints(names))
    reuse = sum(_reusable(n) is not None for n in _fingerprints)
    print(f"    mutmut_fast: {reuse}/{len(_fingerprints)} verdicts reused")
    print(f"    mutmut_fast: setup done in {time.time() - _STARTED:.0f}s")
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
_alive: list[int] = []  # pipe fd: the server's read end, mutmut's write end
_UNKNOWN_TEST = 199  # exit status: a requested test was not collected
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
        signal.signal(signal.SIGCHLD, signal.SIG_IGN)  # no zombie handlers
        while True:
            ready, _, _ = select.select([listener, _alive[0]], [], [])
            if _alive[0] in ready:  # mutmut's process is gone
                os._exit(0)
            conn, _ = listener.accept()
            if os.fork() == 0:
                listener.close()
                # Ignored SIGCHLD makes subprocess report exit 0 for every child.
                signal.signal(signal.SIGCHLD, signal.SIG_DFL)
                self.handle(conn, items)
                os._exit(0)
            conn.close()

    def handle(self, conn: socket.socket, items: dict) -> None:
        """Run one mutant's tests in a fork and reply with its exit status, so a
        test that dies from a signal reports that signal, as pytest.main would."""
        basetemp = Path(tempfile.mkdtemp(prefix="mutmut-fast-"))
        reply = 3
        try:
            request = json.loads(conn.makefile().readline())
            pid = os.fork()
            if pid == 0:
                code = 3
                try:
                    code = self.serve(request, items, basetemp)
                finally:
                    os._exit(_UNKNOWN_TEST if code == -1 else code)
            reply = os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])
        finally:
            shutil.rmtree(basetemp, ignore_errors=True)
            # mutmut may have killed the asking worker on a timeout already
            with contextlib.suppress(OSError):
                conn.sendall(f"{reply}\n".encode())

    def serve(self, request: dict, items: dict, basetemp: Path) -> int:
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
        factory._given_basetemp, factory._basetemp = basetemp, None
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
    """Fork the server twice so it is no child of mutmut's process: mutmut's
    os.wait() must only ever see its own mutant children."""
    for f in (_SOCK, _READY):
        f.unlink(missing_ok=True)
    alive_r, alive_w = os.pipe()  # EOF tells the server mutmut is gone
    pid_r, pid_w = os.pipe()
    first = os.fork()
    if first:
        os.close(alive_r)
        os.close(pid_w)
        os.waitpid(first, 0)
        with os.fdopen(pid_r) as pipe:
            pid = pipe.read()
        if pid:
            _server_pid.append(int(pid))
            _alive.append(alive_w)
        return
    os.close(alive_w)
    os.close(pid_r)
    server = os.fork()
    if server:
        os.write(pid_w, str(server).encode())
        os._exit(0)
    os.close(pid_w)
    _alive[:] = [alive_r]
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
        try:
            os.kill(_server_pid[0], 0)
        except ProcessLookupError:
            return None  # it died while collecting: run the plain way
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
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.connect(str(_SOCK))
            conn.sendall((json.dumps(request) + "\n").encode())
            reply = conn.makefile().readline().strip()
    except OSError:
        return None  # the server is gone: run this mutant the plain way
    if not reply:
        return None
    code = int(reply)
    if code == _UNKNOWN_TEST:
        return None
    if code < 0:  # the tests died from a signal: die from it too, as plain mutmut
        os.kill(os.getpid(), -code)
    return code


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
    reused = _reusable(mutant_name)
    if reused is not None:
        (_REUSED / mutant_name).touch()  # this runs in a mutant child
        return reused
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
