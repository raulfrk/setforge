#!/usr/bin/env python3
"""Fail a changed test that kills no mutant the rest of the suite misses.

testing-standard gate 3. Reads the results of the last ``mutmut run`` (run
``scripts/mutmut_diff_gate.py`` first) from ``mutants/``. For each test in a
changed test file it takes the killed mutants of the functions that test
reaches, and looks for one the test kills while every other test reaching that
function lets it live. A test that reaches no mutated function is outside the
mutation scope and is reported, not failed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from scripts.check_test_stability import changed_test_files, collect

MUTANTS = Path("mutants")
# shortcut: tries at most this many mutants per test; raise it if a test
# with a unique kill is reported as redundant.
MAX_MUTANTS_PER_TEST = 20


def killed_mutants() -> set[str]:
    killed: set[str] = set()
    for meta in MUTANTS.rglob("*.meta"):
        codes = json.loads(meta.read_text(encoding="utf-8"))["exit_code_by_key"]
        # mutmut: 0 survived, None not run, any other code killed it.
        killed |= {name for name, code in codes.items() if code not in (0, None)}
    return killed


def suite_fails(tests: list[str], mutant: str) -> bool:
    env = {**os.environ, "MUTANT_UNDER_TEST": mutant}
    rc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-o",
            "addopts=",
            "-x",
            "-q",
            "-p",
            "no:cacheprovider",
            *tests,
        ],
        cwd=MUTANTS,
        env=env,
        capture_output=True,
    ).returncode
    return rc == 1


def unique_kill(
    test: str, functions: dict[str, list[str]], killed: set[str]
) -> str | None:
    tried = 0
    for function, tests in functions.items():
        if test not in tests:
            continue
        others = [t for t in tests if t != test]
        for mutant in sorted(m for m in killed if m.startswith(f"{function}__mutmut_")):
            if tried == MAX_MUTANTS_PER_TEST:
                return None
            tried += 1
            if suite_fails([test], mutant) and not (
                others and suite_fails(others, mutant)
            ):
                return mutant
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main")
    args = parser.parse_args(argv)
    files = changed_test_files(args.base)
    if not files:
        print("unique-kill: no changed test files")
        return 0
    stats_file = MUTANTS / "mutmut-stats.json"
    if not stats_file.exists():
        print("unique-kill: no mutmut results; run scripts/mutmut_diff_gate.py first")
        return 2
    # shortcut: only test-file edits mark results stale, not source edits;
    # compare against changed setforge/ files too if stale passes bite.
    newest = max(Path(f).stat().st_mtime for f in files)
    if stats_file.stat().st_mtime < newest:
        print(
            "unique-kill: mutmut results are older than the changed tests; rerun mutmut"
        )
        return 2
    functions = json.loads(stats_file.read_text(encoding="utf-8"))[
        "tests_by_mangled_function_name"
    ]
    killed = killed_mutants()
    redundant = []
    for test in collect(files):
        if not any(test in tests for tests in functions.values()):
            print(f"unique-kill: outside mutation scope: {test}")
            continue
        mutant = unique_kill(test, functions, killed)
        if mutant is None:
            redundant.append(test)
        else:
            print(f"unique-kill: {test} alone kills {mutant}")
    for test in redundant:
        print(f"unique-kill: REDUNDANT (merge or drop): {test}")
    return 1 if redundant else 0


if __name__ == "__main__":
    sys.exit(main())
