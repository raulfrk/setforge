"""Index dataclasses + the fail-closed JSON codec."""

from __future__ import annotations

import pytest

from setforge.errors import CorruptIndexError, IndexVersionError
from setforge.reconcile.index_model import (
    CURRENT_VERSION,
    FileEntry,
    Index,
    dumps,
    loads,
)


def test_round_trip() -> None:
    idx = Index(
        files={"a/b": FileEntry(present=True, local_hash="sha256:00", hunks=[])}
    )
    assert loads(dumps(idx)) == idx


def test_empty_round_trip() -> None:
    assert loads(dumps(Index(files={}))) == Index(files={})


def test_dump_byte_stable_and_sorted() -> None:
    idx = Index(
        files={
            "z": FileEntry(present=True, local_hash=None, hunks=[]),
            "a": FileEntry(present=False, local_hash=None, hunks=[]),
        }
    )
    out = dumps(idx)
    assert dumps(idx) == out  # idempotent
    assert out.endswith("\n")
    assert out.index('"a"') < out.index('"z"')  # sort_keys


def test_dump_ascii_escaped() -> None:
    # ensure_ascii=True: non-ASCII in metadata is escaped, never raw bytes.
    idx = Index(
        files={"f": FileEntry(present=True, local_hash="sha256:café", hunks=[])}
    )
    assert "\\u" in dumps(idx)


def test_corrupt_json() -> None:
    with pytest.raises(CorruptIndexError):
        loads("{not json")


def test_missing_version_field_is_corrupt() -> None:
    with pytest.raises(CorruptIndexError):
        loads('{"files": {}}')


def test_top_shape_not_object() -> None:
    with pytest.raises(CorruptIndexError):
        loads("[]")


def test_bad_entry_shape() -> None:
    with pytest.raises(CorruptIndexError):
        loads('{"schema_version": "1.0", "files": {"f": 3}}')


def test_newer_version_refused() -> None:
    with pytest.raises(IndexVersionError):
        loads('{"schema_version": "99.0", "files": {}}')


def test_current_version_loads() -> None:
    text = f'{{"schema_version": "{CURRENT_VERSION}", "files": {{}}}}'
    assert loads(text) == Index(files={})


def test_loads_never_writes(tmp_path, monkeypatch) -> None:
    # codec is pure: migrate-on-read happens in memory, no file is created.
    # chdir into tmp_path so a stray relative-path write would surface here.
    monkeypatch.chdir(tmp_path)
    before = set(tmp_path.rglob("*"))
    loads(dumps(Index(files={"f": FileEntry(present=True, local_hash=None, hunks=[])})))
    assert set(tmp_path.rglob("*")) == before
