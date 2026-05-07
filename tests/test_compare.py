"""Tests for drift compare and YAML drift classification."""

from pathlib import Path

import pytest

from my_setup.compare import (
    CompareStatus,
    classify_yaml_drift,
    compare_profile,
    diff_file,
)
from my_setup.config import Config, Dotfile, Profile


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
    _write(
        src,
        "<!-- my-setup:user-section start -->\n"
        "<!-- my-setup:user-section end -->\n",
    )
    _write(
        dst,
        "<!-- my-setup:user-section start -->\n"
        "live content\n"
        "<!-- my-setup:user-section end -->\n",
    )
    assert diff_file(src, dst, preserve_user_sections=True) == ""


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


def _make_config(profile: Profile, dotfile: Dotfile, key: str) -> Config:
    return Config(
        dotfiles={key: dotfile},
        profiles={"p": profile},
    )


def test_compare_profile_unchanged(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    src = repo / "tracked" / "x"
    _write(src, "same\n")
    dst = tmp_path / "live" / "x"
    _write(dst, "same\n")

    config = _make_config(
        Profile(dotfiles=["x"]),
        Dotfile(src=Path("x"), dst=str(dst)),
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
        Profile(dotfiles=["x"]),
        Dotfile(src=Path("x.md"), dst=str(dst)),
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
        Profile(dotfiles=["x"]),
        Dotfile(src=Path("x.yaml"), dst=str(dst), preserve_user_keys=["a"]),
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
        Profile(dotfiles=["x"]),
        Dotfile(src=Path("x.yaml"), dst=str(dst), preserve_user_keys=["a"]),
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
        Profile(dotfiles=["x"]),
        Dotfile(src=Path("x"), dst=str(dst)),
        "x",
    )
    report = compare_profile(config, "p", repo)
    assert report.entries[0].status is CompareStatus.MISSING
    assert report.has_unexpected_drift is True
