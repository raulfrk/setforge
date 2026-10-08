"""Per-host stored-base bytes store: the merge ancestor for tracked files.

This module persists the *verbatim last-deployed bytes* of every tracked
file, keyed per profile, under ``<state_root>/base/<profile>/<file-id>``.
The ``<file-id>`` is the tracked relative path mirrored as a subtree
(e.g. ``claude/CLAUDE.md`` -> ``base/<profile>/claude/CLAUDE.md``).

These bytes are the common ancestor a three-way merge needs to tell
"the user edited live" apart from "tracked moved upstream". The store
is bytes-only: what is written is read back byte-for-byte, with no
encoding, decoding, or newline translation.

Base-lifecycle contract
------------------------
1. **First-install seeding.** The base is seeded with the *upstream
   (tracked) bytes*, never the live file's bytes and never a
   merge-result. Seeding ``base == upstream`` makes the very first
   three-way merge a no-op against tracked, which preserves any
   pre-existing live edits instead of clobbering them.
2. **Mandatory re-baselining.** After *every* deploy of a tracked file
   — including conflict-resolve and auto-take outcomes — the base MUST
   be rewritten to exactly the bytes that landed live. Skipping this
   makes the next merge diff against a stale ancestor and resurrect
   already-resolved conflicts. :func:`write_base` is the single op for
   both seeding and re-baselining.
3. **Plain-string profile keying.** The base is keyed by the plain
   profile-name string. Renaming an *intermediate* profile therefore
   orphans its old subtree and yields a stale-base (full-content) merge
   on the next install under the new name. This is a known, accepted
   limitation — it degrades to a noisier merge, never a crash.

Revert note: ``setforge revert`` MUST roll the stored base back in
lockstep with the live file it restores, so the post-revert state is a
consistent (live, base) pair. That wiring is owned by the revert
integration, not this module. The scope here is the store primitive plus
the contract documented above — no deploy/install/revert wiring lives
in this file.
"""

from pathlib import Path

from setforge import atomicio, base_store_format
from setforge.errors import BaseStoreError, BaseStoreIOError, ReconcileStoreError
from setforge.paths import state_root


def base_root() -> Path:
    """Root directory holding every profile's stored-base subtree."""
    return state_root() / "base"


def _profile_root(profile: str) -> Path:
    """Resolved root of ``profile``'s stored-base subtree.

    Rejects a ``profile`` that is not a single safe directory name (a path
    separator, ``.``/``..``, empty, or a control character).
    """
    # Imported lazily: ``setforge.reconcile`` imports this module at load time.
    from setforge.reconcile.types import check_profile_name

    try:
        check_profile_name(profile)
    except ReconcileStoreError as err:
        raise BaseStoreError(str(err)) from err
    return (base_root() / profile).resolve()


def _resolve_target(profile: str, file_id: str) -> Path:
    """Map ``(profile, file_id)`` to its on-disk base path, guarding traversal.

    Applies the store-wide path guard
    (:func:`setforge.reconcile.types.resolve_store_path`) to both segments, so
    a malicious or buggy profile or file-id can never touch a base outside
    ``base/<profile>/``.
    """
    # Imported lazily: ``setforge.reconcile`` imports this module at load time.
    from setforge.reconcile.types import resolve_store_path

    try:
        return resolve_store_path(base_root(), profile, file_id)
    except ReconcileStoreError as err:
        raise BaseStoreError(str(err)) from err


def base_path(profile: str, file_id: str) -> Path:
    """Return the on-disk base path for ``(profile, file_id)``.

    Public so the transition state-snapshot integration can capture and
    restore the stored base alongside the spans sidecar and scalar-base
    manifests (Invariant I5: live + base + sidecar roll back
    in lockstep). Applies the same traversal guard as the read/write
    entry points.
    """
    return _resolve_target(profile, file_id)


def read_base(profile: str, file_id: str) -> bytes | None:
    """Return the stored base bytes for ``file_id`` under ``profile``.

    Returns ``None`` when no base has been stored (the file is absent).
    A legitimately empty deployed file returns ``b""`` — absence is
    distinguished from emptiness via the filesystem, never truthiness or
    byte-length.
    """
    base_store_format.check_format_version(_profile_root(profile))
    target = _resolve_target(profile, file_id)
    try:
        return target.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as err:
        raise BaseStoreIOError(
            f"failed to read base for {profile}/{file_id}: {err}"
        ) from err


def write_base(profile: str, file_id: str, data: bytes) -> None:
    """Atomically store ``data`` as the base for ``file_id`` under ``profile``.

    Used for both first-install seeding and post-deploy re-baselining —
    the operation is identical. Rejects an unsafe ``file_id`` before
    touching disk. Writes durably via
    :func:`setforge.atomicio.atomic_write_bytes`.
    """
    target = _resolve_target(profile, file_id)
    try:
        atomicio.atomic_write_bytes(target, data)
    except OSError as err:
        raise BaseStoreIOError(
            f"failed to write base for {profile}/{file_id}: {err}"
        ) from err
    try:
        base_store_format.stamp_format_version(_profile_root(profile))
    except OSError as err:
        raise BaseStoreIOError(
            f"failed to stamp base-store format version for {profile}: {err}"
        ) from err


def list_base_ids(profile: str) -> set[str]:
    profile_root = _profile_root(profile)
    if not profile_root.is_dir():
        return set()
    ids: set[str] = set()
    for path in profile_root.rglob("*"):
        if not path.is_file():
            continue
        file_id = path.relative_to(profile_root).as_posix()
        if file_id == base_store_format.SIDECAR_NAME:
            continue
        ids.add(file_id)
    return ids


def prune(profile: str, live_file_ids: set[str]) -> None:
    """Remove stored bases under ``profile`` not in ``live_file_ids``.

    Strictly profile-scoped: only files under ``base/<profile>/`` are
    considered, so another profile's subtree is never touched. The
    file-id of each on-disk base is derived the same way
    :func:`write_base` keys it (relative POSIX path under the profile
    root); a base whose derived id is not in ``live_file_ids`` is
    unlinked.
    """
    profile_root = _profile_root(profile)
    if not profile_root.is_dir():
        return
    try:
        for path in profile_root.rglob("*"):
            if not path.is_file():
                continue
            file_id = path.relative_to(profile_root).as_posix()
            # The format-version sidecar lives at the profile root and is
            # store metadata, never a tracked-file base — never prune it.
            if file_id == base_store_format.SIDECAR_NAME:
                continue
            if file_id not in live_file_ids:
                path.unlink()
    except OSError as err:
        raise BaseStoreIOError(f"failed to prune bases for {profile}: {err}") from err
