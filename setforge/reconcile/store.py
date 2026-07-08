"""The base/local/index store substrate for the reconcile engine.

Three on-disk stores under ``state_root()``, keyed by ``(profile, file-id)``:

* ``base/<profile>/<file-id>`` — the 3-way merge base (verbatim bytes). Reused
  from :mod:`setforge.base_store`; this module is a thin typed pass-through.
* ``local/<profile>/<file-id>`` — recorded keep-local content (verbatim bytes).
  An explicitly-absent file is recorded as a marker under a SEPARATE
  ``local-absent/<profile>/<file-id>`` subtree (not a sibling suffix of the
  content file), so a file-id can never collide with another file-id's marker.
* ``index/<profile>.json`` — per-profile classification document
  (:mod:`setforge.reconcile.index_model`). In this storage layer every entry's
  hunk list is empty.

The store is **storage only** — no merge, no hunk production, no CLI wiring. It is
**fail-closed**: :func:`verify` cross-checks the index against the on-disk local
files (INV-2 byte round-trip + INV-10 no-orphan) and raises
:class:`~setforge.errors.InvariantViolation`; the index codec refuses corrupt or
future-version documents.

**Concurrency contract.** The mutating entry points (:func:`write_base`,
:func:`write_local`, :func:`write_index`, :func:`record`, :func:`prune`) MUST be
called inside the caller's ``with locking.profile_lock(profile):`` frame. This
module deliberately does **not** acquire the lock itself: the caller (e.g. the
future merge step / ``install``) already holds it across a larger critical section,
and a second in-process acquisition on a fresh fd would deadlock. :func:`record`
writes the index **last**, so a crash leaves at worst a prunable orphan base,
never a dangling index pointer.
"""

from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path

from setforge import atomicio, base_store
from setforge.errors import (
    CorruptIndexError,
    IndexVersionError,
    InvariantViolation,
    ReconcileStoreError,
    UnsafeFileId,
)
from setforge.reconcile import index_model
from setforge.reconcile.index_model import FileEntry, Index
from setforge.reconcile.types import (
    ABSENT,
    Absent,
    FileId,
    HunkClass,
    content_sha,
    file_id,
)
from setforge.transitions import state_root

_DIR_MODE = 0o700
_FILE_MODE = 0o600

__all__ = [
    "prune",
    "read_base",
    "read_drafts",
    "read_index",
    "read_local",
    "reconstruct",
    "record",
    "verify",
    "write_base",
    "write_drafts",
    "write_index",
    "write_local",
]


# --------------------------------------------------------------------------- #
# Roots + path-safe resolver
# --------------------------------------------------------------------------- #


def _local_root() -> Path:
    return state_root() / "local"


def _local_absent_root() -> Path:
    # Absence markers live in their own subtree (NOT a sibling suffix of the
    # content file), so a file-id literally ending in the marker name can never
    # collide with another file-id's marker.
    return state_root() / "local-absent"


def _index_root() -> Path:
    return state_root() / "index"


def _check_segment(value: str, kind: str, *, allow_slash: bool) -> None:
    """Reject a path segment that could escape the store subtree.

    Both profile and file-id come from semi-trusted config and share the
    file-id grammar (no empty / ``.`` / ``..`` / absolute / control char). A
    profile is a SINGLE directory level, so it additionally forbids ``/``; a
    file-id mirrors a relative path and may contain ``/``.
    """
    file_id(value)  # raises UnsafeFileId on empty / '.' / '..' / absolute / control
    if not allow_slash and "/" in value:
        raise UnsafeFileId(f"unsafe {kind} {value!r}: must not contain '/'")


