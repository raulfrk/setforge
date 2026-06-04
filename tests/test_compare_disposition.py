"""Tests for disposition-aware drift reporting in compare.

Covers:
- shared/forked/pinned disposition threading through compare_profile → FileCompare
- drift_is_expected classification (forked/pinned = expected; shared = not expected)
- has_unexpected_drift is suppressed for forked/pinned drifted files
- --json output includes disposition string (or null) and drift_is_expected flag
- Regression: None-disposition files are unaffected (existing fields intact)
"""

import io
import json
from pathlib import Path

from rich.console import Console

from setforge.cli.compare import _compare_json_data
from setforge.compare import (
    CompareStatus,
    compare_profile,
    compare_summary_table,
)
from setforge.config import Config, Disposition, Profile, TrackedFile


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_config(
    tracked_file: TrackedFile,
    key: str = "x",
    profile_name: str = "p",
) -> Config:
    return Config(
        tracked_files={key: tracked_file},
        profiles={profile_name: Profile(tracked_files=[key])},
    )


# ---------------------------------------------------------------------------
# 1. shared file with live != tracked → DRIFTED, disposition SHARED, NOT expected
# ---------------------------------------------------------------------------


def test_shared_drifted_not_expected(tmp_path: Path) -> None:
    """shared file with drift: disposition=SHARED, drift_is_expected=False, DRIFTED."""
    repo = tmp_path / "repo"
    src = repo / "tracked" / "x"
    _write(src, "tracked content\n")
    dst = tmp_path / "live" / "x"
    _write(dst, "live content\n")

    config = _make_config(
        TrackedFile.model_validate(
            {"src": "x", "dst": str(dst), "disposition": "shared"}
        )
    )
    report = compare_profile(config, "p", repo)
    entry = report.entries[0]

    assert entry.disposition is Disposition.SHARED
    assert entry.status is CompareStatus.DRIFTED
    assert entry.drift_is_expected is False
    # shared drift needs attention → treated as unexpected for has_unexpected_drift
    assert report.has_unexpected_drift is True


# ---------------------------------------------------------------------------
# 2. forked file with live != tracked → DRIFTED, disposition FORKED, EXPECTED
# ---------------------------------------------------------------------------


def test_forked_drifted_is_expected(tmp_path: Path) -> None:
    """forked file with drift: disposition=FORKED, drift_is_expected=True, DRIFTED."""
    repo = tmp_path / "repo"
    src = repo / "tracked" / "x"
    _write(src, "tracked content\n")
    dst = tmp_path / "live" / "x"
    _write(dst, "live content\n")

    config = _make_config(
        TrackedFile.model_validate(
            {"src": "x", "dst": str(dst), "disposition": "forked"}
        )
    )
    report = compare_profile(config, "p", repo)
    entry = report.entries[0]

    assert entry.disposition is Disposition.FORKED
    assert entry.status is CompareStatus.DRIFTED
    assert entry.drift_is_expected is True
    # forked drift is expected → does NOT count as unexpected_drift
    assert report.has_unexpected_drift is False


# ---------------------------------------------------------------------------
# 3. pinned file with live != tracked → DRIFTED, disposition PINNED, EXPECTED
# ---------------------------------------------------------------------------


def test_pinned_drifted_is_expected(tmp_path: Path) -> None:
    """pinned file with drift: disposition=PINNED, drift_is_expected=True, DRIFTED."""
    repo = tmp_path / "repo"
    src = repo / "tracked" / "x"
    _write(src, "tracked content\n")
    dst = tmp_path / "live" / "x"
    _write(dst, "live content\n")

    config = _make_config(
        TrackedFile.model_validate(
            {"src": "x", "dst": str(dst), "disposition": "pinned"}
        )
    )
    report = compare_profile(config, "p", repo)
    entry = report.entries[0]

    assert entry.disposition is Disposition.PINNED
    assert entry.status is CompareStatus.DRIFTED
    assert entry.drift_is_expected is True
    assert report.has_unexpected_drift is False


# ---------------------------------------------------------------------------
# 4. shared file with live == tracked → UNCHANGED, disposition SHARED, no drift
# ---------------------------------------------------------------------------


def test_shared_unchanged(tmp_path: Path) -> None:
    """shared file, no drift: UNCHANGED, disposition=SHARED, drift_is_expected=False."""
    repo = tmp_path / "repo"
    src = repo / "tracked" / "x"
    _write(src, "same content\n")
    dst = tmp_path / "live" / "x"
    _write(dst, "same content\n")

    config = _make_config(
        TrackedFile.model_validate(
            {"src": "x", "dst": str(dst), "disposition": "shared"}
        )
    )
    report = compare_profile(config, "p", repo)
    entry = report.entries[0]

    assert entry.disposition is Disposition.SHARED
    assert entry.status is CompareStatus.UNCHANGED
    # No drift → drift_is_expected is False (no drift to classify as expected)
    assert entry.drift_is_expected is False
    assert report.has_unexpected_drift is False


