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

Clean-baseline safety: ``mutmut run`` runs the clean (unmutated) test suite in
its sandbox first and aborts with a nonzero exit + ``Failed to run clean test``
(or ``failed to collect stats``) if that suite is red. This gate treats a
mutmut-run failure that is NOT the expected survivors-present nonzero as a
fail-closed exit 2 — a red sandbox suite can never be read as "0 survivors".

Statuses collected as survivors: ``survived``, ``timeout``, ``suspicious``
(NOT ``survived`` alone — a timeout or suspicious mutant is unkilled too, and
NOT the aggregate ``export-cicd-stats`` counts, which lose the mutant ids).

Allowlist: :data:`ALLOWLIST_PATH` (``tests/mutmut_allowlist.txt``), one mutant
id per line (``#`` comments allowed). Listed ids are subtracted before the
gate decides — the route for equivalent / integration-only-covered survivors.

Invocation::

    uv run python scripts/mutmut_diff_gate.py           # PR diff-scoped
    uv run python scripts/mutmut_diff_gate.py --full     # nightly, whole core
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

# The merge/reconcile/store core — the mutation scope (mirrors
# ``[tool.mutmut].only_mutate``). Files listed here that are absent on disk are
# skipped: the mutmut config predates the disposition/sections retirement, so
# the gate must tolerate a stale-but-larger nominal scope and act on what exists.
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

# The three unkilled statuses. mutmut prints `    <name>: <status>` from
# `mutmut results`; a mutant is unkilled unless it is `killed` / `no tests` /
# `caught by type check` / `not checked` etc.
UNKILLED_STATUSES: frozenset[str] = frozenset({"survived", "timeout", "suspicious"})

# `@@ -old +new @@` unified-diff hunk header. With --unified=0 each changed
# region is its own hunk; the `+N[,M]` side gives the NEW-file start line and
# (optional) count. M defaults to 1; M == 0 means a pure deletion (no new line).
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_DIFF_NEWFILE_RE = re.compile(r"^\+\+\+ b/(.+)$")

# mutmut mutant-name grammar (3.6): trailing `__mutmut_<N>`, a leading module
# dotted-path, and an `x_`-prefixed function OR an `xǁClassǁmethod` method form.
_MUTMUT_SUFFIX_RE = re.compile(r"__mutmut_\d+$")
_METHOD_SEP = "ǁ"  # ǁ — mutmut's class/method mangling separator


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
        # The local part starts at the first `x_` / `xǁ` segment; everything
        # before the last dot preceding it is the module.
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
            # xǁClassǁmethod -> take the final ǁ-delimited segment.
            return local.rsplit(_METHOD_SEP, 1)[-1]
        # x_funcname -> drop the single leading `x_`.
        return local[2:] if local.startswith("x_") else local

    @staticmethod
    def _local_part(stem: str) -> str:
        """The mutant-local part of ``stem`` (after the module dotted-path).

        mutmut prefixes the function/method with ``x_`` / ``xǁ``; the module
        dotted-path never contains ``ǁ`` and never has an ``x_``-prefixed final
        segment, so the local part begins at the last ``.x`` boundary."""
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
    # Drop files that ended up with no added lines (deletion-only).
    return {path: lines for path, lines in changed.items() if lines}


def parse_results(results_text: str) -> list[Survivor]:
    """Parse ``mutmut results`` stdout into the unkilled :class:`Survivor` set.

    Each data line is ``    <mutant_name>: <status>``. Only the three unkilled
    statuses (:data:`UNKILLED_STATUSES`) are retained."""
    out: list[Survivor] = []
    for raw in results_text.splitlines():
        line = raw.strip()
        if ": " not in line:
            continue
        name, _, status = line.rpartition(": ")
        name = name.strip()
        status = status.strip()
        if status in UNKILLED_STATUSES and name:
            out.append(Survivor(name, status))
    return out


def function_spans(source: str) -> dict[str, tuple[int, int]]:
    """Map every function/method name in ``source`` to its ``(start, end)``
    line span (1-based, inclusive) via AST.

    Nested/duplicate names collapse to the LAST definition seen; mutmut targets
    a function by bare name, and the core modules do not reuse names across
    scopes, so this is unambiguous for the gate's purpose."""
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
        # Strip an inline `# reason` comment, then the whole-line case.
        line = raw.split("#", 1)[0].strip()
        if line:
            out.add(line)
    return out


def decide(
    survivors: list[Survivor], allowlist: set[str]
) -> tuple[list[Survivor], int]:
    """Subtract the allowlist and pick the exit code: 1 if any survivor remains,
    else 0. Returns ``(remaining, exit_code)``."""
    remaining = [s for s in survivors if s.name not in allowlist]
    return remaining, (1 if remaining else 0)


# --- git / mutmut driving (the impure edge) -----------------------------------


def _run(cmd: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=check
    )


def _existing_core_files() -> list[str]:
    """The core files that actually exist on disk (tolerating stale config)."""
    return [f for f in CORE_FILES if (REPO_ROOT / f).exists()]


def _git_diff_core() -> str:
    """``git diff --unified=0 <merge-base>...HEAD -- <core files>`` text.

    Three-dot against an explicit ``git merge-base origin/main HEAD`` — never a
    two-dot ``..HEAD`` (which would diff against origin/main's tip, not the
    fork point) — so the changed set is exactly what this branch introduced."""
    base = _run(["git", "merge-base", "origin/main", "HEAD"], check=True).stdout.strip()
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


def _run_mutmut(patterns: list[str] | None) -> None:
    """Run ``mutmut run`` (optionally scoped to ``patterns``).

    mutmut exits nonzero when survivors remain — that is EXPECTED and not a
    gate failure, so its exit code is intentionally ignored here (the gate reads
    survivors from ``mutmut results`` instead). But a clean-baseline abort prints
    a diagnostic and produces NO results; that surfaces downstream as an empty /
    unusable results parse, and :func:`_collect_survivors` fail-closes on it."""
    cmd = ["uv", "run", "mutmut", "run", *(patterns or [])]
    _run(cmd, check=False)


def _mutmut_results() -> str:
    return _run(["uv", "run", "mutmut", "results"], check=True).stdout


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full",
        action="store_true",
        help="gate on every non-allowlisted survivor across the whole core "
        "(nightly), skipping the PR-diff line filter",
    )
    args = parser.parse_args(argv)

    allowlist = read_allowlist()

    if args.full:
        # Nightly: the caller runs `mutmut run` over the whole core first; here
        # we just read + gate the results. (Running it here too is harmless but
        # the nightly workflow already did it, so we only re-run if no results.)
        _run_mutmut(None)
        survivors = parse_results(_mutmut_results())
        remaining, code = decide(survivors, allowlist)
        if remaining:
            _print_block(remaining)
        return code

    # PR diff-scoped path.
    diff_text = _git_diff_core()
    changed = changed_lines_from_diff(diff_text)
    # Restrict to the core files that exist (defence against a stale nominal scope).
    core = set(_existing_core_files())
    changed = {p: lines for p, lines in changed.items() if p in core}
    if not changed:
        # No core line changed by this PR — fast no-op.
        return 0

    # Scope mutmut to exactly the changed core modules via fnmatch globs.
    patterns = [p[: -len(".py")].replace("/", ".") + ".*" for p in sorted(changed)]
    _run_mutmut(patterns)

    survivors = parse_results(_mutmut_results())
    sources = _read_sources(set(changed))
    on_diff = survivors_on_changed_lines(survivors, changed, sources)
    remaining, code = decide(on_diff, allowlist)
    if remaining:
        _print_block(remaining)
    return code


if __name__ == "__main__":
    sys.exit(main())
