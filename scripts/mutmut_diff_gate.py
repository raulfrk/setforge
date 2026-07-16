#!/usr/bin/env python3
"""Function-granular mutmut mutation-testing gate (Tier-1, fail-closed).

A STANDALONE gate script (a sibling of :mod:`scripts.check_policy_lints` /
:mod:`scripts.check_schema_gates` — NOT a pytest test, because pytest is
skippable via markers / ``addopts`` which would silently disarm the contract).
It runs mutmut over the merge/reconcile/store core and BLOCKS on any surviving
mutant that this PR's diff is responsible for.

Two modes:

* **default (diff-scoped)** — the PR gate. Compute the NEW-file lines this PR
  changed in the core files, run mutmut ONLY over those files, and keep a
  survivor only when the function it lives in overlaps a changed line. An empty
  core intersection is a fast exit-0 no-op.
* **``--full``** — the nightly gate. Skip the diff filter entirely and block on
  every non-allowlisted survivor across the whole core.

The GATE decision is THIS SCRIPT'S OWN exit code (0 clean / 1 blocked / 2
fail-closed) — never mutmut's raw exit code (``mutmut run`` exits nonzero on
survivors, which is expected, not a gate failure).

Fidelity constraint (grounded in mutmut 3.6.0's surface):

  mutmut 3.6 mutant names are ``<module.dotted.path>.x_<function>__mutmut_<N>``
  (or, for methods, ``<module>.xǁ<Class>ǁ<method>__mutmut_<N>`` using the
  ``ǁ`` U+01C1 separator). They carry NO line number, and there is no per-mutant
  ``file:line`` surface. So true line-level scoping is impossible: this gate is
  FUNCTION-granular — a survivor's function name is parsed out of its mutant
  name, the function's CURRENT line span is resolved via AST over the source
  file, and the survivor is kept only if that span overlaps the PR's changed
  lines.

Chosen mutmut-3.6 scoping mechanism (verified by hand):

  ``mutmut run '<module>.*'`` — mutant selection is fnmatch-glob matched against
  the mutant key (mutmut ``collect_source_file_mutation_data``), so a
  ``'<module>.*'`` pattern per changed core file mutates ONLY those modules.
  ``source_paths`` stays the whole package (imports must resolve in the sandbox);
  narrowing happens at selection time, not by editing ``source_paths``.

Clean-baseline safety (fail-closed, exit 2): ``mutmut run`` runs the clean
(unmutated) test suite in its sandbox first and aborts with a nonzero exit +
``Failed to run clean test`` (or ``failed to collect stats``) BEFORE writing
any results if that suite is red. The gate refuses to read that as "0
survivors". :func:`catastrophic_run` detects it two ways — the run exited
nonzero with a baseline-abort signature in its output, OR ``mutmut results``
parses ZERO TOTAL mutants when mutants were expected (distinct from "0
survivors of N", a clean pass) — and :func:`main` returns exit 2 for it. A
missing / unresolvable diff base ref (see :func:`_git_merge_base` — the ref is
resolved dynamically: ``main``, ``origin/main``, or a ``--base`` override) is
likewise a :class:`GateFailClosed` exit 2, never a traceback. This mirrors the
0/1/2 fail-closed convention of the sibling gates
``scripts/check_policy_lints.py`` / ``scripts/check_schema_gates.py``.

Statuses collected as survivors: ``survived``, ``timeout``, ``suspicious``
(NOT ``survived`` alone — a timeout or suspicious mutant is unkilled too, and
NOT the aggregate ``export-cicd-stats`` counts, which lose the mutant ids).

Allowlist: :data:`ALLOWLIST_PATH` (``tests/mutmut_allowlist.txt``), one mutant
id per line (``#`` comments allowed). Listed ids are subtracted before the
gate decides — the route for equivalent / integration-only-covered survivors.

Invocation::

    uv run python scripts/mutmut_diff_gate.py             # PR diff-scoped
    uv run python scripts/mutmut_diff_gate.py --full       # nightly, whole core
    uv run python scripts/mutmut_diff_gate.py --base <ref> # override the diff base
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Mirrors [tool.mutmut].only_mutate; absent files are skipped (config predates
# the disposition/sections retirement, so the nominal scope is stale-but-larger).
CORE_FILES: tuple[str, ...] = (
    "setforge/disposition_merge.py",
    "setforge/markdown_merge.py",
    "setforge/scalar_merge.py",
    "setforge/structural_merge.py",
    "setforge/yaml_merge.py",
    "setforge/section_reconcile.py",
    "setforge/base_store.py",
    "setforge/base_store_format.py",
    "setforge/scalar_base_store.py",
)

ALLOWLIST_PATH = REPO_ROOT / "tests" / "mutmut_allowlist.txt"

UNKILLED_STATUSES: frozenset[str] = frozenset({"survived", "timeout", "suspicious"})

# --unified=0 gives one hunk per changed region; `+N[,M]` is the new-file start
# + count (M omitted means 1, M==0 means a pure deletion, no new line).
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_DIFF_NEWFILE_RE = re.compile(r"^\+\+\+ b/(.+)$")

# mutmut 3.6 mutant-name grammar: trailing `__mutmut_<N>`, module dotted-path,
# then an `x_`-prefixed function or an `xǁClassǁmethod` method form.
_MUTMUT_SUFFIX_RE = re.compile(r"__mutmut_\d+$")
_METHOD_SEP = "ǁ"  # mutmut's class/method mangling separator

EXIT_CLEAN = 0  # mirrors check_policy_lints.py / check_schema_gates.py 0/1/2
EXIT_BLOCKED = 1
EXIT_FAILCLOSED = 2

# Substrings mutmut prints when it aborts on a red clean baseline before
# writing results — matching either is the fail-closed "catastrophic" signal.
_BASELINE_ABORT_SIGNATURES: tuple[str, ...] = (
    "Failed to run clean test",
    "failed to collect stats",
)


@dataclass(frozen=True, slots=True)
class Survivor:
    """One unkilled mutant: its full mutmut name + status.

    The module path and target function are derived from the name (mutmut names
    carry no line number, so the function is the finest locatable granularity).
    """

    name: str
    status: str

    @property
    def _stem(self) -> str:
        """The name with the trailing ``__mutmut_<N>`` stripped."""
        return _MUTMUT_SUFFIX_RE.sub("", self.name)

    @property
    def module_dotted(self) -> str:
        """The dotted module path, e.g. ``setforge.scalar_merge``."""
        stem = self._stem
        local = self._local_part(stem)
        module = stem[: len(stem) - len(local)].rstrip(".")
        return module

    @property
    def module_path(self) -> str:
        """The source file path relative to the repo, e.g.
        ``setforge/scalar_merge.py``."""
        return self.module_dotted.replace(".", "/") + ".py"

    @property
    def function(self) -> str:
        """The function (or method) the mutant lives in.

        ``x_resolve_scalar`` -> ``resolve_scalar``; ``xǁStoreǁload`` -> ``load``;
        ``xǁ_Absentǁ__repr__`` -> ``__repr__`` (the span target is the method,
        not the class)."""
        local = self._local_part(self._stem)
        if _METHOD_SEP in local:
            return local.rsplit(_METHOD_SEP, 1)[-1]
        return local[2:] if local.startswith("x_") else local

    @staticmethod
    def _local_part(stem: str) -> str:
        """The mutant-local part of ``stem`` (after the module dotted-path).

        mutmut prefixes the function/method with ``x_`` / ``xǁ``; the module
        dotted-path never contains ``ǁ`` and never has an ``x_``-prefixed final
        segment, so the local part begins at the last ``.x`` boundary. SAFETY:
        this relies on no CORE_FILES module component itself being named
        ``x*`` (which would make ``.x`` match inside the module path). The core
        is the merge/reconcile/store modules — none is x-prefixed — so the last
        ``.x`` is always the mutant marker, never a module segment."""
        idx = stem.rfind(".x")
        return stem[idx + 1 :] if idx != -1 else stem


def changed_lines_from_diff(diff_text: str) -> dict[str, set[int]]:
    """Parse a ``git diff --unified=0`` into per-file sets of changed NEW-file
    line numbers. Pure-deletion hunks (``+N,0``) contribute nothing."""
    changed: dict[str, set[int]] = {}
    current: str | None = None
    for line in diff_text.splitlines():
        m_file = _DIFF_NEWFILE_RE.match(line)
        if m_file:
            current = m_file.group(1)
            changed.setdefault(current, set())
            continue
        m_hunk = _HUNK_RE.match(line)
        if m_hunk and current is not None:
            start = int(m_hunk.group(1))
            count = int(m_hunk.group(2)) if m_hunk.group(2) is not None else 1
            for offset in range(count):
                changed[current].add(start + offset)
    return {path: lines for path, lines in changed.items() if lines}


def _result_lines(results_text: str) -> list[tuple[str, str]]:
    """Every ``    <mutant_name>: <status>`` data line as ``(name, status)``.

    Non-data lines (blanks, banners) yield nothing. This is the total-mutant
    view — every status, not just the unkilled ones — so callers can tell
    "0 survivors of N mutants" (a clean pass) from "0 mutants parsed" (a
    baseline abort that wrote no results)."""
    out: list[tuple[str, str]] = []
    for raw in results_text.splitlines():
        line = raw.strip()
        if ": " not in line:
            continue
        name, _, status = line.rpartition(": ")
        name = name.strip()
        status = status.strip()
        if name and status:
            out.append((name, status))
    return out


def count_mutants(results_text: str) -> int:
    """Total number of mutants ``mutmut results`` reported, in ANY status.

    Zero means mutmut wrote no usable results (e.g. a clean-baseline abort),
    which is distinct from "0 survivors of N" and is the fail-closed signal."""
    return len(_result_lines(results_text))


def parse_results(results_text: str) -> list[Survivor]:
    """Parse ``mutmut results`` stdout into the unkilled :class:`Survivor` set.

    Each data line is ``    <mutant_name>: <status>``. Only the three unkilled
    statuses (:data:`UNKILLED_STATUSES`) are retained."""
    return [
        Survivor(name, status)
        for name, status in _result_lines(results_text)
        if status in UNKILLED_STATUSES
    ]


def function_spans(source: str) -> dict[str, tuple[int, int]]:
    """Map every function/method name in ``source`` to its ``(start, end)``
    line span (1-based, inclusive) via AST.

    ``ast.walk`` visits ALL depths, and duplicate names collapse to the LAST
    definition seen. ASSUMPTION (holds for the core): mutmut mutates only
    top-level functions and one-level-deep methods, and the merge/reconcile/
    store modules do not reuse a function/method name across scopes — so the
    bare-name lookup a mutant needs is unambiguous. If a future core module
    reused a name, the last-wins collapse could pick the wrong span; the
    modules are small and name-unique today, so this is safe."""
    spans: dict[str, tuple[int, int]] = {}
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = node.end_lineno or node.lineno
            spans[node.name] = (node.lineno, end)
    return spans


def span_for_mutant(survivor: Survivor, source: str) -> tuple[int, int] | None:
    """The line span of ``survivor``'s function within ``source``, or ``None``
    if that function is not found (e.g. renamed away)."""
    return function_spans(source).get(survivor.function)


def survivors_on_changed_lines(
    survivors: list[Survivor],
    changed: dict[str, set[int]],
    sources: dict[str, str],
) -> list[Survivor]:
    """Keep only survivors whose function span overlaps a changed line in the
    same file. A survivor whose file is not in ``changed``/``sources``, or whose
    function cannot be resolved, or whose span misses every changed line, drops."""
    kept: list[Survivor] = []
    for s in survivors:
        path = s.module_path
        changed_here = changed.get(path)
        source = sources.get(path)
        if not changed_here or source is None:
            continue
        span = span_for_mutant(s, source)
        if span is None:
            continue
        start, end = span
        if any(start <= line <= end for line in changed_here):
            kept.append(s)
    return kept


def read_allowlist(path: Path = ALLOWLIST_PATH) -> set[str]:
    """Read the mutant-id allowlist: one id per line, ``#`` comments + blanks
    ignored. A missing file is an empty allowlist."""
    if not path.exists():
        return set()
    out: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.add(line)
    return out


def decide(
    survivors: list[Survivor], allowlist: set[str]
) -> tuple[list[Survivor], int]:
    """Subtract the allowlist and pick the exit code: :data:`EXIT_BLOCKED` if any
    survivor remains, else :data:`EXIT_CLEAN`. Returns ``(remaining, exit_code)``."""
    remaining = [s for s in survivors if s.name not in allowlist]
    return remaining, (EXIT_BLOCKED if remaining else EXIT_CLEAN)


@dataclass(frozen=True, slots=True)
class MutmutRun:
    """The captured outcome of a ``mutmut run`` invocation: its return code and
    combined stdout+stderr. Kept as data so the catastrophic-run classifier is
    a PURE function, unit-testable with injected values (no real subprocess)."""

    returncode: int
    output: str


def catastrophic_run(run: MutmutRun, results_text: str, *, expected: bool) -> bool:
    """True when mutmut did NOT produce usable mutation results — the
    fail-closed signal (distinct from "clean pass, 0 survivors").

    Two independent detectors, either sufficient:

    * the run exited nonzero AND its output carries a baseline-abort signature
      (``Failed to run clean test`` / ``failed to collect stats``) — mutmut
      bailed before mutating anything; and
    * ``results_text`` parses ZERO total mutants while mutants were ``expected``
      (a scoped run with patterns, or ``--full``) — the results file is empty /
      unusable, which a real run over a non-empty core never is.

    A nonzero return code ALONE is NOT catastrophic: ``mutmut run`` exits
    nonzero whenever survivors remain, which is the normal blocking case."""
    if run.returncode != 0 and any(
        sig in run.output for sig in _BASELINE_ABORT_SIGNATURES
    ):
        return True
    return expected and count_mutants(results_text) == 0


def _run(cmd: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=check
    )


def _existing_core_files() -> list[str]:
    """The core files that actually exist on disk (tolerating stale config)."""
    return [f for f in CORE_FILES if (REPO_ROOT / f).exists()]


class GateFailClosed(Exception):
    """A precondition failed such that the gate cannot compute a verdict (e.g.
    the diff base ref is absent). Carries a user-facing diagnostic; ``main``
    catches it and returns :data:`EXIT_FAILCLOSED`, never a traceback."""


def _resolve_base_ref(override: str | None = None) -> str:
    """The ref to diff against — ``origin/main`` by default.

    ``override`` (the ``--base`` CLI arg) wins outright: it is returned verbatim
    with no git probing.

    Absent an override, prefer LOCAL ``main``'s fork-point when ``origin/main``
    is STALE. This project's ``main`` is often UNPUSHED, so ``origin/main`` lags
    local ``main`` by all the unpushed history; diffing against ``origin/main``
    would then span main's unpushed commits and mis-scope the gate. The rule:

    * a local ``main`` ref exists (``git rev-parse --verify --quiet
      refs/heads/main`` exits 0), AND
    * ``origin/main`` is an ancestor of local ``main`` (``git merge-base
      --is-ancestor origin/main main`` exits 0)

    -> return ``"main"`` (use the local fork-point). This is the stale-origin
    case. When ``origin/main == main`` (CI after fetch) is-ancestor is also true,
    but the merge-base sha is IDENTICAL, so CI diff-scoping is provably unchanged.
    When ``origin/main`` is ahead of or diverged from local ``main``
    (is-ancestor false — covers both the ahead case and unrelated/diverged
    histories), or no local ``main`` ref exists (some CI checkouts), fall
    through to ``"origin/main"``."""
    if override is not None:
        return override
    default = "origin/main"
    if (
        _run(
            ["git", "rev-parse", "--verify", "--quiet", "refs/heads/main"], check=False
        ).returncode
        != 0
    ):
        return default
    if (
        _run(
            ["git", "merge-base", "--is-ancestor", "origin/main", "main"], check=False
        ).returncode
        == 0
    ):
        return "main"
    return default


def _git_merge_base(base_ref: str) -> str:
    """The ``git merge-base <base_ref> HEAD`` fork-point sha.

    A missing / unresolvable ``base_ref`` (e.g. ``origin/main`` on a fresh local
    clone that never fetched it) makes ``git merge-base`` exit nonzero; that is
    surfaced as a :class:`GateFailClosed` with a fix hint, not an uncaught
    traceback."""
    proc = _run(["git", "merge-base", base_ref, "HEAD"], check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise GateFailClosed(
            f"cannot locate the {base_ref} fork point "
            f"(`git merge-base {base_ref} HEAD` failed). Fetch it first "
            "(`git fetch origin main`), or the gate cannot scope the diff."
        )
    return proc.stdout.strip()


def _git_diff_core(base_ref: str) -> str:
    """``git diff --unified=0 <merge-base>...HEAD -- <core files>`` text.

    Three-dot against an explicit :func:`_git_merge_base` — never a two-dot
    ``..HEAD`` (which would diff against ``base_ref``'s tip, not the fork point)
    — so the changed set is exactly what this branch introduced."""
    base = _git_merge_base(base_ref)
    result = _run(
        [
            "git",
            "diff",
            "--unified=0",
            f"{base}...HEAD",
            "--",
            *_existing_core_files(),
        ],
        check=True,
    )
    return result.stdout


def _run_mutmut(patterns: list[str] | None) -> MutmutRun:
    """Run ``mutmut run`` (optionally scoped to ``patterns``); capture the outcome.

    mutmut exits nonzero when survivors remain — that is EXPECTED and not a gate
    failure (the gate reads survivors from ``mutmut results`` instead). But a
    clean-baseline abort exits nonzero and prints a baseline-abort signature
    while writing NO results. The returncode + output are captured here so
    :func:`catastrophic_run` can tell the two nonzero cases apart and fail-closed
    on the abort."""
    cmd = ["uv", "run", "mutmut", "run", *(patterns or [])]
    proc = _run(cmd, check=False)
    return MutmutRun(returncode=proc.returncode, output=proc.stdout + proc.stderr)


def _mutmut_results() -> str:
    """``mutmut results`` stdout. An infra-level failure (nonzero exit) is
    surfaced as a :class:`GateFailClosed` (exit 2), matching :func:`_git_merge_base`
    — never an uncaught ``CalledProcessError`` traceback."""
    proc = _run(["uv", "run", "mutmut", "results"], check=False)
    if proc.returncode != 0:
        raise GateFailClosed(
            "`mutmut results` failed — cannot read mutation outcomes "
            f"(exit {proc.returncode})."
        )
    return proc.stdout


def _read_sources(paths: set[str]) -> dict[str, str]:
    sources: dict[str, str] = {}
    for path in paths:
        fp = REPO_ROOT / path
        if fp.exists():
            sources[path] = fp.read_text(encoding="utf-8")
    return sources


def _print_block(remaining: list[Survivor]) -> None:
    print("Mutation gate: surviving mutants block this change:", file=sys.stderr)
    for s in sorted(remaining, key=lambda s: s.name):
        print(f"  {s.status:>10}  {s.name}", file=sys.stderr)
    print(
        "\nKill each with a test, or (if equivalent / integration-only-covered) "
        f"add its id + a reason to {ALLOWLIST_PATH.relative_to(REPO_ROOT)}.",
        file=sys.stderr,
    )


def _print_failclosed(reason: str) -> None:
    print(f"Mutation gate FAIL-CLOSED (exit 2): {reason}", file=sys.stderr)


def _run_full(allowlist: set[str]) -> int:
    """Nightly ``--full`` path: gate the results the WORKFLOW already produced.

    The nightly workflow runs ``mutmut run || true`` itself (the ``|| true``
    swallows mutmut's survivors-present nonzero), so this does NOT re-run the
    whole-engine pass — it only reads + gates the existing results. Because the
    workflow's ``|| true`` hides a baseline-abort exit too, the fail-closed
    protection here rides on the results-side detector: zero total mutants
    parsed (an empty/unusable results file) is treated as catastrophic."""
    results_text = _mutmut_results()
    if catastrophic_run(MutmutRun(0, ""), results_text, expected=True):
        _print_failclosed(
            "mutmut produced no usable mutation results (0 mutants parsed) — "
            "the nightly `mutmut run` likely aborted on a red clean baseline."
        )
        return EXIT_FAILCLOSED
    survivors = parse_results(results_text)
    remaining, code = decide(survivors, allowlist)
    if remaining:
        _print_block(remaining)
    return code


def _run_diff(allowlist: set[str], base_ref: str) -> int:
    """PR diff-scoped path: run mutmut over only the changed core modules and
    gate survivors whose function overlaps a changed line."""
    diff_text = _git_diff_core(base_ref)
    changed = changed_lines_from_diff(diff_text)
    core = set(_existing_core_files())
    changed = {p: lines for p, lines in changed.items() if p in core}
    if not changed:
        return EXIT_CLEAN

    patterns = [p[: -len(".py")].replace("/", ".") + ".*" for p in sorted(changed)]
    run = _run_mutmut(patterns)

    results_text = _mutmut_results()
    if catastrophic_run(run, results_text, expected=True):
        _print_failclosed(
            "mutmut did not produce usable mutation results for the changed "
            "core modules — a red clean baseline or an aborted run. Refusing "
            "to read this as '0 survivors'."
        )
        return EXIT_FAILCLOSED

    survivors = parse_results(results_text)
    sources = _read_sources(set(changed))
    on_diff = survivors_on_changed_lines(survivors, changed, sources)
    remaining, code = decide(on_diff, allowlist)
    if remaining:
        _print_block(remaining)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full",
        action="store_true",
        help="gate on every non-allowlisted survivor across the whole core "
        "(nightly), skipping the PR-diff line filter",
    )
    parser.add_argument(
        "--base",
        metavar="REF",
        default=None,
        help="diff base ref to scope changed lines against, overriding the "
        "auto-selection (default: origin/main, or local `main`'s fork-point "
        "when origin/main is a stale ancestor of it). Ignored with --full.",
    )
    args = parser.parse_args(argv)

    allowlist = read_allowlist()
    try:
        if args.full:
            return _run_full(allowlist)
        return _run_diff(allowlist, _resolve_base_ref(args.base))
    except GateFailClosed as exc:
        _print_failclosed(str(exc))
        return EXIT_FAILCLOSED


if __name__ == "__main__":
    sys.exit(main())
