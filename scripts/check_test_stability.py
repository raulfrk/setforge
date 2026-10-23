#!/usr/bin/env python3
"""Run the tests in changed test files 5 times: reversed, then 4 random orders.

Catches order-dependent and flaky tests before they land (testing-standard
gate 4). Compares against ``origin/main`` unless ``--base`` says otherwise.
"""

from __future__ import annotations

import argparse
import random
import subprocess
import sys

RUNS = 5


def changed_test_files(base: str) -> list[str]:
    out = subprocess.run(
        [
            "git",
            "diff",
            "--name-only",
            "--diff-filter=AM",
            f"{base}...HEAD",
            "--",
            "tests/",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    # Working-tree edits and new files count too, so it also works pre-commit.
    out += subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=AM", "HEAD", "--", "tests/"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    out += subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "--", "tests/"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return sorted(
        {
            f
            for f in out
            if f.rsplit("/", 1)[-1].startswith("test_") and f.endswith(".py")
        }
    )


def collect(files: list[str]) -> list[str]:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "--no-cov",
            "-p",
            "no:cacheprovider",
            *files,
        ],
        capture_output=True,
        text=True,
    )
    # 5 = nothing selected (e.g. only Docker tests changed); anything else broke.
    if result.returncode not in (0, 5):
        sys.exit(f"stability: collection failed:\n{result.stdout}{result.stderr}")
    return [line for line in result.stdout.splitlines() if "::" in line]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main")
    args = parser.parse_args(argv)
    files = changed_test_files(args.base)
    if not files:
        print("stability: no changed test files")
        return 0
    ids = collect(files)
    if not ids:
        print("stability: no selected tests")
        return 0
    for run in range(1, RUNS + 1):
        seed = random.randrange(2**32)
        order = ids[::-1]
        # Run 1 is the exact reverse, so any test leaning on an earlier one fails.
        if run > 1:
            random.Random(seed).shuffle(order)
        # pytest runs node ids in argv order; -n 0 keeps that order.
        rc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-n",
                "0",
                "--no-cov",
                "-p",
                "no:cacheprovider",
                *order,
            ],
        ).returncode
        if rc != 0:
            print(f"stability: run {run}/{RUNS} failed (shuffle seed {seed})")
            return 1
    print(f"stability: {len(ids)} tests passed {RUNS} shuffled runs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
