"""mutmut_fast: which lines a mutant changes, which tests it keeps, and that
the warm session gives the plain path's exit codes (end to end, a few seconds)."""

import json
import os
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import mutmut
import pytest

from scripts import mutmut_fast

SANDBOX = """\
def x_f__mutmut_orig(a, b=1):
    total = a + b
    if (
        total > 3
        and a
    ):
        return total
    return 0
def x_f__mutmut_1(a, b=1):
    total = a - b
    if (
        total > 3
        and a
    ):
        return total
    return 0
def x_f__mutmut_2(a, b=1):
    total = a + b
    if (
        total > 3
        or a
    ):
        return total
    return 0
def x_f__mutmut_3(a, b=2):
    total = a + b
    if (
        total > 3
        and a
    ):
        return total
    return 0
"""


@pytest.mark.parametrize(
    ("mutant", "lines"),
    [
        ("x_f__mutmut_1", {2}),  # a simple statement maps to its own line
        ("x_f__mutmut_2", {3, 4, 5, 6}),  # a multi-line condition: its whole header
        ("x_f__mutmut_3", None),  # a signature change keeps every test
    ],
)
def test_changed_lines(mutant: str, lines: set[int] | None) -> None:
    assert mutmut_fast.changed_lines(SANDBOX)[mutant] == lines


@pytest.mark.parametrize("layout", ["pkg/a.py", "src/pkg/a.py"])
def test_a_glob_selected_mutant_runs_only_the_covered_tests_reaching_its_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str
) -> None:
    sandbox = tmp_path / "mutants" / layout
    sandbox.parent.mkdir(parents=True)
    sandbox.write_text(SANDBOX + "# mutmut generated\n")
    monkeypatch.chdir(tmp_path)
    funcs = defaultdict(set, {"pkg.a.x_f": {"t::fast", "t::slow"}})
    monkeypatch.setattr(mutmut, "tests_by_mangled_function_name", funcs)
    seen: list[list[str]] = []

    def fake_coverage(
        runner: object, files: list[Path], tests: list[str]
    ) -> dict[str, dict[int, set[str]]]:
        seen.append(tests)
        return {str(files[0]): {2: {"t::slow"}}}

    monkeypatch.setattr(mutmut_fast, "_line_coverage", fake_coverage)
    monkeypatch.setattr(mutmut_fast, "_selected", {})

    mutmut_fast._narrow(None, ["pkg.a.x_f__mutmut_*"])  # type: ignore[arg-type]

    assert seen == [["t::fast", "t::slow"]]
    assert mutmut_fast._selected["pkg.a.x_f__mutmut_1"] == ["t::slow"]
    assert "pkg.a.x_f__mutmut_3" not in mutmut_fast._selected


def test_likely_killers_put_last_runs_killer_then_sibling_kills_by_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kills = tmp_path / "kills"
    kills.mkdir()
    for sibling, test in [("2", "t::b"), ("3", "t::c"), ("4", "t::c")]:
        (kills / f"pkg.a.x_f__mutmut_{sibling}").write_text(test)
    (kills / "pkg.a.x_g__mutmut_1").write_text("t::other_function")
    monkeypatch.setattr(mutmut_fast, "_KILLS", kills)
    monkeypatch.setattr(mutmut_fast, "_killers", {"pkg.a.x_f__mutmut_1": "t::a"})

    assert mutmut_fast._likely_killers("pkg.a.x_f__mutmut_1") == [
        "t::a",
        "t::c",
        "t::b",
    ]


def test_a_trampoline_hit_after_a_test_changes_directory_is_still_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Config:
        max_stack_depth = -1
        source_paths = (Path("does-not-exist-here"),)

    monkeypatch.setattr(mutmut_fast.mm.Config, "get", staticmethod(lambda: Config))
    monkeypatch.setattr(mutmut, "_stats", set())
    monkeypatch.chdir(tmp_path)

    mutmut_fast._record_trampoline_hit("pkg.a.x_f")

    assert mutmut._stats == {"pkg.a.x_f"}


def test_a_process_started_during_the_stats_run_does_not_record_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess
    import sys

    monkeypatch.setenv("MUTANT_UNDER_TEST", "stats")
    monkeypatch.setattr(subprocess.Popen, "__init__", mutmut_fast._popen_without_stats)
    show = [sys.executable, "-c", "import os; print(os.environ['MUTANT_UNDER_TEST'])"]

    inherited = subprocess.run(show, capture_output=True, text=True, check=True)
    explicit = subprocess.run(
        show,
        env={"MUTANT_UNDER_TEST": "stats"},
        capture_output=True,
        text=True,
        check=True,
    )

    assert inherited.stdout == explicit.stdout == "\n"


