"""The per-profile index document and its fail-closed JSON codec.

``index/<profile>.json`` classifies each tracked file's hunks. The ``hunks``
list is populated by the staging layer (per-hunk classification rows); this
storage layer just persists whatever it's given, validating the on-disk shape
without interpreting hunk semantics::

    {
      "schema_version": "2.0",
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
from enum import StrEnum
from typing import Any, Final

from setforge.errors import CorruptIndexError, IndexVersionError, InvariantViolation
from setforge.reconcile.types import UnitKind, content_sha

CURRENT_VERSION: Final = "2.0"
"""The index schema version this engine writes."""


@dataclass(frozen=True, slots=True)
class FileEntry:
    """One tracked file's index record.

    ``present`` is ``False`` for the explicit-absence sentinel (the file is
    absent on the recorded side), distinct from a zero-byte file. ``local_hash``
    is ``"sha256:<hex>"`` of the verbatim recorded-local bytes, or ``None`` when
    nothing local is recorded. ``hunks`` is populated by the staging layer
    (per-hunk classification rows); an empty list means no drafted hunks.
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

    raw_version = obj["schema_version"]
    if not isinstance(raw_version, str):
        raise CorruptIndexError("index schema_version must be a string")
    found = _version_tuple(raw_version)
    expected = _version_tuple(CURRENT_VERSION)
    if found > expected:
        raise IndexVersionError(
            f"index schema_version {raw_version} is newer than this engine "
            f"reads ({CURRENT_VERSION}); upgrade setforge"
        )
    if raw_version not in {"1.0", CURRENT_VERSION} and found in {(1, 0), expected}:
        raise CorruptIndexError(
            f"index schema_version {raw_version!r} is not a canonical supported value"
        )
    if found not in {(1, 0), expected}:
        raise IndexVersionError(
            f"index schema_version {raw_version} is unsupported; "
            f"this engine reads exactly 1.0 and {CURRENT_VERSION}"
        )

    raw_files = obj.get("files", {})
    if not isinstance(raw_files, dict):
        raise CorruptIndexError("index 'files' must be an object")

    files: dict[str, FileEntry] = {}
    legacy = found == (1, 0)
    for fid, raw_entry in raw_files.items():
        files[fid] = _parse_entry(fid, raw_entry, legacy=legacy)
    return Index(files=files, version=CURRENT_VERSION)


def _frame(part: bytes) -> bytes:
    return len(part).to_bytes(8, "big") + part


def _legacy_line_unit_id(anchor: str) -> str:
    """Give a v1 anchor a deterministic bridge ID during in-memory migration."""
    return content_sha(b"setforge.legacy-line.v1\x00" + _frame(anchor.encode("ascii")))


def _parse_entry(fid: str, raw: object, *, legacy: bool) -> FileEntry:
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
    normalized: list[dict[str, Any]] = []
    for row in hunks:
        if legacy:
            normalized.append(_migrate_v1_hunk(fid, row))
        else:
            _check_hunk_row(fid, row)
            normalized.append(row)
    unit_ids = [
        str(row["unit_id"]) for row in normalized if row.get("kind") == HunkKind.LINE
    ]
    if len(unit_ids) != len(set(unit_ids)):
        raise CorruptIndexError(f"index entry for {fid!r} has duplicate line unit_id")
    key_paths = [
        str(row["path"]) for row in normalized if row.get("kind") == HunkKind.KEY
    ]
    if len(key_paths) != len(set(key_paths)):
        raise CorruptIndexError(f"index entry for {fid!r} has duplicate key path")
    return FileEntry(present=raw["present"], local_hash=local_hash, hunks=normalized)


def _migrate_v1_hunk(fid: str, row: object) -> dict[str, Any]:
    """Normalize one v1 row into the v2 discriminated schema in memory."""
    if not isinstance(row, dict):
        raise CorruptIndexError(f"index entry for {fid!r} has a non-object hunk row")
    kind = row.get("kind", HunkKind.LINE)
    migrated = dict(row)
    if kind == HunkKind.LINE:
        anchor = row.get("anchor")
        if not isinstance(anchor, str):
            raise CorruptIndexError(
                f"index entry for {fid!r} has a line hunk row with a "
                "missing/non-string 'anchor'"
            )
        try:
            unit_id = _legacy_line_unit_id(anchor)
        except UnicodeEncodeError as err:
            raise CorruptIndexError(
                f"index entry for {fid!r} has a non-ASCII legacy anchor"
            ) from err
        migrated.pop("anchor", None)
        migrated["kind"] = HunkKind.LINE
        migrated["unit_id"] = unit_id
        migrated["legacy_anchor"] = anchor
    else:
        migrated["kind"] = kind
    _check_hunk_row(fid, migrated)
    return migrated


