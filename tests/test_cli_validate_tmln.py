"""Tests for ``setforge validate`` local.yaml error UX (setforge-tmln).

Covers three layers per SPEC 9 / mockup D:

1. ``setforge._levenshtein.levenshtein`` — pure-function distance smoke
   (boundary cases: equal, empty-side, substitution).
2. ``setforge.cli._validate_errors`` — formatter + close-match helper
   behavior, including the Levenshtein > 2 hard-gate that suppresses
   "Did you mean".
3. ``setforge.cli.validate`` integration — local.yaml schema errors
   surface via ``format_schema_validation_error`` with file:line +
   snippet + pointer + "Fix:" + report-all (no abort-on-first); YAML
   parse errors surface via ``format_yaml_parse_error`` in the
   "✗ YAML PARSE ERROR" category. ANSI is suppressed when stdout is
   not a TTY (typer-test CliRunner capture path).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge._levenshtein import levenshtein
from setforge.cli import app
from setforge.cli._validate_errors import (
    format_schema_validation_error,
    format_yaml_parse_error,
    suggest_close_match,
)

# ---------------------------------------------------------------------------
# Levenshtein smoke (the same boundary cases captured in the acceptance
# commands; duplicated here for clean pytest discovery + traceback).
# ---------------------------------------------------------------------------


def test_levenshtein_equal_returns_zero() -> None:
    """Identical strings → distance 0 (fast-path branch)."""
    assert levenshtein("foo", "foo") == 0


def test_levenshtein_empty_first_returns_len_other() -> None:
    """Empty left side → distance is the right side's length."""
    assert levenshtein("", "xyz") == 3


def test_levenshtein_empty_second_returns_len_other() -> None:
    """Empty right side → distance is the left side's length."""
    assert levenshtein("xyz", "") == 3


def test_levenshtein_single_substitution() -> None:
    """One-character substitution → distance 1."""
    assert levenshtein("abc", "abd") == 1


def test_levenshtein_insertion() -> None:
    """One-character insertion → distance 1."""
    assert levenshtein("ab", "abc") == 1


def test_levenshtein_swaps_to_shorter_first() -> None:
    """Internal swap branch (``len(a) > len(b)``) returns the same distance
    as the un-swapped call — symmetry sanity check."""
    assert levenshtein("abcdef", "abc") == levenshtein("abc", "abcdef") == 3


def test_levenshtein_unicode_codepoint_iteration() -> None:
    """Distance counts code points, not bytes — non-ASCII is one unit."""
    assert levenshtein("café", "cafe") == 1


# ---------------------------------------------------------------------------
# suggest_close_match — the explicit Levenshtein ≤ 2 hard-gate over the
# difflib pre-filter.
# ---------------------------------------------------------------------------


def test_suggest_close_match_distance_one_returns_candidate() -> None:
    """Distance-1 typo → suggestion returned."""
    assert (
        suggest_close_match("work-internl", ["work-internal", "unrelated"])
        == "work-internal"
    )


def test_suggest_close_match_distance_two_returns_candidate() -> None:
    """Distance-2 typo → suggestion returned (boundary inclusive)."""
    # 'wrk-internal' → 'work-internal' is distance 2 (insert 'o' + 'r' moves
    # — actually 1 insertion). Use a tighter example: drop two letters.
    # 'wok-intenal' vs 'work-internal' is distance 2 (insert 'r' + 'r').
    assert (
        suggest_close_match("wok-intenal", ["work-internal", "unrelated"])
        == "work-internal"
    )


def test_suggest_close_match_distance_three_returns_none() -> None:
    """Distance-3 typo → no suggestion (anti-smell: must NOT surface as
    "did you mean" when distance > 2; difflib's permissive cutoff is
    hard-gated by Levenshtein)."""
    # 'xyz' → 'work-internal' is distance 13; far above the gate.
    assert suggest_close_match("xyz", ["work-internal"]) is None


def test_suggest_close_match_difflib_pre_filter_distance_three_returns_none() -> None:
    """Edge case: difflib MAY return a candidate (cutoff=0.5) at edit
    distance 3 for short words. The explicit Levenshtein guard must
    reject it.

    'abcd' → 'abxx' has ratio ≥ 0.5 (difflib accepts) but edit distance
    is 2 (substitute 2). Use a case where difflib accepts but Levenshtein
    > 2: 'abcdef' → 'abcxyz' has ratio = 0.5 (accepted) and distance 3.
    """
    # SequenceMatcher ratio of 'abcdef' vs 'abcxyz' is 0.5; Levenshtein is 3.
    assert suggest_close_match("abcdef", ["abcxyz"]) is None


