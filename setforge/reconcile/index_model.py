"""The per-profile index document and its fail-closed JSON codec.

``index/<profile>.json`` classifies each tracked file's hunks. In this storage
layer the ``hunks`` list is always empty (the store is the substrate; the later
merge/staging layers produce hunks), but the on-disk shape is fixed now so those
layers populate it without a schema migration::

    {
      "schema_version": "1.0",
      "files": {
        "<file-id>": {"present": true, "local_hash": "sha256:…", "hunks": []}
      }
    }

The codec is **pure and fail-closed**: :func:`loads` never writes to disk
(migrate-on-read happens in memory) and refuses a corrupt document or a
schema_version newer than this engine, rather than best-effort parsing it — the
store exists to kill silent overwrite, so a damaged index is an error, never a
silent re-seed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Final

from setforge.errors import CorruptIndexError, IndexVersionError

CURRENT_VERSION: Final = "1.0"
"""The index schema version this engine writes."""


@dataclass(frozen=True, slots=True)
class FileEntry:
    """One tracked file's index record.

    ``present`` is ``False`` for the explicit-absence sentinel (the file is
    absent on the recorded side), distinct from a zero-byte file. ``local_hash``
    is ``"sha256:<hex>"`` of the verbatim recorded-local bytes, or ``None`` when
    nothing local is recorded. ``hunks`` is always empty in this storage layer.
    """

    present: bool
    local_hash: str | None
    hunks: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Index:
    """A profile's whole index document: file-id → :class:`FileEntry`."""

    files: dict[str, FileEntry]
    version: str = CURRENT_VERSION


def dumps(index: Index) -> str:
    """Serialise ``index`` to byte-stable JSON text (trailing newline included).

    ``sort_keys`` + ``ensure_ascii`` make the output deterministic and ASCII —
    safe because the index is metadata only and is never the byte-reconstruction
    substrate (that is the verbatim ``local/`` store's job).
    """
    obj: dict[str, Any] = {
        "schema_version": index.version,
        "files": {
            fid: {
                "present": entry.present,
                "local_hash": entry.local_hash,
                "hunks": entry.hunks,
            }
            for fid, entry in index.files.items()
        },
    }
    return (
        json.dumps(obj, allow_nan=False, indent=2, sort_keys=True, ensure_ascii=True)
        + "\n"
    )


def _version_tuple(raw: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in raw.split("."))
    except (ValueError, AttributeError) as err:
        raise CorruptIndexError(
            f"index schema_version {raw!r} is not a version string"
        ) from err


def loads(text: str) -> Index:
    """Parse index JSON text into an :class:`Index`, fail-closed.

    Raises :class:`~setforge.errors.CorruptIndexError` on unparseable / wrongly
    shaped / version-field-missing documents and
    :class:`~setforge.errors.IndexVersionError` when the recorded
    ``schema_version`` is newer than :data:`CURRENT_VERSION`. An older version is
    migrated **in memory** (no disk write). Never side-effects the filesystem.
    """
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError) as err:
        raise CorruptIndexError(f"index is not valid JSON: {err}") from err

    if not isinstance(obj, dict):
        raise CorruptIndexError(
            f"index top level must be an object, got {type(obj).__name__}"
        )
    if "schema_version" not in obj:
        raise CorruptIndexError("index is missing the mandatory 'schema_version' field")

    found = _version_tuple(str(obj["schema_version"]))
    expected = _version_tuple(CURRENT_VERSION)
    if found > expected:
        raise IndexVersionError(
            f"index schema_version {obj['schema_version']} is newer than this engine "
            f"reads ({CURRENT_VERSION}); upgrade setforge"
        )
    # found < expected → migrate in memory (identity for 1.0; future maps go here).
    # found == expected → load as-is.

    raw_files = obj.get("files", {})
    if not isinstance(raw_files, dict):
        raise CorruptIndexError("index 'files' must be an object")

    files: dict[str, FileEntry] = {}
    for fid, raw_entry in raw_files.items():
        files[fid] = _parse_entry(fid, raw_entry)
    return Index(files=files, version=CURRENT_VERSION)


