#!/usr/bin/env python3
"""Run the broad local unit/integration lane with bounded parallelism."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence

FAST_MARKER = "not e2e_docker and not test_infra and not slow"
MAX_WORKERS = 6
DEFAULT_WORKERS = min(MAX_WORKERS, os.cpu_count() or 1)


def reserved_pytest_arg(arg: str) -> bool:
    """Return whether an extra argument could override the lane boundary."""
    return (
        arg == "--"
        or arg == "-d"
        or arg.startswith("-m")
        or arg.startswith("-n")
        or arg.startswith("--dist")
        or arg.startswith("--tx")
        or arg.startswith("--numprocesses")
        or arg.startswith("--cov")
        or arg == "--no-cov"
    )


def pytest_argv(*, workers: int, extra: Sequence[str]) -> list[str]:
    """Build the explicit argv so Docker's separate xdist policy is untouched."""
    return [
        sys.executable,
        "-m",
        "pytest",
        "-n",
        str(workers),
        "--dist=worksteal",
        "--no-cov",
        "-m",
        FAST_MARKER,
        *extra,
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-n",
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"xdist workers (default: {DEFAULT_WORKERS})",
    )
    args, pytest_args = parser.parse_known_args(argv)
    if not 1 <= args.workers <= MAX_WORKERS:
        parser.error(f"workers must be between 1 and {MAX_WORKERS}")
    if reserved := next((arg for arg in pytest_args if reserved_pytest_arg(arg)), None):
        parser.error(f"pytest option {reserved!r} would override the fast lane")

    env = dict(os.environ)
    env["HYPOTHESIS_PROFILE"] = "parallel"
    completed = subprocess.run(
        pytest_argv(workers=args.workers, extra=pytest_args),
        env=env,
        check=False,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