# ---------------------------------------------------------------------------
# 5. --json output includes disposition (string or null) and drift_is_expected
# ---------------------------------------------------------------------------


def test_json_includes_disposition_string(tmp_path: Path) -> None:
    """_compare_json_data includes disposition string per entry."""
    repo = tmp_path / "repo"
    _write(repo / "tracked" / "shared_file", "tracked\n")
    _write(tmp_path / "live" / "shared_file", "live\n")

    config = _make_config(
        TrackedFile.model_validate(
            {
                "src": "shared_file",
                "dst": str(tmp_path / "live" / "shared_file"),
                "disposition": "shared",
            }
        ),
        key="shared_file",
    )
    report = compare_profile(config, "p", repo)
    data = _compare_json_data(report)

    entry_json = data["entries"][0]
    assert entry_json["disposition"] == "shared"
    assert entry_json["drift_is_expected"] is False


def test_json_forked_disposition_expected(tmp_path: Path) -> None:
    """_compare_json_data: forked drifted file has drift_is_expected=True in JSON."""
    repo = tmp_path / "repo"
    _write(repo / "tracked" / "x", "tracked\n")
    _write(tmp_path / "live" / "x", "live\n")

    config = _make_config(
        TrackedFile.model_validate(
            {"src": "x", "dst": str(tmp_path / "live" / "x"), "disposition": "forked"}
        )
    )
    report = compare_profile(config, "p", repo)
    data = _compare_json_data(report)

    entry_json = data["entries"][0]
    assert entry_json["disposition"] == "forked"
    assert entry_json["drift_is_expected"] is True


def test_json_null_disposition_for_non_disposition_file(tmp_path: Path) -> None:
    """_compare_json_data: None-disposition file serializes disposition: null."""
    repo = tmp_path / "repo"
    _write(repo / "tracked" / "x", "same\n")
    _write(tmp_path / "live" / "x", "same\n")

    config = _make_config(TrackedFile(src=Path("x"), dst=str(tmp_path / "live" / "x")))
    report = compare_profile(config, "p", repo)
    data = _compare_json_data(report)

    entry_json = data["entries"][0]
    assert entry_json["disposition"] is None
    # drift_is_expected is False when there is no disposition and no drift
    assert entry_json["drift_is_expected"] is False


# ---------------------------------------------------------------------------
# 6. Regression: None-disposition file unchanged by this change
# ---------------------------------------------------------------------------


def test_regression_no_disposition_drifted(tmp_path: Path) -> None:
    """REGRESSION: plain drifted file (no disposition) still works as before.

    has_unexpected_drift stays True, expected/unexpected_drift_keys intact,
    disposition is None, drift_is_expected is False.
    """
    repo = tmp_path / "repo"
    src = repo / "tracked" / "x.yaml"
    _write(src, "a: 1\nb: 2\n")
    dst = tmp_path / "live" / "x.yaml"
    _write(dst, "a: 99\nb: 2\n")

    config = _make_config(
        TrackedFile.model_validate(
            {"src": "x.yaml", "dst": str(dst), "preserve_user_keys": ["a"]}
        )
    )
    report = compare_profile(config, "p", repo)
    entry = report.entries[0]

    assert entry.disposition is None
    assert entry.status is CompareStatus.DRIFTED
    assert entry.expected_drift_keys == ["a"]
    assert entry.unexpected_drift_keys == []
    assert entry.drift_is_expected is False
    assert report.has_unexpected_drift is False


def test_regression_no_disposition_unexpected_drift(tmp_path: Path) -> None:
    """REGRESSION: None-disposition file with unexpected drift unchanged."""
    repo = tmp_path / "repo"
    src = repo / "tracked" / "x"
    _write(src, "tracked\n")
    dst = tmp_path / "live" / "x"
    _write(dst, "live\n")

    config = _make_config(TrackedFile(src=Path("x"), dst=str(dst)))
    report = compare_profile(config, "p", repo)
    entry = report.entries[0]

    assert entry.disposition is None
    assert entry.status is CompareStatus.DRIFTED
    assert entry.drift_is_expected is False
    assert report.has_unexpected_drift is True