HunkKind = UnitKind
"""Backwards-compatible name for the persisted unit-kind discriminator."""


class HunkCls(StrEnum):
    """The valid ``cls`` values of a persisted hunk row — the on-disk mirror of
    :class:`setforge.reconcile.types.HunkClass`'s value set.

    Defined locally (not imported) to keep this codec a leaf with no dependency
    on the staging layer; the values are a frozen part of the on-disk schema and
    must stay byte-identical to ``types.HunkClass``.
    """

    LOCAL = "local"
    SHARED = "shared"
    PENDING = "pending"
    SHARED_DRAFTED = "shared_drafted"


#: Backwards-compatible aliases for the ``kind`` discriminators (the staging
#: layer imports :data:`KIND_KEY`); both equal their bare on-disk string.
KIND_LINE: Final = HunkKind.LINE
KIND_KEY: Final = HunkKind.KEY


def require_unit_kind(
    rows: list[dict[str, object]], expected: UnitKind
) -> list[dict[str, object]]:
    """Return ``rows`` only when every persisted unit matches ``expected``."""
    mismatched = [row.get("kind") for row in rows if row.get("kind") != expected]
    if mismatched:
        raise InvariantViolation(
            f"persisted unit kind {mismatched[0]!r} is incompatible with "
            f"current {expected.value!r} routing"
        )
    return rows


#: The keys a v2 LINE hunk row must carry (mirrors ``hunks.serialize``).
_HUNK_ROW_KEYS_LINE: Final = ("cls", "label", "live_hash", "unit_id")
#: The keys a structured KEY-unit row must carry (mirrors
#: ``structured_units.serialize_structured``): a dotted ``path`` + ``value_hash``
#: identity in place of the line row's ``anchor`` + ``live_hash``.
_HUNK_ROW_KEYS_KEY: Final = ("cls", "label", "path", "value_hash")
#: Valid ``kind`` discriminators; an absent ``kind`` defaults to ``line``.
_HUNK_KINDS: Final = frozenset(HunkKind)
#: Valid ``cls`` values, derived from the local :class:`HunkCls` StrEnum.
_HUNK_CLASSES: Final = frozenset(HunkCls)


def _check_hunk_row(fid: str, row: object) -> None:
    """Fail-closed validation of one persisted hunk row.

    The ``hunks`` field is now populated (the staging layer), so the codec must
    cover it too: a hand-edited index with a malformed row must raise
    :class:`~setforge.errors.CorruptIndexError`, never let a partial dict reach
    the staging layer as an unwrapped ``KeyError`` / ``ValueError``.

    A mandatory ``kind`` discriminator selects the required identity keys: a
    ``line`` row carries ``live_hash`` + ``unit_id``; a
    ``key`` row (structured key-unit) carries ``path`` + ``value_hash``. A
    ``shared_drafted`` row of either kind additionally carries a ``draft_hash``
    (string) — the identity of its shareable draft; the codec requires it so a
    drafted row can never reach the staging layer without the key its
    reconstruction keys on. An optional ``reloc_anchor`` (heading identity) may
    appear on any row; when present it must be a string, else it is never
    required.
    """
    if not isinstance(row, dict):
        raise CorruptIndexError(f"index entry for {fid!r} has a non-object hunk row")
    kind = row.get("kind")
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
    _check_optional_hunk_fields(fid, row, kind)


def _check_optional_hunk_fields(fid: str, row: dict[object, object], kind: str) -> None:
    """Validate class-dependent and optional fields after core row validation."""
    draft_hash = row.get("draft_hash")
    if row["cls"] == HunkCls.SHARED_DRAFTED:
        if not isinstance(draft_hash, str):
            raise CorruptIndexError(
                f"index entry for {fid!r} has a 'shared_drafted' hunk row with a "
                f"missing/non-string 'draft_hash'"
            )
    elif draft_hash is not None and not isinstance(draft_hash, str):
        raise CorruptIndexError(
            f"index entry for {fid!r} has a hunk row with a non-string 'draft_hash'"
        )
    reloc_anchor = row.get("reloc_anchor")
    if reloc_anchor is not None and not isinstance(reloc_anchor, str):
        raise CorruptIndexError(
            f"index entry for {fid!r} has a hunk row with a non-string 'reloc_anchor'"
        )
    legacy_anchor = row.get("legacy_anchor")
    if legacy_anchor is not None and (
        kind != KIND_LINE or not isinstance(legacy_anchor, str)
    ):
        raise CorruptIndexError(
            f"index entry for {fid!r} has a hunk row with an invalid 'legacy_anchor'"
        )
