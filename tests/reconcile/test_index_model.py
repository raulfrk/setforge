"""Index dataclasses + the fail-closed JSON codec."""

from __future__ import annotations

import pytest

from setforge.errors import CorruptIndexError, IndexVersionError, InvariantViolation
from setforge.reconcile.index_model import (
    CURRENT_VERSION,
    FileEntry,
    HunkCls,
    HunkKind,
    Index,
    dumps,
    loads,
    require_unit_kind,
)
from setforge.reconcile.types import UnitKind


def test_round_trip() -> None:
    idx = Index(
        files={"a/b": FileEntry(present=True, local_hash="sha256:00", hunks=[])}
    )
    assert loads(dumps(idx)) == idx


def test_key_kind_hunk_row_round_trips() -> None:
    """A structured ``kind:'key'`` row (path + value_hash) survives dumps/loads."""
    row = {
        "kind": "key",
        "cls": "shared",
        "label": "editor.fontSize",
        "path": "editor.fontSize",
        "value_hash": "sha256:v",
    }
    idx = Index(
        files={"s.yaml": FileEntry(present=True, local_hash="sha256:l", hunks=[row])}
    )
    out = loads(dumps(idx))
    assert out.files["s.yaml"].hunks[0]["path"] == "editor.fontSize"


@pytest.mark.parametrize(
    ("stored", "current"),
    [(UnitKind.LINE, UnitKind.KEY), (UnitKind.KEY, UnitKind.LINE)],
)
def test_require_unit_kind_rejects_incompatible_routing(
    stored: UnitKind, current: UnitKind
) -> None:
    with pytest.raises(InvariantViolation, match="incompatible with current"):
        require_unit_kind([{"kind": stored}], current)


def test_require_unit_kind_accepts_only_current_routing() -> None:
    rows: list[dict[str, object]] = [{"kind": UnitKind.KEY}, {"kind": "key"}]
    assert require_unit_kind(rows, UnitKind.KEY) is rows


def test_discriminators_serialise_to_bare_on_disk_strings() -> None:
    """The local StrEnums persist byte-identically to the frozen on-disk schema.

    ``HunkKind``/``HunkCls`` members ARE ``str`` and equal their ``.value``, so
    ``dumps`` must emit the bare discriminator strings — no ``"HunkKind.KEY"`` /
    enum-qualified leak. Asserting on the raw JSON text proves the StrEnum
    conversion did not change the persisted format.
    """
    # members equal their exact on-disk byte-strings
    assert HunkKind.LINE == "line"
    assert HunkKind.KEY == "key"
    assert HunkCls.LOCAL == "local"
    assert HunkCls.SHARED == "shared"
    assert HunkCls.PENDING == "pending"
    assert HunkCls.SHARED_DRAFTED == "shared_drafted"

    row = {
        "kind": HunkKind.KEY,
        "cls": HunkCls.SHARED_DRAFTED,
        "label": "editor.fontSize",
        "path": "editor.fontSize",
        "value_hash": "sha256:v",
        "draft_hash": "sha256:d",
    }
    idx = Index(
        files={"s.yaml": FileEntry(present=True, local_hash="sha256:l", hunks=[row])}
    )
    text = dumps(idx)
    # raw persisted text carries the bare literals, never an enum-qualified name
    assert '"kind": "key"' in text
    assert '"cls": "shared_drafted"' in text
    assert "HunkKind" not in text
    assert "HunkCls" not in text
    # and the bytes round-trip back to the same literal strings
    out = loads(text)
    hunk = out.files["s.yaml"].hunks[0]
    assert hunk["kind"] == "key"
    assert hunk["cls"] == "shared_drafted"


def test_line_row_without_kind_still_valid() -> None:
    """A v1 line row migrates in memory to the discriminated v2 identity shape."""
    import json

    text = json.dumps(
        {
            "schema_version": "1.0",
            "files": {
                "f": {
                    "present": True,
                    "local_hash": None,
                    "hunks": [
                        {
                            "cls": "shared",
                            "label": "x",
                            "live_hash": "sha256:h",
                            "anchor": "sha256:a",
                        }
                    ],
                }
            },
        }
    )
    row = loads(text).files["f"].hunks[0]
    assert row["kind"] == "line"
    assert row["legacy_anchor"] == "sha256:a"
    assert str(row["unit_id"]).startswith("sha256:")
    assert "anchor" not in row