def test_suggest_close_match_empty_candidates_returns_none() -> None:
    """No candidates → no suggestion (degenerate input)."""
    assert suggest_close_match("anything", []) is None


def test_suggest_close_match_picks_closest_when_multiple_under_gate() -> None:
    """When several candidates fall under the gate, the difflib pre-filter
    orders by similarity ratio and we return the first one whose
    Levenshtein ≤ max_distance — i.e. the closest match."""
    got = suggest_close_match(
        "work-internl",
        ["work-internal", "work-external", "irrelevant"],
    )
    assert got == "work-internal"


def test_suggest_close_match_custom_max_distance() -> None:
    """``max_distance`` is configurable; the default is 2."""
    # 'foobar' vs 'foobaz' is distance 1, accepted at default.
    assert suggest_close_match("foobar", ["foobaz"]) == "foobaz"
    # With max_distance=0, only exact matches survive.
    assert suggest_close_match("foobar", ["foobaz"], max_distance=0) is None


# ---------------------------------------------------------------------------
# format_yaml_parse_error — the "✗ YAML PARSE ERROR" category.
# ---------------------------------------------------------------------------


def test_format_yaml_parse_error_includes_file_line_msg() -> None:
    """Parse-error line must carry the parser category, file:line:col, and msg."""
    out = format_yaml_parse_error(
        Path("/home/u/.config/setforge/local.yaml"), 7, 3, "found character '\\t'"
    )
    assert "✗ YAML PARSE ERROR" in out
    assert "local.yaml:7:3" in out
    assert "found character" in out


def test_format_yaml_parse_error_no_snippet() -> None:
    """Anti-smell guard: parse errors must NOT render a snippet (the file
    may be unsafe to slice — the parser failed at a structural level)."""
    out = format_yaml_parse_error(
        Path("/x/local.yaml"), 1, 1, "mapping values are not allowed here"
    )
    assert "←───" not in out
    assert "^^^^" not in out


# ---------------------------------------------------------------------------
# format_schema_validation_error — the multi-line mockup-D shape.
# ---------------------------------------------------------------------------


def test_format_schema_validation_error_shape_contains_pointer_and_underline() -> None:
    """Output carries the literal ``←───`` line marker and ``^^^^`` underline
    per mockup D."""
    out = format_schema_validation_error(
        path=Path("/home/u/.config/setforge/local.yaml"),
        line=14,
        col=31,
        snippet_lines=[
            "plugins:",
            "  add:",
            "    - secure-code-review@work-internl",
        ],
        field_value="work-internl",
        fix_hint=("edit ~/.config/setforge/local.yaml:14 — typo in marketplace name"),
        suggestion="work-internal",
    )
    assert "←─── line 14" in out
    assert "^" in out  # underline marker
    assert "Did you mean 'work-internal'" in out
    assert "Fix:" in out
    assert "local.yaml:14" in out


def test_format_schema_validation_error_omits_did_you_mean_without_suggestion() -> None:
    """When ``suggestion`` is None, the "Did you mean" line is omitted."""
    out = format_schema_validation_error(
        path=Path("/x/local.yaml"),
        line=1,
        col=1,
        snippet_lines=["foo: bar"],
        field_value="bar",
        fix_hint="edit /x/local.yaml:1 — invalid value",
        suggestion=None,
    )
    assert "Did you mean" not in out
    assert "Fix:" in out


def test_format_schema_validation_error_underline_aligns_with_value() -> None:
    """``^^^^^`` count equals the field value's length."""
    out = format_schema_validation_error(
        path=Path("/x/local.yaml"),
        line=2,
        col=1,
        snippet_lines=["key: hello"],
        field_value="hello",
        fix_hint="edit /x/local.yaml:2 — replace value",
    )
    # The underline line has exactly len('hello') = 5 carets.
    assert "^^^^^" in out
    # And not 6+ (must not over-underline).
    assert "^^^^^^" not in out


# ---------------------------------------------------------------------------
# validate CLI integration — local.yaml errors flow through the formatters.
# ---------------------------------------------------------------------------

_CLEAN_YAML = """\
version: 1
tracked_files:
  d:
    src: tracked_file.txt
    dst: ~/.some-tracked_file
profiles:
  p:
    tracked_files: [d]
"""


