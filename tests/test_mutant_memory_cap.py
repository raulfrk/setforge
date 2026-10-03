"""Resource limits of mutation-test workers (memory cap, temp retention)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_PROBE = """
import resource
import sys
from tests import conftest

conftest.pytest_sessionstart(None)
soft, _ = resource.getrlimit(resource.RLIMIT_AS)
if soft == resource.RLIM_INFINITY:
    sys.exit(10)
try:
    bytearray(3 << 30)
except MemoryError:
    sys.exit(11)
sys.exit(12)
"""


def _probe(mutant: str | None) -> int:
    env = {k: v for k, v in os.environ.items() if k != "MUTANT_UNDER_TEST"}
    if mutant is not None:
        env["MUTANT_UNDER_TEST"] = mutant
    return subprocess.run(
        [sys.executable, "-c", _PROBE], cwd=_REPO, env=env, check=False
    ).returncode


@pytest.mark.skipif(not Path("/proc/self/statm").exists(), reason="needs Linux /proc")
def test_mutant_worker_cannot_allocate_without_bound() -> None:
    assert _probe("setforge.structural_merge.x_split_key_path__mutmut_41") == 11


@pytest.mark.parametrize("mutant", [None, "", "stats", "fail"])
def test_ordinary_and_bookkeeping_sessions_are_not_capped(mutant: str | None) -> None:
    assert _probe(mutant) == 10