@pytest.fixture
def prep_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A mutated sandbox file plus mutmut's stats file, on commit "c1"."""
    (tmp_path / "mutants/pkg").mkdir(parents=True)
    (tmp_path / "mutants/pkg/a.py").write_text(SANDBOX + "# mutmut generated\n")
    (tmp_path / "mutants/mutmut-stats.json").write_text('{"stats": 1}')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mutmut_fast, "_commit", lambda: "c1")
    monkeypatch.setattr(mutmut_fast, "_prep_lines", {})
    monkeypatch.setattr(mutmut_fast, "_fresh_stats", [])
    monkeypatch.setenv("MUTMUT_FAST_PREP", str(tmp_path / "prep"))
    monkeypatch.setenv("PY_IGNORE_IMPORTMISMATCH", "")
    return tmp_path


COV = {"mutants/pkg/a.py": {2: {"t::slow", "t::fast"}, 3: {"t::slow"}, 7: set()}}


def test_prep_loads_on_the_same_commit_and_sandbox_and_skips_the_clean_run(
    prep_sandbox: Path,
) -> None:
    mutmut_fast._write_prep(prep_sandbox / "prep", COV)
    (prep_sandbox / "mutants/mutmut-stats.json").unlink()

    assert mutmut_fast._load_prep()

    assert mutmut_fast._prep_lines == {
        "mutants/pkg/a.py": {2: {"t::slow", "t::fast"}, 3: {"t::slow"}}
    }
    assert (prep_sandbox / "mutants/mutmut-stats.json").read_text() == '{"stats": 1}'
    assert mutmut_fast._fresh_stats == [True]


@pytest.mark.parametrize("change", ["commit", "sandbox"])
def test_prep_from_another_commit_or_sandbox_is_ignored(
    prep_sandbox: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    mutmut_fast._write_prep(prep_sandbox / "prep", COV)
    (prep_sandbox / "mutants/mutmut-stats.json").unlink()
    if change == "commit":
        monkeypatch.setattr(mutmut_fast, "_commit", lambda: "c2")
    else:
        with (prep_sandbox / "mutants/pkg/a.py").open("a") as f:
            f.write("\n")

    assert not mutmut_fast._load_prep()

    assert mutmut_fast._prep_lines == {}
    assert not (prep_sandbox / "mutants/mutmut-stats.json").exists()
    assert mutmut_fast._fresh_stats == []


def test_narrow_uses_the_prep_line_map_instead_of_a_coverage_run(
    prep_sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    funcs = defaultdict(set, {"pkg.a.x_f": {"t::fast", "t::slow"}})
    monkeypatch.setattr(mutmut, "tests_by_mangled_function_name", funcs)
    monkeypatch.setattr(mutmut_fast, "_selected", {})
    monkeypatch.setattr(
        mutmut_fast, "_prep_lines", {"mutants/pkg/a.py": {2: {"t::slow"}}}
    )
    monkeypatch.setattr(mutmut_fast, "_line_coverage", None)  # must not run

    mutmut_fast._narrow(None, ["pkg.a.x_f__mutmut_1"])  # type: ignore[arg-type]

    assert mutmut_fast._selected == {"pkg.a.x_f__mutmut_1": ["t::slow"]}


FAST = Path(mutmut_fast.__file__)

PROJECT = {
    "pyproject.toml": """\
[tool.mutmut]
source_paths = ["pkg"]
pytest_add_cli_args = ["-o", "addopts="]
""",
    "pkg/__init__.py": "",
    "pkg/calc.py": """\
def add_one(x):
    return x + 1


def crash_guard(x):
    return x > 0
""",
    "tests/__init__.py": "",
    "tests/test_calc.py": """\
import os
import signal

from pkg.calc import add_one, crash_guard


def test_add_one():
    assert add_one(1) == 2


def test_a_wrong_guard_kills_the_process():
    if not crash_guard(1):
        os.kill(os.getpid(), signal.SIGSEGV)
""",
}


def _codes(root: Path, session: str) -> dict[str, int]:
    shutil.rmtree(root / "mutants", ignore_errors=True)
    env = {**os.environ, "MUTMUT_FAST_SESSION": session, "PYTHONDONTWRITEBYTECODE": "1"}
    subprocess.run(
        [sys.executable, str(FAST), "run", "--max-children", "2"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    codes: dict[str, int] = {}
    for meta in (root / "mutants").rglob("*.meta"):
        codes.update(json.loads(meta.read_text())["exit_code_by_key"])
    return codes


@pytest.mark.skipif(sys.platform != "linux", reason="fork server is Linux-only")
def test_warm_and_plain_paths_give_the_same_exit_codes(tmp_path: Path) -> None:
    for rel, text in PROJECT.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)

    plain, warm = _codes(tmp_path, "0"), _codes(tmp_path, "1")
    served = (tmp_path / "mutants/.mutmut-fast-ready.json").exists()

    assert plain, "mutmut produced no results"
    assert served, "the warm run fell back to pytest.main"
    assert warm == plain
    assert -11 in plain.values()  # a mutant whose test dies from SIGSEGV
