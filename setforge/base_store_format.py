"""Payload-agnostic format-version sidecar for the base store.

The per-host stored-base store (:mod:`setforge.base_store`) persists a merge
ancestor whose on-disk format could change in a future release. Without a
marker, a future-format ancestor would be silently mis-parsed. This module
adds one ``.format-version`` sidecar per *store root* (a ``Path``) recording
the ``MAJOR.MINOR`` format the writer used, and a refuse-on-mismatch check.

The helper knows nothing about the payload and only ever touches
``root/.format-version``. The store calls :func:`check_format_version` before
reading and :func:`stamp_format_version` from its write path.

Grandfather / refuse contract
-----------------------------
* A *truly absent* sidecar (``FileNotFoundError``) is grandfathered:
  pre-versioning roots — first-run and legacy-data alike — read as the
  current version and are lazily stamped on the next write.
* A *present-but-unparseable* sidecar, or any other ``OSError`` reading
  it, is refused with :class:`BaseStoreSchemaError` — never grandfathered,
  so corruption can never masquerade as "no sidecar".
* A present sidecar whose recorded version is not byte-for-byte the
  accepted version (tuple-compared via
  :func:`setforge.migrations.parse_schema_version`) is refused. There is
  exactly one accepted format today; any other tuple — higher OR lower —
  refuses.

The lazy v1 stamp is written from the write path only (where the
single-writer-install invariant holds), atomically via
:func:`setforge.atomicio.atomic_write_text`; reads stay side-effect-free.
"""

from pathlib import Path
from typing import Final

from setforge import atomicio
from setforge.errors import BaseStoreSchemaError, ConfigError
from setforge.migrations import parse_schema_version

SIDECAR_NAME: Final[str] = ".format-version"
"""Filename of the per-root format-version sidecar."""

BASE_STORE_FORMAT_VERSION: Final[str] = "1.0"
"""The on-disk base-store format this engine reads and writes.

A ``MAJOR.MINOR`` token, independent of
:data:`setforge.migrations.current_expected_schema_version` (the *config*
schema): the base-store payload format evolves on its own cadence. Bumped
manually when the store layout changes incompatibly.
"""


def check_format_version(root: Path) -> None:
    """Refuse if ``root``'s sidecar records a version this engine does not write.

    Reads ``root/.format-version`` and tuple-compares its recorded version
    against :data:`BASE_STORE_FORMAT_VERSION`. A missing sidecar is
    grandfathered (returns silently). A present-but-unreadable or unparseable
    sidecar, or a version that does not equal it, raises
    :class:`BaseStoreSchemaError`. Side-effect-free: never writes.
    """
    sidecar = root / SIDECAR_NAME
    try:
        recorded = sidecar.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return
    except OSError as err:
        raise BaseStoreSchemaError(
            f"cannot read base-store format sidecar at {sidecar}: {err}; "
            f"delete {root} to re-grandfather it (next merge is noisier)"
        ) from err
    try:
        found = parse_schema_version(recorded)
    except ConfigError as err:
        raise BaseStoreSchemaError(
            f"unparseable base-store format version {recorded!r} at {sidecar}: "
            f"{err}; delete {root} to re-grandfather it (next merge is noisier)"
        ) from err
    if found != parse_schema_version(BASE_STORE_FORMAT_VERSION):
        raise BaseStoreSchemaError(
            f"incompatible base-store format at {root}: found {recorded!r}, "
            f"this engine writes {BASE_STORE_FORMAT_VERSION!r}; delete {root} "
            "to re-grandfather it (next merge is noisier)"
        )


def stamp_format_version(root: Path) -> None:
    """Atomically record :data:`BASE_STORE_FORMAT_VERSION` in ``root``'s sidecar.

    Idempotent: re-writing the same version yields identical content. Call
    from the write path only — the single-writer-install invariant makes
    the benign-race write safe. Uses
    :func:`setforge.atomicio.atomic_write_text` (fsync-data, replace, parent
    fsync), so no ``.<name>.tmp`` debris survives a successful write.
    """
    atomicio.atomic_write_text(root / SIDECAR_NAME, BASE_STORE_FORMAT_VERSION + "\n")