def _resolve(sub_root: Path, profile: str, fid: str) -> Path:
    """Map ``(profile, fid)`` to a path under ``sub_root``, guarding traversal.

    Guards BOTH ``profile`` and ``fid`` (the latent gap in
    :func:`setforge.base_store._resolve_target` is the unguarded profile), and
    pins containment to a **constant** ``sub_root`` rather than a profile-derived
    root, so a malicious profile can never widen the allowed area.
    """
    _check_segment(profile, "profile", allow_slash=False)
    _check_segment(fid, "file-id", allow_slash=True)
    root = sub_root.resolve()
    target = (root / profile / fid).resolve()
    if target != root and root not in target.parents:
        raise ReconcileStoreError(
            f"path for {profile}/{fid} resolves outside {sub_root.name}/"
        )
    return target


def _index_path(profile: str) -> Path:
    """Resolve ``index/<profile>.json``, guarding the profile segment."""
    _check_segment(profile, "profile", allow_slash=False)
    root = _index_root().resolve()
    target = (root / f"{profile}.json").resolve()
    if target.parent != root:
        raise ReconcileStoreError(f"index path for {profile} resolves outside index/")
    return target


def _mkdir_secure(path: Path) -> None:
    """Create ``path`` (and parents) with private (0o700) permissions."""
    path.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)


# --------------------------------------------------------------------------- #
# base/ — thin typed pass-through to base_store
# --------------------------------------------------------------------------- #


def read_base(profile: str, fid: FileId) -> bytes | None:
    """Return the stored base bytes for ``fid``, or ``None`` if none recorded."""
    return base_store.read_base(profile, str(fid))


def write_base(profile: str, fid: FileId, data: bytes) -> None:
    """Store ``data`` as the base for ``fid``. Call inside ``profile_lock``."""
    base_store.write_base(profile, str(fid), data)


# --------------------------------------------------------------------------- #
# local/ — verbatim bytes + explicit-absence marker
# --------------------------------------------------------------------------- #


def _local_paths(profile: str, fid: FileId) -> tuple[Path, Path]:
    content = _resolve(_local_root(), profile, str(fid))
    marker = _resolve(_local_absent_root(), profile, str(fid))
    return content, marker


def read_local(profile: str, fid: FileId) -> bytes | None | Absent:
    """Return recorded keep-local content for ``fid``.

    ``None`` — nothing recorded; ``ABSENT`` — explicitly recorded as absent;
    otherwise the verbatim bytes (including ``b""`` for an empty file). The
    trichotomy is decided by the filesystem, never by truthiness.
    """
    content, marker = _local_paths(profile, fid)
    try:
        return content.read_bytes()
    except FileNotFoundError:
        pass
    except OSError as err:
        raise ReconcileStoreError(
            f"failed to read local for {profile}/{fid}: {err}"
        ) from err
    return ABSENT if marker.exists() else None


def write_local(profile: str, fid: FileId, data: bytes | Absent) -> None:
    """Record ``data`` as keep-local content for ``fid`` (verbatim bytes) or, for
    :data:`ABSENT`, an absence marker. Call inside ``profile_lock``.
    """
    content, marker = _local_paths(profile, fid)
    try:
        if data is ABSENT:
            _mkdir_secure(marker.parent)
            atomicio.atomic_write_bytes(marker, b"", mode=_FILE_MODE)
            content.unlink(missing_ok=True)
        else:
            _mkdir_secure(content.parent)
            atomicio.atomic_write_bytes(content, data, mode=_FILE_MODE)
            marker.unlink(missing_ok=True)
    except OSError as err:
        raise ReconcileStoreError(
            f"failed to write local for {profile}/{fid}: {err}"
        ) from err


# --------------------------------------------------------------------------- #
# drafts/ — A5c shareable-draft bytes (per-fid manifest: anchor → bytes)
# --------------------------------------------------------------------------- #


def _drafts_path(profile: str, fid: FileId) -> Path:
    """Typed (FileId) wrapper over :func:`drafts_manifest_path` for in-module use."""
    return drafts_manifest_path(profile, str(fid))


