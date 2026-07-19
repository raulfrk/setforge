"""Tests for :mod:`setforge.migrations._fs_ops`."""

from __future__ import annotations

from pathlib import Path

from setforge.migrations._fs_ops import atomic_replace, backup_path


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