def _parse_entry(fid: str, raw: object) -> FileEntry:
    if not isinstance(raw, dict):
        raise CorruptIndexError(f"index entry for {fid!r} must be an object")
    if "present" not in raw or not isinstance(raw["present"], bool):
        raise CorruptIndexError(
            f"index entry for {fid!r} has a missing/invalid 'present'"
        )
    local_hash = raw.get("local_hash")
    if local_hash is not None and not isinstance(local_hash, str):
        raise CorruptIndexError(
            f"index entry for {fid!r} has a non-string 'local_hash'"
        )
    hunks = raw.get("hunks", [])
    if not isinstance(hunks, list):
        raise CorruptIndexError(f"index entry for {fid!r} has a non-list 'hunks'")
    for row in hunks:
        _check_hunk_row(fid, row)
    return FileEntry(present=raw["present"], local_hash=local_hash, hunks=hunks)


#: The two ``kind`` discriminators of a persisted hunk row — the frozen on-disk
#: schema values this module owns. ``line`` is a markdown line-hunk row; ``key``
#: is a structured key-unit row.
KIND_LINE: Final = "line"
KIND_KEY: Final = "key"

#: The keys a LINE hunk row must carry (mirrors ``hunks.serialize``). ``line`` is
#: the default ``kind``, so a legacy row with no ``kind`` stays valid + byte-stable.
_HUNK_ROW_KEYS_LINE: Final = ("cls", "label", "live_hash", "anchor")
#: The keys a structured KEY-unit row must carry (mirrors
#: ``structured_units.serialize_structured``): a dotted ``path`` + ``value_hash``
#: identity in place of the line row's ``anchor`` + ``live_hash``.
_HUNK_ROW_KEYS_KEY: Final = ("cls", "label", "path", "value_hash")
#: Valid ``kind`` discriminators; an absent ``kind`` defaults to ``line``.
_HUNK_KINDS: Final = frozenset({KIND_LINE, KIND_KEY})
#: Valid ``cls`` values — the :class:`~setforge.reconcile.types.HunkClass` value set.
#: Inlined (not imported) to keep this codec a leaf with no dependency on the
#: staging layer; the values are a frozen part of the on-disk schema.
_HUNK_CLASSES: Final = frozenset({"local", "shared", "pending", "shared_drafted"})


def _check_hunk_row(fid: str, row: object) -> None:
    """Fail-closed validation of one persisted hunk row.

    The ``hunks`` field is now populated (the staging layer), so the codec must
    cover it too: a hand-edited index with a malformed row must raise
    :class:`~setforge.errors.CorruptIndexError`, never let a partial dict reach
    the staging layer as an unwrapped ``KeyError`` / ``ValueError``.

    A ``kind`` discriminator selects the required identity keys: a ``line`` row
    (the default, for markdown line-hunks) carries ``live_hash`` + ``anchor``; a
    ``key`` row (structured key-unit) carries ``path`` + ``value_hash``. A
    ``shared_drafted`` row of either kind additionally carries a ``draft_hash``
    (string) — the identity of its shareable draft; the codec requires it so a
    drafted row can never reach the staging layer without the key its
    reconstruction keys on.
    """
    if not isinstance(row, dict):
        raise CorruptIndexError(f"index entry for {fid!r} has a non-object hunk row")
    kind = row.get("kind", KIND_LINE)
    if not isinstance(kind, str) or kind not in _HUNK_KINDS:
        raise CorruptIndexError(
            f"index entry for {fid!r} has a hunk row with an unknown kind {kind!r}"
        )
    required = _HUNK_ROW_KEYS_KEY if kind == KIND_KEY else _HUNK_ROW_KEYS_LINE
    for key in required:
        if not isinstance(row.get(key), str):
            raise CorruptIndexError(
                f"index entry for {fid!r} has a {kind} hunk row with a "
                f"missing/non-string {key!r}"
            )
    if row["cls"] not in _HUNK_CLASSES:
        raise CorruptIndexError(
            f"index entry for {fid!r} has a hunk row with an unknown cls {row['cls']!r}"
        )
    draft_hash = row.get("draft_hash")
    if row["cls"] == "shared_drafted":
        if not isinstance(draft_hash, str):
            raise CorruptIndexError(
                f"index entry for {fid!r} has a 'shared_drafted' hunk row with a "
                f"missing/non-string 'draft_hash'"
            )
    elif draft_hash is not None and not isinstance(draft_hash, str):
        raise CorruptIndexError(
            f"index entry for {fid!r} has a hunk row with a non-string 'draft_hash'"
        )