# --------------------------------------------------------------------------- #
# Public path accessors — for the snapshot/revert layer (transitions.py), which
# captures + restores each store file by path. Take a string ``key`` (the
# tracked-file id / ``expand_tracked_file`` sub_name), mirroring base_store.base_path.
# --------------------------------------------------------------------------- #


def local_content_path(profile: str, key: str) -> Path:
    """The local keep-content file path for ``key`` (one leg of the local store)."""
    return _resolve(_local_root(), profile, key)


def local_absent_path(profile: str, key: str) -> Path:
    """The local absence-marker file path for ``key`` (the other local leg)."""
    return _resolve(_local_absent_root(), profile, key)


def drafts_manifest_path(profile: str, key: str) -> Path:
    """The per-fid drafts manifest path for ``key``."""
    return _resolve(_drafts_root(), profile, key)


def index_manifest_path(profile: str) -> Path:
    """The per-profile index document path (profile-scoped, key-independent)."""
    return _index_path(profile)


def _drafts_root() -> Path:
    return state_root() / "drafts"


def read_drafts(profile: str, fid: FileId) -> dict[str, bytes]:
    """Return the shareable-draft bytes for ``fid``, keyed by hunk ``anchor``.

    Empty dict when nothing is recorded. The on-disk manifest is a JSON object
    mapping ``anchor`` → base64 draft bytes; each drafted hunk's ``draft_hash`` is
    recorded in the index, but the bytes themselves live here (the index stays
    pure metadata). Fail-closed: a damaged manifest raises rather than silently
    dropping a blessed draft.
    """
    path = _drafts_path(profile, fid)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as err:
        raise ReconcileStoreError(
            f"failed to read drafts for {profile}/{fid}: {err}"
        ) from err
    try:
        obj = json.loads(text)
        if not isinstance(obj, dict):
            raise ValueError("drafts manifest top level must be an object")
        result: dict[str, bytes] = {}
        for anchor, b64 in obj.items():
            # Validate the value type rather than str()-coercing it: a non-string
            # scalar (null/number/bool) would otherwise decode to garbage bytes
            # instead of failing closed (the docstring's "a damaged manifest
            # raises" contract). JSON keys are always strings, so anchor is sound.
            if not isinstance(b64, str):
                raise ValueError(
                    f"drafts manifest value for {anchor!r} is not a string"
                )
            result[str(anchor)] = base64.b64decode(b64, validate=True)
        return result
    except (ValueError, json.JSONDecodeError, binascii.Error) as err:
        raise ReconcileStoreError(
            f"drafts manifest for {profile}/{fid} is corrupt: {err}"
        ) from err


def write_drafts(profile: str, fid: FileId, drafts: dict[str, bytes]) -> None:
    """Record ``drafts`` (anchor → bytes) for ``fid``. Call inside ``profile_lock``.

    An empty mapping removes the manifest (no drafted hunks remain). Written
    BEFORE the index in :func:`record` so a crash leaves a prunable orphan
    manifest, never an index row pointing at a draft that was never stored.
    """
    path = _drafts_path(profile, fid)
    try:
        if not drafts:
            path.unlink(missing_ok=True)
            return
        obj = {
            anchor: base64.b64encode(data).decode("ascii")
            for anchor, data in drafts.items()
        }
        _mkdir_secure(path.parent)
        atomicio.atomic_write_text(
            path,
            json.dumps(
                obj, allow_nan=False, indent=2, sort_keys=True, ensure_ascii=True
            )
            + "\n",
            mode=_FILE_MODE,
        )
    except OSError as err:
        raise ReconcileStoreError(
            f"failed to write drafts for {profile}/{fid}: {err}"
        ) from err


# --------------------------------------------------------------------------- #
# index/ — per-profile JSON document
# --------------------------------------------------------------------------- #


