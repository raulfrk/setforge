"""Tests for :mod:`setforge.migrations._fs_ops`."""

from __future__ import annotations

from pathlib import Path

from setforge.migrations._fs_ops import (
    atomic_replace,
    backup_path,
    iter_tracked_text_files,
)


def test_backup_path_format(tmp_path: Path) -> None:
    p = tmp_path / "setforge.yaml"
    assert backup_path(p, "1.1") == tmp_path / "setforge.yaml.pre-1.1.bak"


def test_backup_path_lives_in_same_directory(tmp_path: Path) -> None:
    nested = tmp_path / "deep" / "tree" / "CLAUDE.md"
    bp = backup_path(nested, "2.0")
    assert bp.parent == nested.parent
    assert bp.name == "CLAUDE.md.pre-2.0.bak"


def test_backup_path_with_multiple_dots_in_name(tmp_path: Path) -> None:
    p = tmp_path / "config.test.yaml"
    assert backup_path(p, "1.1") == tmp_path / "config.test.yaml.pre-1.1.bak"


def test_atomic_replace_moves_tmp_to_dst(tmp_path: Path) -> None:
    src = tmp_path / "fresh.tmp"
    src.write_text("new content\n", encoding="utf-8")
    dst = tmp_path / "target.yaml"
    dst.write_text("stale content\n", encoding="utf-8")
    atomic_replace(src, dst)
    assert not src.exists()
    assert dst.read_text(encoding="utf-8") == "new content\n"


def test_atomic_replace_creates_dst_when_absent(tmp_path: Path) -> None:
    src = tmp_path / "fresh.tmp"
    src.write_text("hi\n", encoding="utf-8")
    dst = tmp_path / "new_target.yaml"
    atomic_replace(src, dst)
    assert dst.read_text(encoding="utf-8") == "hi\n"


def test_iter_tracked_text_files_excludes_dot_git(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# readme\n", encoding="utf-8")
    out = list(iter_tracked_text_files(tmp_path))
    assert out == [tmp_path / "README.md"]


def test_iter_tracked_text_files_skips_binary_suffixes(tmp_path: Path) -> None:
    (tmp_path / "data.png").write_bytes(b"\x89PNG")
    (tmp_path / "doc.md").write_text("# hi\n", encoding="utf-8")
    out = set(iter_tracked_text_files(tmp_path))
    assert out == {tmp_path / "doc.md"}


def test_iter_tracked_text_files_recurses_subdirs(tmp_path: Path) -> None:
    (tmp_path / "tracked" / "claude").mkdir(parents=True)
    (tmp_path / "tracked" / "claude" / "CLAUDE.md").write_text(
        "hi", encoding="utf-8"
    )
    (tmp_path / "top.md").write_text("top", encoding="utf-8")
    out = set(iter_tracked_text_files(tmp_path))
    assert tmp_path / "tracked" / "claude" / "CLAUDE.md" in out
    assert tmp_path / "top.md" in out


def test_iter_tracked_text_files_empty_root(tmp_path: Path) -> None:
    out = list(iter_tracked_text_files(tmp_path))
    assert out == []


def test_iter_tracked_text_files_missing_root(tmp_path: Path) -> None:
    out = list(iter_tracked_text_files(tmp_path / "does-not-exist"))
    assert out == []
