"""Where the retired forked-scalar base manifests live on disk.

The store itself was retired with the disposition model at schema 3.0. One
JSON manifest per ``(profile, file-id)`` used to sit at
``<state_root>/scalar-base/<profile>/<file-id>.json``; the path is still needed
to restore a pre-3.0 transition's snapshot and to remove the manifests during
the 2.1 -> 3.0 migration.
"""

from pathlib import Path

from setforge.errors import BaseStoreError
from setforge.paths import state_root


def scalar_base_root() -> Path:
    """Root directory holding every profile's scalar-base manifests."""
    return state_root() / "scalar-base"


def _profile_root(profile: str) -> Path:
    """Resolved root of ``profile``'s scalar-base subtree."""
    return (scalar_base_root() / profile).resolve()


def _resolve_target(profile: str, file_id: str) -> Path:
    """Map ``(profile, file_id)`` to its manifest path, guarding traversal.

    Rejects a ``file_id`` that is absolute or contains a ``..``
    component, and verifies the resolved manifest stays within the
    profile's subtree, so a malicious or buggy file-id can never write a
    manifest outside ``scalar-base/<profile>/``.
    """
    candidate = Path(file_id)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise BaseStoreError(
            f"unsafe file-id {file_id!r}: must be a relative path with no "
            "'..' components"
        )
    profile_root = _profile_root(profile)
    target = (profile_root / f"{candidate}.json").resolve()
    if profile_root not in target.parents:
        raise BaseStoreError(
            f"file-id {file_id!r} resolves outside scalar-base/{profile}/"
        )
    return target


def manifest_path(profile: str, file_id: str) -> Path:
    """Return the on-disk manifest path for ``(profile, file_id)``.

    Guards traversal, so restoring an old transition or unlinking a
    manifest during migration never touches a path outside
    ``scalar-base/<profile>/``.
    """
    return _resolve_target(profile, file_id)