def test_v1_duplicate_line_anchors_fail_closed() -> None:
    import json

    row = {
        "cls": "shared",
        "label": "x",
        "live_hash": "sha256:h",
        "anchor": "sha256:a",
    }
    text = json.dumps(
        {
            "schema_version": "1.0",
            "files": {"f": {"present": True, "local_hash": None, "hunks": [row, row]}},
        }
    )
    with pytest.raises(CorruptIndexError, match="duplicate line unit_id"):
        loads(text)


def test_v2_duplicate_line_unit_ids_fail_closed() -> None:
    import json

    row = {
        "kind": "line",
        "cls": "shared",
        "label": "x",
        "live_hash": "sha256:h",
        "unit_id": "sha256:u",
    }
    text = json.dumps(
        {
            "schema_version": "2.0",
            "files": {"f": {"present": True, "local_hash": None, "hunks": [row, row]}},
        }
    )
    with pytest.raises(CorruptIndexError, match="duplicate line unit_id"):
        loads(text)


def test_v2_duplicate_key_paths_fail_closed() -> None:
    import json

    row = {
        "kind": "key",
        "cls": "shared",
        "label": "x",
        "path": "same",
        "value_hash": "sha256:v",
    }
    text = json.dumps(
        {
            "schema_version": "2.0",
            "files": {"f": {"present": True, "local_hash": None, "hunks": [row, row]}},
        }
    )
    with pytest.raises(CorruptIndexError, match="duplicate key path"):
        loads(text)


def test_same_identity_across_line_and_key_kinds_is_valid() -> None:
    rows = [
        {
            "kind": "line",
            "cls": "shared",
            "label": "line",
            "unit_id": "same",
            "live_hash": "sha256:l",
        },
        {
            "kind": "key",
            "cls": "shared",
            "label": "key",
            "path": "same",
            "value_hash": "sha256:k",
        },
    ]
    idx = Index(files={"f": FileEntry(True, None, rows)})
    assert loads(dumps(idx)).files["f"].hunks == rows


@pytest.mark.parametrize("version", ["0.9", "1.1", "1.9"])
def test_unsupported_historical_versions_are_rejected(version: str) -> None:
    with pytest.raises(IndexVersionError, match="unsupported"):
        loads(f'{{"schema_version":"{version}","files":{{}}}}')


def test_key_row_missing_path_is_rejected() -> None:
    """A ``kind:'key'`` row missing its ``path`` fails closed (CorruptIndexError)."""
    import json

    text = json.dumps(
        {
            "schema_version": "1.0",
            "files": {
                "f": {
                    "present": True,
                    "local_hash": None,
                    "hunks": [
                        {
                            "kind": "key",
                            "cls": "shared",
                            "label": "a",
                            "value_hash": "sha256:v",
                        }
                    ],
                }
            },
        }
    )
    with pytest.raises(CorruptIndexError):
        loads(text)


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


@pytest.mark.parametrize("version", [1.0, 2.0, 1, 2])
def test_numeric_schema_version_is_corrupt(version: object) -> None:
    import json

    with pytest.raises(CorruptIndexError, match="must be a string"):
        loads(json.dumps({"schema_version": version, "files": {}}))


@pytest.mark.parametrize("version", ["1.00", "01.0", "2.00", "02.0"])
def test_schema_version_alias_is_corrupt(version: str) -> None:
    import json

    with pytest.raises(CorruptIndexError, match="canonical supported value"):
        loads(json.dumps({"schema_version": version, "files": {}}))


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


# --------------------------------------------------------------------------- #
# Hunk-row shape validation (A5 populated the previously-empty hunks field)
# --------------------------------------------------------------------------- #

_VALID_HUNK = (
    '{"cls":"shared","label":"## Shell","live_hash":"sha256:aa","anchor":"sha256:bb"}'
)


def test_valid_hunk_row_round_trips() -> None:
    text = (
        '{"schema_version":"1.0","files":{"f":'
        f'{{"present":true,"local_hash":"sha256:00","hunks":[{_VALID_HUNK}]}}}}}}'
    )
    entry = loads(text).files["f"]
    assert entry.hunks[0]["cls"] == "shared"


