"""Where the retired forked-scalar base manifests live on disk.

The store itself was retired with the disposition model at schema 3.0. One
JSON manifest per ``(profile, file-id)`` used to sit at
``<state_root>/scalar-base/<profile>/<file-id>.json``; the path is still needed
to restore a pre-3.0 transition's snapshot and to remove the manifests during
the 2.1 -> 3.0 migration.
"""

from pathlib import Path

from setforge.errors import BaseStoreError, ReconcileStoreError
from setforge.paths import state_root
from setforge.reconcile.types import resolve_store_path


def scalar_base_root() -> Path:
    """Root directory holding every profile's scalar-base manifests."""
    return state_root() / "scalar-base"


def manifest_path(profile: str, file_id: str) -> Path:
    """Return the on-disk manifest path for ``(profile, file_id)``.

    Applies the store-wide path guard
    (:func:`setforge.reconcile.types.resolve_store_path`), so restoring an old
    transition or unlinking a manifest during migration never touches a path
    outside ``scalar-base/<profile>/``.
    """
    try:
        return resolve_store_path(scalar_base_root(), profile, file_id, suffix=".json")
    except ReconcileStoreError as err:
        raise BaseStoreError(str(err)) from err