def _write_minimal_config(tmp_path: Path) -> Path:
    """Write a minimal valid setforge.yaml + dummy tracked source."""
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(_CLEAN_YAML, encoding="utf-8")
    (tmp_path / "tracked").mkdir(exist_ok=True)
    (tmp_path / "tracked" / "tracked_file.txt").write_text("x\n", encoding="utf-8")
    return cfg


@pytest.fixture
def local_yaml_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point setforge's LOCAL_CONFIG_PATH at a per-test path."""
    local = tmp_path / "local.yaml"
    monkeypatch.setattr("setforge.cli.validate._LOCAL_CONFIG_PATH", local)
    return local


def test_validate_local_yaml_absent_exits_zero(
    tmp_path: Path, local_yaml_at: Path
) -> None:
    """Anti-smell guard: an absent local.yaml is valid; validate exits 0."""
    cfg = _write_minimal_config(tmp_path)
    assert not local_yaml_at.exists()
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    assert "ok" in result.output


def test_validate_local_yaml_empty_exits_zero(
    tmp_path: Path, local_yaml_at: Path
) -> None:
    """Empty local.yaml is valid; validate exits 0."""
    cfg = _write_minimal_config(tmp_path)
    local_yaml_at.write_text("", encoding="utf-8")
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 0, result.output


def test_validate_local_yaml_parse_error_uses_parse_category(
    tmp_path: Path, local_yaml_at: Path
) -> None:
    """A malformed local.yaml (unparseable) surfaces in the YAML PARSE
    category, NOT the schema category."""
    cfg = _write_minimal_config(tmp_path)
    # Tab-indented mapping is a YAML parse error.
    local_yaml_at.write_text("source:\n\tpath: /tmp\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    assert "✗ YAML PARSE ERROR" in result.output
    assert "local.yaml" in result.output
    assert "validation FAILED" in result.output


def test_validate_local_yaml_unknown_top_level_key_surfaces_schema_error(
    tmp_path: Path, local_yaml_at: Path
) -> None:
    """Unknown top-level key triggers the schema-error path (extra="forbid")
    with snippet + pointer + Fix; not collapsed into parse-error."""
    cfg = _write_minimal_config(tmp_path)
    local_yaml_at.write_text(
        "binaries:\n  uv: /usr/bin/uv\nunknown_key: oops\n", encoding="utf-8"
    )
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    assert "✗ SCHEMA VALIDATION ERROR" in result.output
    assert "←─── line" in result.output
    assert "Fix:" in result.output
    assert "validation FAILED" in result.output


def test_validate_local_yaml_close_match_suggests_known_key(
    tmp_path: Path, local_yaml_at: Path
) -> None:
    """Typo'd top-level key within Levenshtein ≤ 2 of a known key triggers
    a "Did you mean" line."""
    cfg = _write_minimal_config(tmp_path)
    # 'binares' is distance 2 from 'binaries' (insert 'i' + a swap).
    local_yaml_at.write_text("binares:\n  uv: /usr/bin/uv\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    assert "Did you mean 'binaries'" in result.output


def test_validate_local_yaml_no_close_match_omits_did_you_mean(
    tmp_path: Path, local_yaml_at: Path
) -> None:
    """Typo with no candidate inside the Levenshtein ≤ 2 gate must NOT
    show a "Did you mean" line (anti-smell: no false-positive
    suggestions)."""
    cfg = _write_minimal_config(tmp_path)
    local_yaml_at.write_text("thoroughly_unrelated_xyz: oops\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    assert "Did you mean" not in result.output


def test_validate_local_yaml_multiple_errors_all_reported(
    tmp_path: Path, local_yaml_at: Path
) -> None:
    """All schema errors are collected and reported together (no abort
    on first); final summary names the count."""
    cfg = _write_minimal_config(tmp_path)
    local_yaml_at.write_text("unknown_a: 1\nunknown_b: 2\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    # Two distinct schema errors land in the output.
    assert result.output.count("✗ SCHEMA VALIDATION ERROR") >= 2
    # Final summary names the count.
    assert "validation FAILED:" in result.output
    assert "errors" in result.output


def test_validate_local_yaml_no_ansi_when_not_tty(
    tmp_path: Path, local_yaml_at: Path
) -> None:
    """When stdout is not a TTY (CliRunner capture), no ANSI escapes leak
    into the output."""
    cfg = _write_minimal_config(tmp_path)
    local_yaml_at.write_text("unknown_x: 1\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    # No ANSI escape sequences in the captured output.
    assert re.search(r"\x1b\[", result.output) is None