@pytest.mark.parametrize(
    "bad_hunk",
    [
        '"not-an-object"',
        '{"cls":"shared","label":"x","live_hash":"sha256:aa"}',  # missing anchor
        '{"cls":"shared","label":"x","live_hash":123,"anchor":"sha256:bb"}',  # non-str
        '{"cls":"bogus","label":"x","live_hash":"sha256:aa","anchor":"sha256:bb"}',
    ],
)
def test_malformed_hunk_row_is_corrupt(bad_hunk: str) -> None:
    text = (
        '{"schema_version":"1.0","files":{"f":'
        f'{{"present":true,"local_hash":"sha256:00","hunks":[{bad_hunk}]}}}}}}'
    )
    with pytest.raises(CorruptIndexError):
        loads(text)


# --------------------------------------------------------------------------- #
# A5c: the shared_drafted class + its draft_hash
# --------------------------------------------------------------------------- #

_DRAFTED_HUNK = (
    '{"cls":"shared_drafted","label":"## Worktrees","live_hash":"sha256:aa",'
    '"anchor":"sha256:bb","draft_hash":"sha256:cc"}'
)


def test_shared_drafted_row_round_trips() -> None:
    text = (
        '{"schema_version":"1.0","files":{"f":'
        f'{{"present":true,"local_hash":"sha256:00","hunks":[{_DRAFTED_HUNK}]}}}}}}'
    )
    entry = loads(text).files["f"]
    assert entry.hunks[0]["cls"] == "shared_drafted"
    assert entry.hunks[0]["draft_hash"] == "sha256:cc"


def test_shared_drafted_without_draft_hash_is_corrupt() -> None:
    # a drafted row must carry the draft_hash its reconstruction keys on.
    bad = (
        '{"cls":"shared_drafted","label":"x","live_hash":"sha256:aa",'
        '"anchor":"sha256:bb"}'
    )
    text = (
        '{"schema_version":"1.0","files":{"f":'
        f'{{"present":true,"local_hash":"sha256:00","hunks":[{bad}]}}}}}}'
    )
    with pytest.raises(CorruptIndexError):
        loads(text)


def test_non_string_draft_hash_is_corrupt() -> None:
    bad = (
        '{"cls":"shared","label":"x","live_hash":"sha256:aa","anchor":"sha256:bb",'
        '"draft_hash":123}'
    )
    text = (
        '{"schema_version":"1.0","files":{"f":'
        f'{{"present":true,"local_hash":"sha256:00","hunks":[{bad}]}}}}}}'
    )
    with pytest.raises(CorruptIndexError):
        loads(text)


def test_line_row_with_reloc_anchor_round_trips() -> None:
    row = (
        '{"cls":"local","label":"## My Tweaks","live_hash":"sha256:aa",'
        '"anchor":"sha256:bb","reloc_anchor":"## My Tweaks"}'
    )
    text = (
        '{"schema_version":"1.0","files":{"f":'
        f'{{"present":true,"local_hash":"sha256:00","hunks":[{row}]}}}}}}'
    )
    entry = loads(text).files["f"]
    assert entry.hunks[0]["reloc_anchor"] == "## My Tweaks"


def test_non_string_reloc_anchor_is_corrupt() -> None:
    bad = (
        '{"cls":"local","label":"x","live_hash":"sha256:aa","anchor":"sha256:bb",'
        '"reloc_anchor":123}'
    )
    text = (
        '{"schema_version":"1.0","files":{"f":'
        f'{{"present":true,"local_hash":"sha256:00","hunks":[{bad}]}}}}}}'
    )
    with pytest.raises(CorruptIndexError):
        loads(text)


def test_line_row_without_reloc_anchor_is_valid() -> None:
    row = '{"cls":"local","label":"x","live_hash":"sha256:aa","anchor":"sha256:bb"}'
    text = (
        '{"schema_version":"1.0","files":{"f":'
        f'{{"present":true,"local_hash":"sha256:00","hunks":[{row}]}}}}}}'
    )
    entry = loads(text).files["f"]
    assert "reloc_anchor" not in entry.hunks[0]