def read_index(profile: str) -> Index:
    """Load ``index/<profile>.json``; empty :class:`Index` when absent.

    Raises :class:`~setforge.errors.CorruptIndexError` /
    :class:`~setforge.errors.IndexVersionError` (via the codec) on a damaged or
    future-version document — fail-closed, never a silent re-seed.
    """
    path = _index_path(profile)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Index(files={})
    except OSError as err:
        raise ReconcileStoreError(f"failed to read index for {profile}: {err}") from err
    try:
        return index_model.loads(text)
    except CorruptIndexError as err:
        raise CorruptIndexError(
            f"index for profile {profile!r} at {path}: {err}. "
            f"Inspect or delete it to rebuild."
        ) from err
    except IndexVersionError as err:
        raise IndexVersionError(
            f"index for profile {profile!r} at {path}: {err}"
        ) from err


def write_index(profile: str, index: Index) -> None:
    """Atomically write ``index/<profile>.json``. Call inside ``profile_lock``."""
    path = _index_path(profile)
    _mkdir_secure(path.parent)
    try:
        atomicio.atomic_write_text(path, index_model.dumps(index), mode=_FILE_MODE)
    except OSError as err:
        raise ReconcileStoreError(
            f"failed to write index for {profile}: {err}"
        ) from err


# --------------------------------------------------------------------------- #
# reconstruction + invariants
# --------------------------------------------------------------------------- #


def reconstruct(profile: str, fid: FileId) -> bytes | None | Absent:
    """Reconstruct the live content for ``fid``.

    In this storage layer the reconstruction operator is the identity:
    ``base + recorded-local`` collapses to the recorded local bytes (the
    hunk-granular ``+`` arrives with the future 3-way merge). Returns the same
    trichotomy as :func:`read_local`.
    """
    return read_local(profile, fid)


def verify(profile: str, fid: FileId | None = None) -> None:
    """Check INV-2 + INV-10 for ``fid`` (or every indexed file when ``None``).

    INV-2: the recorded-local bytes hash matches the index's ``local_hash``.
    INV-10: an index entry exists iff its on-disk local file/marker exists, with
    no orphan on either side. Additionally cross-checks the drafts store (via
    :func:`_verify_drafts`): the manifest's anchors must equal the entry's
    ``SHARED_DRAFTED`` hunk anchors (INV-10 analog) and each draft's bytes must
    hash to that hunk's recorded ``draft_hash`` (INV-2 analog) — so an orphan,
    missing, or tampered draft is caught too. Raises
    :class:`~setforge.errors.InvariantViolation` naming the profile, file-id, and
    invariant.
    """
    index = read_index(profile)
    if fid is None:
        targets = [file_id(key) for key in index.files]
        targets += [f for f in _on_disk_file_ids(profile) if f not in index.files]
    else:
        targets = [fid]
    for target in targets:
        _verify_one(profile, target, index.files.get(str(target)))


def _verify_one(profile: str, fid: FileId, entry: FileEntry | None) -> None:
    content, marker = _local_paths(profile, fid)
    on_disk = content.exists() or marker.exists()
    if entry is None:
        if on_disk:
            raise InvariantViolation(
                f"INV-10: {profile}/{fid} has local content with no index entry"
            )
        _verify_drafts(profile, fid, entry)  # catch an orphan drafts manifest too
        return
    if not on_disk:
        raise InvariantViolation(
            f"INV-10: {profile}/{fid} indexed but absent from the local store"
        )
    # present-flag must agree with the marker/content split.
    is_absent = marker.exists() and not content.exists()
    if entry.present == is_absent:
        raise InvariantViolation(
            f"INV-10: {profile}/{fid} index 'present' disagrees with the local store"
        )
    if entry.present:
        actual = content_sha(content.read_bytes())
        if actual != entry.local_hash:
            raise InvariantViolation(
                f"INV-2: {profile}/{fid} local bytes do not match recorded hash"
            )
    _verify_drafts(profile, fid, entry)


