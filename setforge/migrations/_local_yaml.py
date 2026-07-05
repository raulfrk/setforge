"""Dedicated schema logic for ``~/.config/setforge/local.yaml``.

``local.yaml`` carries its OWN ``schema_version`` — a separate contract
surface from ``setforge.yaml`` (different baseline, different migration
chain). Keeping all of local.yaml's detect / guard / migrate logic in
this one module (rather than weaving it into :mod:`setforge.source` or
:mod:`setforge.config`) keeps the seam thin: the loaders only CALL these
helpers.

The three helpers mirror their ``setforge.yaml`` counterparts so the two
surfaces stay behaviorally aligned:

- :func:`detect_local_yaml_schema` ↔
  :func:`setforge.migrations.detect_current_schema` — a RAW round-trip
  read of ``schema_version`` (never through the ``extra="forbid"`` model,
  so a cross-major doc is readable for its version BEFORE strict
  validation would reject its shape).
- :func:`guard_local_yaml_schema` ↔
  :func:`setforge.config._guard_schema_version` — a major-compare against
  the local baseline; a newer MAJOR refuses cleanly via
  :class:`~setforge.errors.ConfigError` (the CLI handler turns that into a
  one-line message + nonzero exit, never a traceback). A malformed
  version raises ``ConfigError`` via
  :func:`~setforge.migrations.parse_schema_version`.
- :func:`migrate_local_yaml` wraps
  :func:`setforge.overlay_migration.migrate_local_yaml_overlay_spans`
  (the ``host_local_sections`` → OVERLAY spans rewrite), version-gated by
  the detected baseline. ruamel round-trip fidelity + atomicity +
  idempotency are inherited from that delegate.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Final

from ruamel.yaml import YAML

from setforge.errors import ConfigError
from setforge.migrations import _require_mapping_root, parse_schema_version
from setforge.overlay_migration import (
    OverlayMigrationResult,
    migrate_local_yaml_overlay_spans,
)

__all__ = [
    "LOCAL_YAML_BASELINE_VERSION",
    "OverlayMigrationResult",
    "detect_local_yaml_schema",
    "guard_local_yaml_schema",
    "migrate_local_yaml",
    "strip_retired_keys",
]

LOCAL_YAML_BASELINE_VERSION: Final[str] = "1.0"
"""Baseline schema version for ``local.yaml``.

