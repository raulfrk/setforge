"""Typed keys and sentinels for the reconcile store.

:data:`FileId` is a :class:`~typing.NewType` over ``str`` whose only sanctioned
constructor is :func:`file_id`, which validates a ``tracked_files`` key into a
path-safe identifier. The store API is typed on ``FileId`` so an unvalidated raw
string is a type error at the boundary; the resolver re-validates defensively
because a ``NewType`` cast is a runtime no-op.

:class:`HunkClass` is the per-hunk classification recorded in the index (set by
the later merge/staging layers — in this storage layer the hunk list is always
empty). :data:`ABSENT` is the explicit "this file is absent" sentinel, distinct
from a zero-byte file and from "not recorded yet" (``None``).
"""

from __future__ import annotations

import hashlib
from enum import Enum, StrEnum
from typing import Final, NewType

from setforge.errors import UnsafeFileId

FileId = NewType("FileId", str)

_CONTROL: Final = frozenset({chr(c) for c in range(0x20)} | {"\x7f"})


def content_sha(data: bytes) -> str:
    """The one content-integrity hash: stage's write path and store's
    fail-closed verify must agree byte-for-byte, or verify raises.
    """
    return "sha256:" + hashlib.sha256(data).hexdigest()


def file_id(key: str) -> FileId:
    """Validate a ``tracked_files`` key into a path-safe :data:`FileId`.

    Rejects the empty string, ``.``, ``..`` (whole or any ``/``-part), an
    absolute / leading-slash path, and any C0/DEL control character. Slashes
    are allowed (a file-id mirrors a relative path, e.g. ``claude/CLAUDE.md``).
    Raises :class:`~setforge.errors.UnsafeFileId` on rejection.
    """
    if key in ("", ".", "..") or key.startswith("/"):
        raise UnsafeFileId(f"unsafe file-id {key!r}: empty, '.', '..', or absolute")
    if any(ch in _CONTROL for ch in key):
        raise UnsafeFileId(f"unsafe file-id {key!r}: contains a control character")
    if any(part in ("", ".", "..") for part in key.split("/")):
        raise UnsafeFileId(f"unsafe file-id {key!r}: empty / '.' / '..' path part")
    return FileId(key)


class HunkClass(StrEnum):
    """Classification of an index hunk: kept-local, shared upstream, pending, or
    shared-via-draft.

    ``SHARED_DRAFTED`` marks a hunk whose tracked bytes are a Claude-rewritten
    *shareable* draft (in the ``drafts/`` store), not the host's live bytes — so
    ``live != tracked`` is the *expected*, blessed state for such a hunk (see
    :func:`setforge.reconcile.hunks.reconstruct`). The interactive ``stage``
    share-draft sub-flow produces these; this storage/engine layer only stores,
    reconstructs, and verifies them.
    """

    LOCAL = "local"
    SHARED = "shared"
    PENDING = "pending"
    SHARED_DRAFTED = "shared_drafted"


class _Absent(Enum):
    """Singleton enum backing :data:`ABSENT` (Enum members are unique + picklable)."""

    TOKEN = "absent"

    def __repr__(self) -> str:
        return "ABSENT"


ABSENT: Final = _Absent.TOKEN
"""Sentinel for an explicitly-absent file — distinct from ``b""`` and ``None``."""

Absent = _Absent
"""Type alias for annotations, e.g. ``bytes | None | Absent``."""
