"""Tests for drift compare and unified-diff rendering."""

import io
from pathlib import Path

from rich.console import Console

from setforge.compare import (
    CompareStatus,
    compare_profile,
    compare_summary_table,
    diff_file,
)
from setforge.config import Config, Profile, TrackedFile


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_diff_file_unchanged(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _write(src, "a\n")
    _write(dst, "a\n")
    assert diff_file(src, dst) == ""


def test_diff_file_basic_drift(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _write(src, "a\nb\n")
    _write(dst, "a\nB\n")
    assert "B" in diff_file(src, dst)


def test_diff_file_missing_dst_is_empty(tmp_path: Path) -> None:
    """diff_file returns '' when the live (dst) file does not exist.

    A missing dst is the MISSING status axis handled by ``compare_profile``;
    ``diff_file`` itself has nothing to diff and short-circuits to ''.
    """
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _write(src, "a\n")
    assert diff_file(src, dst) == ""


def _make_config(profile: Profile, tracked_file: TrackedFile, key: str) -> Config:
    return Config(
        tracked_files={key: tracked_file},
        profiles={"p": profile},
    )


def test_compare_profile_unchanged(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    src = repo / "tracked" / "x"
    _write(src, "same\n")
    dst = tmp_path / "live" / "x"
    _write(dst, "same\n")

    config = _make_config(
        Profile(tracked_files=["x"]),
        TrackedFile(src=Path("x"), dst=str(dst)),
        "x",
    )
    report = compare_profile(config, "p", repo)
    assert len(report.entries) == 1
    assert report.entries[0].status is CompareStatus.UNCHANGED
    assert report.has_unexpected_drift is False


def test_compare_profile_drifted_markdown_unexpected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    src = repo / "tracked" / "x.md"
    _write(src, "tracked\n")
    dst = tmp_path / "live" / "x.md"
    _write(dst, "live\n")

    config = _make_config(
        Profile(tracked_files=["x"]),
        TrackedFile(src=Path("x.md"), dst=str(dst)),
        "x",
    )
    report = compare_profile(config, "p", repo)
    assert report.entries[0].status is CompareStatus.DRIFTED
    assert report.has_unexpected_drift is True


def test_compare_profile_missing_dst(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    src = repo / "tracked" / "x"
    _write(src, "data\n")
    dst = tmp_path / "live" / "x"

    config = _make_config(
        Profile(tracked_files=["x"]),
        TrackedFile(src=Path("x"), dst=str(dst)),
        "x",
    )
    report = compare_profile(config, "p", repo)
    assert report.entries[0].status is CompareStatus.MISSING
    assert report.has_unexpected_drift is True


# ---------------------------------------------------------------------------
# rich summary table + --check / --check --strict exit codes
# ---------------------------------------------------------------------------


def test_compare_summary_table_renders_headers(tmp_path: Path) -> None:
    """compare_summary_table returns a Table whose columns include 'file',
    'expected drift', and 'unexpected drift'."""
    repo = tmp_path / "repo"
    src = repo / "tracked" / "x"
    _write(src, "a\n")
    dst = tmp_path / "live" / "x"
    _write(dst, "a\n")
    config = _make_config(
        Profile(tracked_files=["x"]),
        TrackedFile(src=Path("x"), dst=str(dst)),
        "x",
    )
    report = compare_profile(config, "p", repo)
    table = compare_summary_table(report)
    # Capture via Console to a StringIO
    buf = io.StringIO()
    console = Console(file=buf, highlight=False, markup=False, no_color=True)
    console.print(table)
    output = buf.getvalue()
    assert "file" in output.lower()
    assert "expected" in output.lower()
    assert "unexpected" in output.lower()


def test_compare_summary_table_drifted_row(tmp_path: Path) -> None:
    """A DRIFTED entry appears as a row in the table."""
    repo = tmp_path / "repo"
    src = repo / "tracked" / "x"
    _write(src, "tracked\n")
    dst = tmp_path / "live" / "x"
    _write(dst, "live\n")
    config = _make_config(
        Profile(tracked_files=["x"]),
        TrackedFile(src=Path("x"), dst=str(dst)),
        "x",
    )
    report = compare_profile(config, "p", repo)
    table = compare_summary_table(report)
    buf = io.StringIO()
    console = Console(file=buf, highlight=False, markup=False, no_color=True)
    console.print(table)
    output = buf.getvalue()
    assert "x" in output  # tracked_file name appears as a row


def test_check_flag_clean_exits_0(tmp_path: Path) -> None:
    """--check exits 0 on a clean profile (no drift)."""
    repo = tmp_path / "repo"
    src = repo / "tracked" / "x"
    _write(src, "same\n")
    dst = tmp_path / "live" / "x"
    _write(dst, "same\n")
    config = _make_config(
        Profile(tracked_files=["x"]),
        TrackedFile(src=Path("x"), dst=str(dst)),
        "x",
    )
    report = compare_profile(config, "p", repo)
    assert not report.has_unexpected_drift
    # also assert no DRIFTED entries
    assert all(e.status != CompareStatus.DRIFTED for e in report.entries)


def test_check_flag_unexpected_drift_exits_1(tmp_path: Path) -> None:
    """--check: has_unexpected_drift True when unexpected drift present."""
    repo = tmp_path / "repo"
    src = repo / "tracked" / "x"
    _write(src, "tracked\n")
    dst = tmp_path / "live" / "x"
    _write(dst, "live\n")
    config = _make_config(
        Profile(tracked_files=["x"]),
        TrackedFile(src=Path("x"), dst=str(dst)),
        "x",
    )
    report = compare_profile(config, "p", repo)
    assert report.has_unexpected_drift


def test_check_strict_drifted_is_drifted(tmp_path: Path) -> None:
    """--check --strict: any DRIFTED entry is treated as 'has_any_drift'."""
    repo = tmp_path / "repo"
    src = repo / "tracked" / "x"
    _write(src, "tracked\n")
    dst = tmp_path / "live" / "x"
    _write(dst, "live\n")
    config = _make_config(
        Profile(tracked_files=["x"]),
        TrackedFile(src=Path("x"), dst=str(dst)),
        "x",
    )
    report = compare_profile(config, "p", repo)
    has_any_drift = any(e.status == CompareStatus.DRIFTED for e in report.entries)
    assert has_any_drift


def test_check_strict_clean_is_not_drifted(tmp_path: Path) -> None:
    """--check --strict: clean profile has no DRIFTED entries."""
    repo = tmp_path / "repo"
    src = repo / "tracked" / "x"
    _write(src, "same\n")
    dst = tmp_path / "live" / "x"
    _write(dst, "same\n")
    config = _make_config(
        Profile(tracked_files=["x"]),
        TrackedFile(src=Path("x"), dst=str(dst)),
        "x",
    )
    report = compare_profile(config, "p", repo)
    has_any_drift = any(e.status == CompareStatus.DRIFTED for e in report.entries)
    assert not has_any_drift


def test_cli_compare_check_exits_0_no_drift(tmp_path: Path) -> None:
    """CLI compare --check exits 0 on clean profile."""
    from typer.testing import CliRunner

    from setforge.cli import app

    repo = tmp_path / "repo"
    src = repo / "tracked" / "x"
    _write(src, "same\n")
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
        app, ["compare", "--profile=p", f"--config={cfg_path}", "--check"]
    )
    assert result.exit_code == 0


def test_cli_compare_check_exits_1_unexpected_drift(tmp_path: Path) -> None:
    """CLI compare --check exits 1 when unexpected drift exists."""
    from typer.testing import CliRunner

    from setforge.cli import app

    repo = tmp_path / "repo"
    src = repo / "tracked" / "x"
    _write(src, "tracked\n")
    dst = tmp_path / "live" / "x"
    _write(dst, "live\n")
    cfg_path = repo / "setforge.yaml"
    cfg_path.write_text(
        f"version: 1\ntracked_files:\n  x:\n    src: x\n    dst: {dst}\n"
        "profiles:\n  p:\n    tracked_files: [x]\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(
        app, ["compare", "--profile=p", f"--config={cfg_path}", "--check"]
    )
    assert result.exit_code == 1


def test_cli_compare_check_strict_exits_1_drift(tmp_path: Path) -> None:
    """CLI compare --check --strict exits 1 on any drift."""
    from typer.testing import CliRunner

    from setforge.cli import app

    repo = tmp_path / "repo"
    src = repo / "tracked" / "x"
    _write(src, "tracked\n")
    dst = tmp_path / "live" / "x"
    _write(dst, "live\n")
    cfg_path = repo / "setforge.yaml"
    cfg_path.write_text(
        f"version: 1\ntracked_files:\n  x:\n    src: x\n    dst: {dst}\n"
        "profiles:\n  p:\n    tracked_files: [x]\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(
        app, ["compare", "--profile=p", f"--config={cfg_path}", "--check", "--strict"]
    )
    assert result.exit_code == 1


def test_cli_compare_check_strict_exits_0_clean(tmp_path: Path) -> None:
    """CLI compare --check --strict exits 0 on a clean profile."""
    from typer.testing import CliRunner

    from setforge.cli import app

    repo = tmp_path / "repo"
    src = repo / "tracked" / "x"
    _write(src, "same\n")
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
        app, ["compare", "--profile=p", f"--config={cfg_path}", "--check", "--strict"]
    )
    assert result.exit_code == 0


def test_cli_compare_strict_without_check_is_usage_error(tmp_path: Path) -> None:
    """compare --strict without --check is a usage error (non-zero, no compare).

    --strict alone used to parse fine and change nothing → exit 0, so a
    user believing they had CI gating did not. The guard now raises a
    usage error before any comparison runs.
    """
    from typer.testing import CliRunner

    from setforge.cli import app

    repo = tmp_path / "repo"
    src = repo / "tracked" / "x"
    _write(src, "same\n")
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
        app, ["compare", "--profile=p", f"--config={cfg_path}", "--strict"]
    )
    assert result.exit_code != 0
    assert "--strict requires --check" in result.stderr


def test_cli_compare_strict_with_check_unchanged(tmp_path: Path) -> None:
    """compare --strict --check keeps existing behavior: clean profile exits 0.

    The new guard only rejects --strict alone; --strict --check must still
    run the comparison and not raise a usage error.
    """
    from typer.testing import CliRunner

    from setforge.cli import app

    repo = tmp_path / "repo"
    src = repo / "tracked" / "x"
    _write(src, "same\n")
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
        app, ["compare", "--profile=p", f"--config={cfg_path}", "--strict", "--check"]
    )
    assert result.exit_code == 0
    assert "--strict requires --check" not in result.output


def test_cli_compare_full_diff_includes_markers(tmp_path: Path) -> None:
    """CLI compare --full-diff includes +++ / --- diff markers."""
    from typer.testing import CliRunner

    from setforge.cli import app

    repo = tmp_path / "repo"
    src = repo / "tracked" / "x"
    _write(src, "tracked\n")
    dst = tmp_path / "live" / "x"
    _write(dst, "live\n")
    cfg_path = repo / "setforge.yaml"
    cfg_path.write_text(
        f"version: 1\ntracked_files:\n  x:\n    src: x\n    dst: {dst}\n"
        "profiles:\n  p:\n    tracked_files: [x]\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(
        app, ["compare", "--profile=p", f"--config={cfg_path}", "--full-diff"]
    )
    assert result.exit_code == 0
    assert "+++" in result.stdout or "---" in result.stdout
