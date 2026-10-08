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
* **``--full``** — the nightly gate. Skip the diff filter and require a mutation
  score strictly above 80% across the whole core.
* **``--results-only``** — score a diff from the complete results of a fresh
  full-core run on this exact checkout, without executing the same mutants
  again. The caller must retain the full-run log as provenance.

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

Chosen mutmut-3.6 scoping mechanism:

  ``mutmut run '<pattern>' ...`` — mutant selection is fnmatch-glob matched
  against the mutant key (mutmut ``collect_source_file_mutation_data``). The gate
  only blocks on survivors in changed FUNCTIONS, so it passes one pattern per
  changed function (``<module>.x_<function>__mutmut_*`` or
  ``<module>.xǁ<Class>ǁ<method>__mutmut_*``; the ``__mutmut_`` suffix keeps
  ``add`` from selecting ``add_all``). See :func:`scoped_patterns`. A changed line
  outside every top-level function or top-level-class method falls back to the
  whole-module pattern ``<module>.*`` for that file. ``source_paths`` stays the
  whole package (imports must resolve in the sandbox); narrowing happens at
  selection time, not by editing ``source_paths``.

Clean-baseline safety (fail-closed, exit 2): ``mutmut run`` runs the clean
(unmutated) test suite in its sandbox first and aborts with a nonzero exit +
``Failed to run clean test`` (or ``failed to collect stats``) BEFORE writing
any results if that suite is red. The gate refuses to read that as "0
survivors". :func:`catastrophic_run` detects it two ways — the run exited
nonzero with a baseline-abort signature in its output, OR ``mutmut results``
parses ZERO TOTAL mutants when mutants were expected (distinct from "0
survivors of N", a clean pass) — and :func:`main` returns exit 2 for it. A
missing or unresolvable diff base ref is likewise a :class:`GateFailClosed`
exit 2, never a traceback. This mirrors the 0/1/2 fail-closed convention of the
sibling gates ``scripts/check_policy_lints.py`` / ``scripts/check_schema_gates.py``.

Diff mode treats ``survived``, ``timeout``, and ``suspicious`` as unkilled.
Full mode follows the project score contract: killed / (killed + survived),
excluding no-test, timeout, and suspicious outcomes from the denominator.

Excusing an equivalent mutant: put ``# pragma: no mutate`` on the statement,
with the reason in a comment above it, when every mutant of that line is
equivalent. mutmut 3.6 reads the pragma only as the trailing comment of a
simple statement or of a compound-statement header, and then generates no
mutant for any node that STARTS on that statement's first line — so it cannot
single out one mutant of a line, nor one line inside a multi-line expression.

Allowlist: :data:`ALLOWLIST_PATH` (``tests/mutmut_allowlist.txt``), one mutant
id per line (``#`` comments allowed). Listed ids are subtracted before the
gate decides — the route for an equivalent / integration-only-covered survivor
whose line also carries killable mutants, which a pragma would hide.

Invocation::

    uv run python scripts/mutmut_diff_gate.py           # PR diff-scoped
    uv run python scripts/mutmut_diff_gate.py --full     # nightly, whole core
    uv run python scripts/mutmut_diff_gate.py --results-only --base main
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Mirrors [tool.mutmut].only_mutate.
CORE_FILES: tuple[str, ...] = (
    "setforge/scalar_merge.py",
    "setforge/structural_merge.py",
    "setforge/base_store.py",
    "setforge/base_store_format.py",
    "setforge/scalar_base_store.py",
    "setforge/project_sync.py",
    "setforge/project_record.py",
    "setforge/reconcile_apply.py",
    "setforge/reconcile/merge.py",
)

ALLOWLIST_PATH = REPO_ROOT / "tests" / "mutmut_allowlist.txt"

UNKILLED_STATUSES: frozenset[str] = frozenset({"survived", "timeout", "suspicious"})
KNOWN_STATUSES: frozenset[str] = frozenset(
    {"killed", "survived", "no tests", "timeout", "suspicious", "not checked"}
)
FULL_SCORE_THRESHOLD = 0.80

# --unified=0 gives one hunk per changed region; `+N[,M]` is the new-file start
# + count (M omitted means 1, M==0 means a pure deletion, no new line).
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_DIFF_NEWFILE_RE = re.compile(r"^\+\+\+ b/(.+)$")

# mutmut 3.6 mutant-name grammar: trailing `__mutmut_<N>`, module dotted-path,
# then an `x_`-prefixed function or an `xǁClassǁmethod` method form.
_MUTMUT_SUFFIX_RE = re.compile(r"__mutmut_\d+$")
_METHOD_SEP = "ǁ"  # mutmut's class/method mangling separator
_CORE_MODULES = tuple(path.removesuffix(".py").replace("/", ".") for path in CORE_FILES)
_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
_MUTMUT_NAME_RE = re.compile(
    rf"^(?:{'|'.join(re.escape(module) for module in _CORE_MODULES)})\."
    rf"(?:x_{_IDENTIFIER}|x{_METHOD_SEP}{_IDENTIFIER}{_METHOD_SEP}{_IDENTIFIER})"
    r"__mutmut_\d+$"
)

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
    def local(self) -> str:
        """The mutant-key local part, e.g. ``x_load`` or ``xǁStoreǁload``."""
        return self._local_part(self._stem)

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

    Non-data lines (blanks, unindented banners) yield nothing. Indented
    colon-delimited records must match the pinned mutmut mutant-name grammar
    and known status vocabulary; otherwise the result stream is unusable and
    the gate fails closed. This is the total-mutant view — every status, not
    just the unkilled ones — so callers can tell "0 survivors of N mutants"
    (a clean pass) from "0 mutants parsed" (a baseline abort that wrote no
    results)."""
    out: list[tuple[str, str]] = []
    for raw in results_text.splitlines():
        if not raw[:1].isspace():
            continue
        line = raw.strip()
        if not line:
            continue
        if ": " not in line:
            raise GateFailClosed(
                f"`mutmut results` contained a malformed indented record: {line!r}."
            )
        name, _, status = line.rpartition(": ")
        name = name.strip()
        status = status.strip()
        if not _MUTMUT_NAME_RE.fullmatch(name) or status not in KNOWN_STATUSES:
            raise GateFailClosed(
                f"`mutmut results` contained an unrecognized mutant record: {line!r}."
            )
        out.append((name, status))
    return out


def count_mutants(results_text: str) -> int:
    """Total number of mutants ``mutmut results`` reported, in ANY status.

    Zero means mutmut wrote no usable results (e.g. a clean-baseline abort),
    which is distinct from "0 survivors of N" and is the fail-closed signal."""
    return len(_result_lines(results_text))


def mutation_score(results_text: str, allowlist: set[str]) -> float | None:
    """Return killed / (killed + non-allowlisted survived), if scoreable."""
    killed = 0
    survived = 0
    for name, status in _result_lines(results_text):
        if status == "killed":
            killed += 1
        elif status == "survived" and name not in allowlist:
            survived += 1
    denominator = killed + survived
    return killed / denominator if denominator else None


def parse_results(results_text: str) -> list[Survivor]:
    """Parse ``mutmut results`` stdout into the unkilled :class:`Survivor` set.

    Each data line is ``    <mutant_name>: <status>``. Only the three unkilled
    statuses (:data:`UNKILLED_STATUSES`) are retained."""
    return [
        Survivor(name, status)
        for name, status in _result_lines(results_text)
        if status in UNKILLED_STATUSES
    ]


def _mutant_units(source: str) -> list[tuple[int, int, str]]:
    """``(start, end, local)`` for every unit mutmut mutates: top-level functions
    and methods of top-level classes. ``local`` is the mutant-key local part
    (``x_<function>`` / ``xǁ<Class>ǁ<method>``), so same-named methods of
    different classes stay distinct. A decorator line starts its unit."""
    units: list[tuple[int, int, str]] = []
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            members = [(node, f"x_{node.name}")]
        elif isinstance(node, ast.ClassDef):
            members = [
                (m, f"x{_METHOD_SEP}{node.name}{_METHOD_SEP}{m.name}")
                for m in node.body
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
        else:
            continue
        for member, local in members:
            start = min([member.lineno, *(d.lineno for d in member.decorator_list)])
            units.append((start, member.end_lineno or member.lineno, local))
    return units


def function_spans(source: str) -> dict[str, tuple[int, int]]:
    """Map each mutated unit's mutant-key local name (``x_<function>`` /
    ``xǁ<Class>ǁ<method>``) in ``source`` to its ``(start, end)`` line span
    (1-based, inclusive)."""
    return {local: (start, end) for start, end, local in _mutant_units(source)}


def span_for_mutant(survivor: Survivor, source: str) -> tuple[int, int] | None:
    """The line span of ``survivor``'s function within ``source``, or ``None``
    if that function is not found (e.g. renamed away)."""
    return function_spans(source).get(survivor.local)


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


def scoped_patterns(module: str, source: str, changed: set[int]) -> list[str]:
    """The mutmut selection patterns covering the changed lines of one module.

    mutmut mutates only top-level functions and the methods of top-level
    classes (nested functions are part of their enclosing function's mutants),
    so each changed line maps to one such unit and yields
    ``<module>.x_<function>__mutmut_*`` or ``<module>.xǁ<Class>ǁ<method>__mutmut_*``.
    A changed line outside every unit that is not blank or a comment (module
    statements, class attributes, nested classes) makes the result the single
    whole-module pattern ``<module>.*``."""
    whole = [f"{module}.*"]
    units = _mutant_units(source)
    lines = source.splitlines()
    selected: set[str] = set()
    for line in changed:
        hit = [local for start, end, local in units if start <= line <= end]
        if hit:
            selected.add(hit[0])
        elif 0 < line <= len(lines) and (
            not lines[line - 1].strip() or lines[line - 1].strip().startswith("#")
        ):
            continue
        else:
            return whole
    return [f"{module}.{local}__mutmut_*" for local in sorted(selected)]


def stale_allowlist_entries(results_text: str, allowlist: set[str]) -> list[str]:
    """Allowlisted ids that name no mutant in ``results_text``, for modules the
    results cover. Mutant numbers shift when a function is edited, so an entry
    past the function's current mutant count is certainly stale (an entry that
    shifted but still exists cannot be detected here)."""
    names = {name for name, _ in _result_lines(results_text)}
    covered = {Survivor(name, "").module_dotted for name in names}
    return sorted(
        entry
        for entry in allowlist
        if entry not in names and Survivor(entry, "").module_dotted in covered
    )


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
    """``override`` wins outright; else prefer local ``main`` over a stale
    ``origin/main`` (this project's ``main`` is often unpushed)."""
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
    """The ``git merge-base <base_ref> HEAD`` fork-point sha."""
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
    """Complete ``mutmut results`` stdout. An infra-level failure (nonzero exit) is
    surfaced as a :class:`GateFailClosed` (exit 2), matching :func:`_git_merge_base`
    — never an uncaught ``CalledProcessError`` traceback."""
    proc = _run(["uv", "run", "mutmut", "results", "--all", "true"], check=False)
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
        "\nKill each with a test. An equivalent mutant is excused with "
        "`# pragma: no mutate` on its statement when every mutant of that line "
        "is equivalent; otherwise (or if integration-only-covered) add its id "
        f"+ a reason to {ALLOWLIST_PATH.relative_to(REPO_ROOT)}.",
        file=sys.stderr,
    )


def _warn_stale(results_text: str, allowlist: set[str]) -> None:
    for entry in stale_allowlist_entries(results_text, allowlist):
        print(f"Mutation gate warning: stale allowlist entry {entry}", file=sys.stderr)


def _print_failclosed(reason: str) -> None:
    print(f"Mutation gate FAIL-CLOSED (exit 2): {reason}", file=sys.stderr)


def _run_full(allowlist: set[str]) -> int:
    """Nightly ``--full`` path: score the results the workflow already produced.

    The nightly workflow runs ``mutmut run || true`` itself (the ``|| true``
    swallows mutmut's survivors-present nonzero), so this does NOT re-run the
    whole-engine pass — it only reads + gates the existing results. Because the
    workflow's ``|| true`` hides a baseline-abort exit too, the fail-closed
    protection here rides on the results-side detector: zero total mutants
    parsed (an empty/unusable results file) is treated as catastrophic. The
    score excludes timeout/no-test/suspicious outcomes, matching the governing
    mutation policy and mutmut's standard denominator."""
    results_text = _mutmut_results()
    if catastrophic_run(MutmutRun(0, ""), results_text, expected=True):
        _print_failclosed(
            "mutmut produced no usable mutation results (0 mutants parsed) — "
            "the nightly `mutmut run` likely aborted on a red clean baseline."
        )
        return EXIT_FAILCLOSED
    if any(status == "not checked" for _, status in _result_lines(results_text)):
        _print_failclosed(
            "mutation results contain `not checked` mutants — the whole-core "
            "run did not complete."
        )
        return EXIT_FAILCLOSED
    _warn_stale(results_text, allowlist)
    score = mutation_score(results_text, allowlist)
    if score is None:
        _print_failclosed(
            "mutation results contain no killed/survived score denominator."
        )
        return EXIT_FAILCLOSED
    result_lines = _result_lines(results_text)
    counts = Counter(status for _, status in result_lines)
    killed = counts["killed"]
    survived = sum(
        status == "survived" and name not in allowlist for name, status in result_lines
    )
    allowlisted = counts["survived"] - survived
    message = (
        f"Mutation score: {score:.2%} ({killed} killed / "
        f"{killed + survived} scored; required > {FULL_SCORE_THRESHOLD:.0%}). "
        f"Outcomes: {counts['survived']} survived ({allowlisted} allowlisted), "
        f"{counts['no tests']} no tests, {counts['timeout']} timeout, "
        f"{counts['suspicious']} suspicious."
    )
    print(message, file=sys.stderr if score <= FULL_SCORE_THRESHOLD else sys.stdout)
    return EXIT_CLEAN if score > FULL_SCORE_THRESHOLD else EXIT_BLOCKED


def _run_diff(allowlist: set[str], base_ref: str, *, results_only: bool = False) -> int:
    """PR diff-scoped path: run mutmut over only the changed core modules and
    gate survivors whose function overlaps a changed line. A completed full
    pass can supply the same results without rerunning its mutants."""
    diff_text = _git_diff_core(base_ref)
    changed = changed_lines_from_diff(diff_text)
    core = set(_existing_core_files())
    changed = {p: lines for p, lines in changed.items() if p in core}
    if not changed:
        return EXIT_CLEAN

    sources = _read_sources(set(changed))
    patterns = [
        pattern
        for path in sorted(changed)
        for pattern in scoped_patterns(
            path.removesuffix(".py").replace("/", "."), sources[path], changed[path]
        )
    ]
    if not patterns:
        return EXIT_CLEAN
    run = MutmutRun(0, "") if results_only else _run_mutmut(patterns)

    results_text = _mutmut_results()
    if catastrophic_run(run, results_text, expected=True):
        _print_failclosed(
            "mutmut did not produce usable mutation results for the changed "
            "core modules — a red clean baseline or an aborted run. Refusing "
            "to read this as '0 survivors'."
        )
        return EXIT_FAILCLOSED

    records = _result_lines(results_text)
    if results_only and any(status == "not checked" for _, status in records):
        _print_failclosed(
            "results-only diff requires a complete full-core run on this "
            "checkout; mutation results still contain `not checked` mutants."
        )
        return EXIT_FAILCLOSED
    incomplete = [
        Survivor(name, status) for name, status in records if status == "not checked"
    ]
    if survivors_on_changed_lines(incomplete, changed, sources):
        _print_failclosed(
            "mutation results contain `not checked` mutants in changed "
            "functions — the scoped run did not complete."
        )
        return EXIT_FAILCLOSED
    survivors = parse_results(results_text)
    on_diff = survivors_on_changed_lines(survivors, changed, sources)
    remaining, code = decide(on_diff, allowlist)
    _warn_stale(results_text, allowlist)
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
        "--results-only",
        action="store_true",
        help="score the diff from a complete full-core run on this exact "
        "checkout, without rerunning mutants",
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
    if args.full and args.results_only:
        parser.error("--results-only applies only to diff mode")

    allowlist = read_allowlist()
    try:
        if args.full:
            return _run_full(allowlist)
        return _run_diff(
            allowlist, _resolve_base_ref(args.base), results_only=args.results_only
        )
    except GateFailClosed as exc:
        _print_failclosed(str(exc))
        return EXIT_FAILCLOSED


if __name__ == "__main__":
    sys.exit(main())