def test_regression_json_fields_intact_for_preserve_file(tmp_path: Path) -> None:
    """REGRESSION: JSON output for a preserve_user_keys file has all original fields."""
    repo = tmp_path / "repo"
    _write(repo / "tracked" / "x.yaml", "a: 1\nb: 2\n")
    dst = tmp_path / "live" / "x.yaml"
    _write(dst, "a: 99\nb: 88\n")

    config = _make_config(
        TrackedFile.model_validate(
            {"src": "x.yaml", "dst": str(dst), "preserve_user_keys": ["a"]}
        )
    )
    report = compare_profile(config, "p", repo)
    data = _compare_json_data(report)

    entry_json = data["entries"][0]
    # Original fields still present
    assert "name" in entry_json
    assert "status" in entry_json
    assert "expected_drift_keys" in entry_json
    assert "unexpected_drift_keys" in entry_json
    # New fields with defaults
    assert entry_json["disposition"] is None
    assert entry_json["drift_is_expected"] is False


# ---------------------------------------------------------------------------
# Text renderer: disposition tag shows in summary table
# ---------------------------------------------------------------------------


def test_compare_summary_table_shows_disposition_tag(tmp_path: Path) -> None:
    """compare_summary_table includes disposition tag for each disposition value."""
    repo = tmp_path / "repo"
    _write(repo / "tracked" / "x", "tracked\n")
    dst = tmp_path / "live" / "x"
    _write(dst, "live\n")

    for disposition in ("shared", "forked", "pinned"):
        config = _make_config(
            TrackedFile.model_validate(
                {"src": "x", "dst": str(dst), "disposition": disposition}
            )
        )
        report = compare_profile(config, "p", repo)
        table = compare_summary_table(report)
        buf = io.StringIO()
        console = Console(file=buf, highlight=False, markup=False, no_color=True)
        console.print(table)
        output = buf.getvalue()
        assert f"[{disposition}]" in output, (
            f"Expected '[{disposition}]' tag in table output, got:\n{output}"
        )


def test_compare_summary_table_forked_shows_expected_note(tmp_path: Path) -> None:
    """compare_summary_table shows 'expected' indicator for forked/pinned drift."""
    repo = tmp_path / "repo"
    _write(repo / "tracked" / "x", "tracked\n")
    dst = tmp_path / "live" / "x"
    _write(dst, "live\n")

    config = _make_config(
        TrackedFile.model_validate(
            {"src": "x", "dst": str(dst), "disposition": "forked"}
        )
    )
    report = compare_profile(config, "p", repo)
    table = compare_summary_table(report)
    buf = io.StringIO()
    console = Console(file=buf, highlight=False, markup=False, no_color=True)
    console.print(table)
    output = buf.getvalue()
    # The table should indicate this drift is expected
    assert "expected" in output.lower()


# ---------------------------------------------------------------------------
# CLI integration: --format=json includes disposition fields
# ---------------------------------------------------------------------------


def test_cli_compare_json_disposition_field(tmp_path: Path) -> None:
    """compare --format=json output includes disposition and drift_is_expected."""
    from typer.testing import CliRunner

    from setforge.cli import app

    repo = tmp_path / "repo"
    _write(repo / "tracked" / "x", "tracked\n")
    dst = tmp_path / "live" / "x"
    _write(dst, "live\n")
    cfg_path = repo / "setforge.yaml"
    cfg_path.write_text(
        f"version: 1\ntracked_files:\n  x:\n    src: x\n    dst: {dst}\n"
        "    disposition: forked\nprofiles:\n  p:\n    tracked_files: [x]\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--format=json", "compare", "--profile=p", f"--config={cfg_path}"],
    )
    assert result.exit_code == 0, result.output
    outer = json.loads(result.stdout)
    entries = outer["data"]["entries"]
    assert len(entries) == 1
    assert entries[0]["disposition"] == "forked"
    assert entries[0]["drift_is_expected"] is True


def test_cli_compare_json_null_disposition(tmp_path: Path) -> None:
    """compare --format=json: None-disposition file has disposition=null."""
    from typer.testing import CliRunner

    from setforge.cli import app

    repo = tmp_path / "repo"
    _write(repo / "tracked" / "x", "same\n")
    dst = tmp_path / "live" / "x"
    _write(dst, "same\n")
    cfg_path = repo / "setforge.yaml"
    cfg_path.write_text(
        f"version: 1\ntracked_files:\n  x:\n    src: x\n    dst: {dst}\n"
        "profiles:\n  p:\n    tracked_files: [x]\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--format=json", "compare", "--profile=p", f"--config={cfg_path}"],
    )
    assert result.exit_code == 0, result.output
    outer = json.loads(result.stdout)
    entries = outer["data"]["entries"]
    assert len(entries) == 1
    assert entries[0]["disposition"] is None
    assert entries[0]["drift_is_expected"] is False
