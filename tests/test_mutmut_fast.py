"""mutmut_fast: which lines a mutant changes, and which tests it keeps."""

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
