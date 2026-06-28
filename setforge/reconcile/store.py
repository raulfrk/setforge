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

import hashlib
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
from setforge.reconcile.types import ABSENT, Absent, FileId, file_id
from setforge.transitions import state_root

_DIR_MODE = 0o700
_FILE_MODE = 0o600

__all__ = [
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


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


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
    no orphan on either side. Raises :class:`~setforge.errors.InvariantViolation`
    naming the profile, file-id, and invariant.
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
        actual = _sha(content.read_bytes())
        if actual != entry.local_hash:
            raise InvariantViolation(
                f"INV-2: {profile}/{fid} local bytes do not match recorded hash"
            )


def _on_disk_file_ids(profile: str) -> set[FileId]:
    """Every file-id with a recorded local content file or absence marker."""
    found: set[FileId] = set()
    for root in (_local_root() / profile, _local_absent_root() / profile):
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
) -> None:
    """Record a base + local + index triple for ``fid``. Call inside ``profile_lock``.

    Writes the index entry **last** so a crash leaves a prunable orphan base
    rather than an index pointing at content that was never written.

    ``hunks`` is the A5 per-hunk classification list. When ``None`` (the default,
    used by the non-staging ``install`` writeback) the existing entry's hunks are
    **preserved** — so a re-baseline never silently flattens a host's staged
    classifications. An explicit list overwrites them (the ``sync`` staging path
    passes a freshly-classified set).
    """
    write_base(profile, fid, base)
    write_local(profile, fid, local)
    present = local is not ABSENT
    local_hash = _sha(local) if isinstance(local, bytes) else None
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
    # local content + absence-marker trees
    for root in (_local_root() / profile, _local_absent_root() / profile):
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
