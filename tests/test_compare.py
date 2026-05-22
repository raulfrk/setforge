"""Tests for drift compare and YAML drift classification."""

import io
from pathlib import Path

from rich.console import Console

from setforge.compare import (
    CompareStatus,
    classify_yaml_drift,
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


def test_diff_file_preserves_user_sections(tmp_path: Path) -> None:
    src = tmp_path / "src.md"
    dst = tmp_path / "dst.md"
    # Both end markers carry the same (placeholder) hash so the
    # ``strip_section_content`` template comparison treats them as
    # byte-identical — the splice path then renders an empty diff
    # because preserve_user_sections substitutes live body into tracked.
    same_hash = "a" * 64
    _write(
        src,
        "<!-- setforge:user-section start host-local -->\n"
        f"<!-- setforge:user-section end host-local hash={same_hash} -->\n",
    )
    _write(
        dst,
        "<!-- setforge:user-section start host-local -->\n"
        "live content\n"
        f"<!-- setforge:user-section end host-local hash={same_hash} -->\n",
    )
    assert diff_file(src, dst, preserve_user_sections=True) == ""


def test_diff_file_hash_fast_path_returns_empty(tmp_path: Path) -> None:
    """When section bodies hash-match AND non-section content is identical,
    diff_file short-circuits to '' via the hash_sections fast path
    (setforge-xyw)."""
    src = tmp_path / "src.md"
    dst = tmp_path / "dst.md"
    same = (
        "shared header\n"
        "<!-- setforge:user-section start host-local s -->\n"
        "same body\n"
        f"<!-- setforge:user-section end host-local s hash={'a' * 64} -->\n"
        "shared footer\n"
    )
    _write(src, same)
    _write(dst, same)
    assert diff_file(src, dst, preserve_user_sections=True) == ""


def test_diff_file_hash_fast_path_falls_through_on_section_drift(
    tmp_path: Path,
) -> None:
    """When section bodies differ but the template matches, the fast path
    declines (hashes mismatch) and the splice+diff path runs — yielding
    '' because preserve_user_sections substitutes live into tracked."""
    src = tmp_path / "src.md"
    dst = tmp_path / "dst.md"
    same_hash = "a" * 64
    _write(
        src,
        "header\n"
        "<!-- setforge:user-section start host-local s -->\n"
        "tracked body\n"
        f"<!-- setforge:user-section end host-local s hash={same_hash} -->\n"
        "footer\n",
    )
    _write(
        dst,
        "header\n"
        "<!-- setforge:user-section start host-local s -->\n"
        "live body\n"
        f"<!-- setforge:user-section end host-local s hash={same_hash} -->\n"
        "footer\n",
    )
    # preserve_user_sections=True splices live body into the tracked template
    # before diffing, so the diff comes out empty even though bodies differ.
    assert diff_file(src, dst, preserve_user_sections=True) == ""


def test_diff_file_hash_fast_path_declines_on_template_drift(
    tmp_path: Path,
) -> None:
    """When section bodies match but template text differs, the fast path
    declines and the diff surfaces the template drift."""
    src = tmp_path / "src.md"
    dst = tmp_path / "dst.md"
    same_hash = "a" * 64
    _write(
        src,
        "tracked header\n"
        "<!-- setforge:user-section start host-local s -->\n"
        "shared body\n"
        f"<!-- setforge:user-section end host-local s hash={same_hash} -->\n",
    )
    _write(
        dst,
        "live header\n"
        "<!-- setforge:user-section start host-local s -->\n"
        "shared body\n"
        f"<!-- setforge:user-section end host-local s hash={same_hash} -->\n",
    )
    diff = diff_file(src, dst, preserve_user_sections=True)
    assert "tracked header" in diff
    assert "live header" in diff


def test_diff_file_yaml_keys_preserved_no_drift(tmp_path: Path) -> None:
    src = tmp_path / "src.yaml"
    dst = tmp_path / "dst.yaml"
    _write(src, "a: 1\nb: 2\n")
    _write(dst, "a: 99\nb: 2\n")
    assert diff_file(src, dst, preserve_user_keys=["a"]) == ""


def test_diff_file_yaml_keys_unexpected_drift(tmp_path: Path) -> None:
    src = tmp_path / "src.yaml"
    dst = tmp_path / "dst.yaml"
    _write(src, "a: 1\nb: 2\n")
    _write(dst, "a: 99\nb: 88\n")
    diff = diff_file(src, dst, preserve_user_keys=["a"])
    assert "b: 2" in diff or "b: 88" in diff


def test_classify_yaml_drift_all_expected(tmp_path: Path) -> None:
    src = tmp_path / "src.yaml"
    dst = tmp_path / "dst.yaml"
    _write(src, "a: 1\nb: 2\n")
    _write(dst, "a: 99\nb: 2\n")
    expected, unexpected = classify_yaml_drift(src, dst, ["a"])
    assert expected == ["a"]
    assert unexpected == []


def test_classify_yaml_drift_mixed(tmp_path: Path) -> None:
    src = tmp_path / "src.yaml"
    dst = tmp_path / "dst.yaml"
    _write(src, "a: 1\nb:\n  c: 2\n  d: 3\n")
    _write(dst, "a: 99\nb:\n  c: 88\n  d: 3\n")
    expected, unexpected = classify_yaml_drift(src, dst, ["a"])
    assert expected == ["a"]
    assert unexpected == ["b.c"]


def test_classify_yaml_drift_subtree_preserve(tmp_path: Path) -> None:
    src = tmp_path / "src.yaml"
    dst = tmp_path / "dst.yaml"
    _write(src, "settings:\n  theme: dark\n  font: mono\n")
    _write(dst, "settings:\n  theme: light\n  font: sans\n")
    expected, unexpected = classify_yaml_drift(src, dst, ["settings"])
    assert set(expected) == {"settings.theme", "settings.font"}
    assert unexpected == []


def test_classify_yaml_drift_list_each(tmp_path: Path) -> None:
    src = tmp_path / "src.yaml"
    dst = tmp_path / "dst.yaml"
    _write(src, "items:\n  - a\n  - b\n")
    _write(dst, "items:\n  - X\n  - Y\n")
    expected, unexpected = classify_yaml_drift(src, dst, ["items[*]"])
    assert set(expected) == {"items[0]", "items[1]"}
    assert unexpected == []


def test_classify_yaml_drift_list_whole(tmp_path: Path) -> None:
    src = tmp_path / "src.yaml"
    dst = tmp_path / "dst.yaml"
    _write(src, "items:\n  - a\n")
    _write(dst, "items:\n  - X\n  - Y\n")
    expected, unexpected = classify_yaml_drift(src, dst, ["items[]"])
    assert "items[0]" in expected
    assert "items[1]" in expected
    assert unexpected == []


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


def test_compare_profile_yaml_all_expected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    src = repo / "tracked" / "x.yaml"
    _write(src, "a: 1\nb: 2\n")
    dst = tmp_path / "live" / "x.yaml"
    _write(dst, "a: 99\nb: 2\n")

    config = _make_config(
        Profile(tracked_files=["x"]),
        TrackedFile.model_validate(
            {"src": "x.yaml", "dst": str(dst), "preserve_user_keys": ["a"]}
        ),
        "x",
    )
    report = compare_profile(config, "p", repo)
    entry = report.entries[0]
    assert entry.status is CompareStatus.DRIFTED
    assert entry.expected_drift_keys == ["a"]
    assert entry.unexpected_drift_keys == []
    assert report.has_unexpected_drift is False


def test_compare_profile_yaml_mixed_drift(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    src = repo / "tracked" / "x.yaml"
    _write(src, "a: 1\nb: 2\n")
    dst = tmp_path / "live" / "x.yaml"
    _write(dst, "a: 99\nb: 88\n")

    config = _make_config(
        Profile(tracked_files=["x"]),
        TrackedFile.model_validate(
            {"src": "x.yaml", "dst": str(dst), "preserve_user_keys": ["a"]}
        ),
        "x",
    )
    report = compare_profile(config, "p", repo)
    entry = report.entries[0]
    assert entry.status is CompareStatus.DRIFTED
    assert entry.expected_drift_keys == ["a"]
    assert entry.unexpected_drift_keys == ["b"]
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
# P4.1 — rich summary table + --check / --check --strict exit codes
# ---------------------------------------------------------------------------


def _make_config_with_yaml(
    tmp_path: Path, src_text: str, dst_text: str, preserve: list[str]
) -> tuple[Config, Path]:
    """Helper: write src + dst files, return (Config, repo_root)."""
    repo = tmp_path / "repo"
    src = repo / "tracked" / "x.yaml"
    _write(src, src_text)
    dst = tmp_path / "live" / "x.yaml"
    _write(dst, dst_text)
    config = _make_config(
        Profile(tracked_files=["x"]),
        TrackedFile.model_validate(
            {"src": "x.yaml", "dst": str(dst), "preserve_user_keys": preserve}
        ),
        "x",
    )
    return config, repo


def test_compare_summary_table_renders_headers(tmp_path: Path) -> None:
    """compare_summary_table returns a Table whose columns include 'file',
    'expected drift', and 'unexpected drift'."""
    repo = tmp_path / "repo"
    src = repo / "tracked" / "x.yaml"
    _write(src, "a: 1\n")
    dst = tmp_path / "live" / "x.yaml"
    _write(dst, "a: 1\n")
    config = _make_config(
        Profile(tracked_files=["x"]),
        TrackedFile(src=Path("x.yaml"), dst=str(dst)),
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
    """A DRIFTED entry with unexpected drift appears in the table."""
    config, repo = _make_config_with_yaml(
        tmp_path, "a: 1\nb: 2\n", "a: 99\nb: 88\n", ["a"]
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


def test_check_flag_all_expected_drift_exits_0(tmp_path: Path) -> None:
    """--check: when drift sits in preserve_user_keys, has_unexpected_drift is False."""
    config, repo = _make_config_with_yaml(
        tmp_path, "a: 1\nb: 2\n", "a: 99\nb: 2\n", ["a"]
    )
    report = compare_profile(config, "p", repo)
    assert not report.has_unexpected_drift


def test_check_flag_unexpected_drift_exits_1(tmp_path: Path) -> None:
    """--check: has_unexpected_drift True when unexpected drift present."""
    config, repo = _make_config_with_yaml(
        tmp_path, "a: 1\nb: 2\n", "a: 99\nb: 88\n", ["a"]
    )
    report = compare_profile(config, "p", repo)
    assert report.has_unexpected_drift


def test_check_strict_all_expected_is_drifted(tmp_path: Path) -> None:
    """--check --strict: all-expected drift still triggers 'has_any_drift'."""
    config, repo = _make_config_with_yaml(
        tmp_path, "a: 1\nb: 2\n", "a: 99\nb: 2\n", ["a"]
    )
    report = compare_profile(config, "p", repo)
    # For strict mode: any DRIFTED entry should be treated as failing
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
    src = repo / "tracked" / "x.yaml"
    _write(src, "a: 1\nb: 2\n")
    dst = tmp_path / "live" / "x.yaml"
    _write(dst, "a: 99\nb: 88\n")
    cfg_path = repo / "setforge.yaml"
    cfg_path.write_text(
        f"version: 1\ntracked_files:\n  x:\n    src: x.yaml\n    dst: {dst}\n"
        f"    preserve_user_keys: [a]\nprofiles:\n  p:\n    tracked_files: [x]\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(
        app, ["compare", "--profile=p", f"--config={cfg_path}", "--check"]
    )
    assert result.exit_code == 1


def test_cli_compare_check_exits_0_all_expected_drift(tmp_path: Path) -> None:
    """CLI compare --check exits 0 when all drift is expected (preserve_user_keys)."""
    from typer.testing import CliRunner

    from setforge.cli import app

    repo = tmp_path / "repo"
    src = repo / "tracked" / "x.yaml"
    _write(src, "a: 1\nb: 2\n")
    dst = tmp_path / "live" / "x.yaml"
    _write(dst, "a: 99\nb: 2\n")
    cfg_path = repo / "setforge.yaml"
    cfg_path.write_text(
        f"version: 1\ntracked_files:\n  x:\n    src: x.yaml\n    dst: {dst}\n"
        f"    preserve_user_keys: [a]\nprofiles:\n  p:\n    tracked_files: [x]\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(
        app, ["compare", "--profile=p", f"--config={cfg_path}", "--check"]
    )
    assert result.exit_code == 0


def test_cli_compare_check_strict_exits_1_expected_drift(tmp_path: Path) -> None:
    """CLI compare --check --strict exits 1 on expected drift."""
    from typer.testing import CliRunner

    from setforge.cli import app

    repo = tmp_path / "repo"
    src = repo / "tracked" / "x.yaml"
    _write(src, "a: 1\nb: 2\n")
    dst = tmp_path / "live" / "x.yaml"
    _write(dst, "a: 99\nb: 2\n")
    cfg_path = repo / "setforge.yaml"
    cfg_path.write_text(
        f"version: 1\ntracked_files:\n  x:\n    src: x.yaml\n    dst: {dst}\n"
        f"    preserve_user_keys: [a]\nprofiles:\n  p:\n    tracked_files: [x]\n",
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


def test_yaml_compare_drift_treats_deep_paths_as_expected(tmp_path: Path) -> None:
    """Deep-list paths whose drift would otherwise look 'unexpected'
    must classify as expected so the wizard does not surface them."""
    src = tmp_path / "src.yaml"
    dst = tmp_path / "dst.yaml"
    _write(src, "a:\n  b: 1\n")
    _write(dst, "a:\n  b: 99\n  c: 2\n")
    expected, unexpected = classify_yaml_drift(
        src, dst, [], preserve_user_keys_deep=["a"]
    )
    assert "a.b" in expected or "a" in expected or "a.c" in expected
    assert unexpected == []


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
