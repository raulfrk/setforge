"""The unique-kill gate keeps a test only when it alone kills some mutant."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts import check_unique_kill as gate

FUNC = "setforge.m.x_f"
M1, M2 = f"{FUNC}__mutmut_1", f"{FUNC}__mutmut_2"


@pytest.mark.parametrize(
    ("kills", "expected"),
    [
        # new test alone kills M2; the others only kill M1
        ({"new": {M1, M2}, "old": {M1}}, M2),
        # every mutant the new test kills, the others kill too
        ({"new": {M1}, "old": {M1, M2}}, None),
        # the new test is the only test reaching the function
        ({"new": {M1}}, M1),
    ],
)
def test_unique_kill_finds_a_mutant_only_the_test_kills(
    monkeypatch: pytest.MonkeyPatch, kills: dict[str, set[str]], expected: str | None
) -> None:
    def fake_suite_fails(tests: list[str], mutant: str) -> bool:
        return any(mutant in kills[t] for t in tests)

    monkeypatch.setattr(gate, "suite_fails", fake_suite_fails)
    functions = {FUNC: list(kills)}

    assert gate.unique_kill("new", functions, {M1, M2}) == expected


@pytest.mark.parametrize(
    ("stats", "expected_rc", "expected_out"),
    [
        (None, 2, "no mutmut results"),
        ("stale", 2, "older than the changed tests"),
        ("fresh", 0, "outside mutation scope: tests/test_new.py::test_new"),
    ],
)
def test_main_fails_closed_without_fresh_results_and_reports_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stats: str | None,
    expected_rc: int,
    expected_out: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], check=True)
    git_id = ["-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(
        ["git", *git_id, "commit", "-q", "--allow-empty", "-m", "b"], check=True
    )
    test_file = tmp_path / "tests" / "test_new.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_new():\n    assert True\n", encoding="utf-8")
    if stats:
        stats_file = tmp_path / "mutants" / "mutmut-stats.json"
        stats_file.parent.mkdir()
        stats_file.write_text(
            json.dumps(
                {"tests_by_mangled_function_name": {FUNC: ["tests/other.py::t"]}}
            ),
            encoding="utf-8",
        )
        age = -60 if stats == "stale" else 60
        stamp = test_file.stat().st_mtime + age
        os.utime(stats_file, (stamp, stamp))

    rc = gate.main(["--base", "HEAD"])

    assert (rc, expected_out in capsys.readouterr().out) == (expected_rc, True)
