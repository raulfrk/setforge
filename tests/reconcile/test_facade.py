"""The reconcile package re-export façade."""

from __future__ import annotations

import setforge.reconcile as r


def test_facade_exports() -> None:
    expected = {
        "ABSENT",
        "FileEntry",
        "FileId",
        "HunkClass",
        "Index",
        "file_id",
        "prune",
        "read_base",
        "read_index",
        "read_local",
        "reconstruct",
        "record",
        "verify",
        "write_base",
        "write_index",
        "write_local",
    }
    assert expected <= set(r.__all__)
    for name in expected:
        assert hasattr(r, name), name


def test_all_is_sorted() -> None:
    assert list(r.__all__) == sorted(r.__all__)
