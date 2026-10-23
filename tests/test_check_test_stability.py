"""The stability gate fails an order-dependent new test and passes a stable one."""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_test_stability.py"

ORDER_DEPENDENT = """\
STATE = []


def test_a_sets():
    STATE.append(1)


def test_b_needs_a():
    assert STATE
"""

STABLE = """\
def test_one():
    assert 1 + 1 == 2


def test_two():
    assert "a".upper() == "A"
"""


NO_TESTS = "HELPER = 1\n"


@pytest.mark.parametrize(
    ("body", "expected_rc", "expected_out"),
    [
        (ORDER_DEPENDENT, 1, "run 1/5 failed"),
        (STABLE, 0, "2 tests passed 5 shuffled runs"),
        # an empty selection must not fall through to the whole suite
        (NO_TESTS, 0, "no selected tests"),
    ],
)
def test_gate_flags_only_order_dependent_tests(
    tmp_path: Path, body: str, expected_rc: int, expected_out: str
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "base",
        ],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_new.py").write_text(body, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "HEAD"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert (result.returncode, expected_out in result.stdout) == (expected_rc, True)
