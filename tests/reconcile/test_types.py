"""FileId factory, HunkClass, and the ABSENT sentinel."""

from __future__ import annotations

from pathlib import Path

import pytest

from setforge.errors import ReconcileStoreError, UnsafeFileId
from setforge.reconcile import types as t


@pytest.mark.parametrize("good", ["claude/CLAUDE.md", "a", "x/y/z.json"])
def test_file_id_accepts_safe(good: str) -> None:
    assert t.file_id(good) == good  # NewType is identity at runtime


@pytest.mark.parametrize("bad", ["", ".", "..", "/abs", "a/../b", "a/\x00/b", "x\n"])
def test_file_id_rejects_unsafe(bad: str) -> None:
    with pytest.raises(UnsafeFileId):
        t.file_id(bad)


def test_hunkclass_values() -> None:
    assert {t.HunkClass.LOCAL, t.HunkClass.SHARED, t.HunkClass.PENDING}
    assert t.HunkClass.LOCAL == "local"  # StrEnum


def test_absent_is_singleton() -> None:
    assert t.ABSENT is t.ABSENT
    assert repr(t.ABSENT) == "ABSENT"


def test_resolve_store_path_maps_under_the_profile(tmp_path: Path) -> None:
    resolved = t.resolve_store_path(tmp_path / "local", "deb", "claude/CLAUDE.md")
    assert resolved == tmp_path.resolve() / "local" / "deb" / "claude" / "CLAUDE.md"


def test_resolve_store_path_appends_the_suffix_to_the_last_part(
    tmp_path: Path,
) -> None:
    resolved = t.resolve_store_path(tmp_path / "s", "deb", "a/b", suffix=".json")
    assert resolved == tmp_path.resolve() / "s" / "deb" / "a" / "b.json"


@pytest.mark.parametrize("profile", ["../../etc", "..", "", ".", "a/b", "x\x00y"])
def test_resolve_store_path_rejects_bad_profile(tmp_path: Path, profile: str) -> None:
    with pytest.raises(UnsafeFileId):
        t.resolve_store_path(tmp_path, profile, "x")


@pytest.mark.parametrize("fid", ["a/../../x", "/abs", "..", "", "a/./b", "a/\x00/b"])
def test_resolve_store_path_rejects_bad_fileid(tmp_path: Path, fid: str) -> None:
    with pytest.raises(UnsafeFileId):
        t.resolve_store_path(tmp_path, "p", fid)


def test_resolve_store_path_names_a_profile_that_holds_a_separator(
    tmp_path: Path,
) -> None:
    with pytest.raises(UnsafeFileId, match=r"^unsafe profile 'a/b': must not contain"):
        t.resolve_store_path(tmp_path, "a/b", "x")


def test_resolve_store_path_rejects_a_symlink_out_of_the_store(
    tmp_path: Path,
) -> None:
    (tmp_path / "store" / "vm").mkdir(parents=True)
    (tmp_path / "store" / "vm" / "out").symlink_to(tmp_path)
    with pytest.raises(
        ReconcileStoreError, match=r"^file-id 'out/x' resolves outside store/vm/$"
    ):
        t.resolve_store_path(tmp_path / "store", "vm", "out/x")


def test_resolve_store_path_rejects_a_symlink_into_a_sibling_profile(
    tmp_path: Path,
) -> None:
    (tmp_path / "store" / "p1").mkdir(parents=True)
    (tmp_path / "store" / "p2").mkdir()
    (tmp_path / "store" / "p2" / "victim").write_bytes(b"p2 bytes")
    (tmp_path / "store" / "p1" / "sib").symlink_to(tmp_path / "store" / "p2")
    with pytest.raises(
        ReconcileStoreError, match=r"^file-id 'sib/victim' resolves outside store/p1/$"
    ):
        t.resolve_store_path(tmp_path / "store", "p1", "sib/victim")


def test_resolve_store_path_rejects_a_profile_directory_that_is_a_symlink(
    tmp_path: Path,
) -> None:
    (tmp_path / "store" / "real").mkdir(parents=True)
    (tmp_path / "store" / "vm").symlink_to(tmp_path / "store" / "real")
    with pytest.raises(
        ReconcileStoreError, match=r"^file-id 'x' resolves outside store/vm/$"
    ):
        t.resolve_store_path(tmp_path / "store", "vm", "x")