The implicit version every pre-versioning ``local.yaml`` is on — returned
by :func:`detect_local_yaml_schema` when the file is absent, empty, or has
no ``schema_version`` key. Deliberately a SEPARATE constant from
:data:`setforge.schema_manifest.SCHEMA_MAJOR` (setforge.yaml's): the two
documents version independently.
"""


def detect_local_yaml_schema(path: Path) -> str:
    """Read ``schema_version`` from ``local.yaml``; baseline on absence.

    Uses a RAW round-trip ruamel read — NOT the ``extra="forbid"``
    :class:`~setforge.local_config.LocalConfig` model — so a cross-major
    or retired-key document is still readable for its version BEFORE
    strict validation would reject its shape. Mirrors
    :func:`setforge.migrations.detect_current_schema`.

    Missing file, empty file, or a document with no top-level
    ``schema_version`` key all resolve to
    :data:`LOCAL_YAML_BASELINE_VERSION`. Raises
    :class:`~setforge.errors.ConfigError` (via :func:`_require_mapping_root`)
    when the root is a non-mapping, and may propagate a ruamel
    ``YAMLError`` if the file is not well-formed YAML.
    """
    if not path.exists():
        return LOCAL_YAML_BASELINE_VERSION
    yaml = YAML(typ="rt")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.load(fh)
    if data is None:
        return LOCAL_YAML_BASELINE_VERSION
    data = _require_mapping_root(data, path)
    raw = data.get("schema_version")
    if raw is None:
        return LOCAL_YAML_BASELINE_VERSION
    return str(raw)


def guard_local_yaml_schema(data: object, path: Path) -> None:
    """Refuse a cross-major-newer ``local.yaml`` cleanly, before validation.

    Reads ``schema_version`` from the RAW mapping (baseline on absence),
    parses it semantically, and compares MAJORS against
    :data:`LOCAL_YAML_BASELINE_VERSION`:

    - newer MAJOR → :class:`~setforge.errors.ConfigError` ("upgrade
      setforge") — a clean, traceback-free refusal the CLI handler turns
      into a one-line message + nonzero exit. The engine never best-effort
      reads a ``local.yaml`` whose major it does not understand.
    - same major (newer minor or older) → proceed; the migration +
      forward-tolerant validation handle the in-major window.

    Mirrors :func:`setforge.config._guard_schema_version`. Does NOT call
    ``sys.exit`` — it raises ``ConfigError`` and lets the CLI handler exit.
    A malformed ``schema_version`` flows through
    :func:`~setforge.migrations.parse_schema_version` → ``ConfigError``
    (never a bare ``ValueError`` / ``IndexError``).
    """
    raw = data.get("schema_version") if isinstance(data, Mapping) else None
    detected = str(raw) if raw is not None else LOCAL_YAML_BASELINE_VERSION
    detected_major = parse_schema_version(detected)[0]
    baseline_major = parse_schema_version(LOCAL_YAML_BASELINE_VERSION)[0]
    if detected_major > baseline_major:
        raise ConfigError(
            f"{path}: local.yaml schema_version {detected!r} requires a newer "
            f"setforge (this build supports local.yaml schema "
            f"{LOCAL_YAML_BASELINE_VERSION!r}); upgrade setforge to "
            f">= {detected_major}.0 to read this config"
        )


def migrate_local_yaml(path: Path) -> OverlayMigrationResult:
    """Run version-gated local.yaml migrations on ``path`` in place.

    Detects the local.yaml schema version, then delegates the
    ``host_local_sections`` → OVERLAY spans rewrite to
    :func:`setforge.overlay_migration.migrate_local_yaml_overlay_spans`.
    That delegate is a presence-check: a file with no retired key is left
    byte-for-byte untouched (``migrated=False``), so this wrapper is
    idempotent — re-running converges. ruamel round-trip fidelity, atomic
    write, and mode preservation are inherited from the delegate.

    Returns the delegate's :class:`OverlayMigrationResult` so the caller
    can tell whether the file was rewritten and reload accordingly.
    """
    # Today the only registered transform is the baseline-major span
    # rewrite, which is itself a presence-check (idempotent, byte-exact on
    # a clean file), so no explicit version gate is needed yet. A future
    # in-major migration would branch here on
    # ``detect_local_yaml_schema(path)`` before delegating.
    return migrate_local_yaml_overlay_spans(path)


def _strip_tracked_file(tracked_file: MutableMapping[str, object]) -> bool:
    """Strip one tracked_file's retired span-declaration keys in memory.

    Drops the whole ``host_local_sections`` block AND the whole ``spans``
    key (all entries, not just OVERLAY ones): the strict
    :class:`setforge.source._LocalTrackedFileOverlay` model no longer
    declares EITHER field, so any surviving entry would trip
    ``extra_forbidden``. Operates on the plain mappings a
    ``YAML(typ="safe")`` load yields; mutates ``tracked_file`` in place.
    Returns whether anything was removed.
    """
    changed = False
    if tracked_file.pop("host_local_sections", None) is not None:
        changed = True
    if tracked_file.pop("spans", None) is not None:
        changed = True
    return changed


def strip_retired_keys(data: object) -> bool:
    """Strip retired local.yaml span-declaration keys in ``data``; report if any.

    The IN-MEMORY forward-tolerance step: it removes the retired
    ``host_local_sections`` / OVERLAY ``spans`` keys from each
    ``tracked_files.<id>`` so the strict
    :class:`~setforge.local_config.LocalConfig` /
    ``_LocalSourceConfig`` model accepts a pre-4.0 document (degraded — the
    host-local span-declaration surface is no longer read from
    ``local.yaml``; it lives in the reconcile store now), WITHOUT touching
    disk. The on-disk retirement is owned by the 3.0→4.0 span-surface-retire
    migration, which snapshots the pre-migration bytes first so ``revert``
    restores them byte-for-byte; mutating the file here would race that
    snapshot.

    Returns ``True`` when at least one key was stripped. A document with no
    retired key is left untouched (returns ``False``).
    """
    if not isinstance(data, MutableMapping):
        return False
    tracked_files = data.get("tracked_files")
    if not isinstance(tracked_files, Mapping):
        return False
    changed = False
    for tracked_file in tracked_files.values():
        if isinstance(tracked_file, MutableMapping):
            changed |= _strip_tracked_file(tracked_file)
    return changed
