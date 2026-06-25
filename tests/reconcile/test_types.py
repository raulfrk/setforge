"""FileId factory, HunkClass, and the ABSENT sentinel."""

from __future__ import annotations

import pytest

from setforge.errors import UnsafeFileId
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