def _verify_drafts(profile: str, fid: FileId, entry: FileEntry | None) -> None:
    """INV-10/INV-2 analog for the drafts store: the manifest's anchors must equal
    the entry's ``SHARED_DRAFTED`` hunk anchors, and each draft's bytes must hash
    to that hunk's recorded ``draft_hash`` — so an orphan draft, a missing draft
    for a drafted row, or a tampered draft is caught fail-closed.
    """
    drafted = {
        str(row["anchor"]): str(row["draft_hash"])
        for row in (entry.hunks if entry is not None else [])
        if row.get("cls") == HunkClass.SHARED_DRAFTED.value
    }
    manifest = read_drafts(profile, fid)
    if set(manifest) != set(drafted):
        raise InvariantViolation(
            f"INV-10: {profile}/{fid} drafts manifest anchors do not match the "
            f"SHARED_DRAFTED hunk set"
        )
    for anchor, data in manifest.items():
        if content_sha(data) != drafted[anchor]:
            raise InvariantViolation(
                f"INV-2: {profile}/{fid} draft bytes for {anchor} do not match the "
                f"recorded draft_hash"
            )


def _on_disk_file_ids(profile: str) -> set[FileId]:
    """Every file-id with a recorded local content file, absence marker, or drafts
    manifest — so :func:`verify` catches an orphan draft (manifest with no index
    entry) too, not just an orphan local file.
    """
    found: set[FileId] = set()
    for root in (
        _local_root() / profile,
        _local_absent_root() / profile,
        _drafts_root() / profile,
    ):
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file():
                found.add(file_id(path.relative_to(root).as_posix()))
    return found


# --------------------------------------------------------------------------- #
# record (index-last) + prune (lock-scoped)
# --------------------------------------------------------------------------- #


def record(
    profile: str,
    fid: FileId,
    *,
    base: bytes,
    local: bytes | Absent,
    hunks: list[dict[str, object]] | None = None,
    drafts: dict[str, bytes] | None = None,
) -> None:
    """Record a base+local+drafts+index quad for ``fid``. Call inside ``profile_lock``.

    Writes the index entry **last** (after base, local, AND the drafts manifest)
    so a crash leaves a prunable orphan rather than an index pointing at content
    or a draft that was never written.

    ``hunks`` is the A5 per-hunk classification list; ``drafts`` is the A5c
    anchor→draft-bytes mapping for any ``SHARED_DRAFTED`` hunks. When either is
    ``None`` (the default, used by the non-staging ``install`` writeback) the
    existing entry's hunks / on-disk drafts are **preserved** — so a re-baseline
    never silently flattens a host's staged classifications or blessed drafts. An
    explicit ``hunks`` list overwrites them, and an explicit ``drafts`` mapping
    (the ``sync`` staging path) rewrites the manifest (empty ⇒ removed).
    """
    write_base(profile, fid, base)
    write_local(profile, fid, local)
    if drafts is not None:
        write_drafts(profile, fid, drafts)
    present = local is not ABSENT
    local_hash = content_sha(local) if isinstance(local, bytes) else None
    index = read_index(profile)
    files = dict(index.files)
    if hunks is None:
        prior = files.get(str(fid))
        hunks = list(prior.hunks) if prior is not None else []
    files[str(fid)] = FileEntry(present=present, local_hash=local_hash, hunks=hunks)
    write_index(profile, Index(files=files))


def prune(profile: str, live_fids: set[FileId]) -> None:
    """Drop stored base/local/index records not in ``live_fids``. Call inside
    ``profile_lock``. Never removes a listed file-id.
    """
    live_str = {str(f) for f in live_fids}
    # local content + absence-marker trees + drafts manifests
    for root in (
        _local_root() / profile,
        _local_absent_root() / profile,
        _drafts_root() / profile,
    ):
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.relative_to(root).as_posix() not in live_str:
                path.unlink(missing_ok=True)
    # base store (reuse its own prune)
    base_store.prune(profile, live_str)
    # index document
    index = read_index(profile)
    kept = {fid: entry for fid, entry in index.files.items() if fid in live_str}
    if kept != index.files:
        write_index(profile, Index(files=kept))
