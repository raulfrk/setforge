"""Schema migration registry + Migration Protocol for ``setforge migrate``.

A Migration is NOT just a schema-YAML edit. It is the FULL set of
local-file changes required to make the user's setforge installation
compatible with the target ``to_version``. That can include:

- ``setforge.yaml`` schema field renames / adds / removes.
- ``local.yaml`` field renames / adds / removes.
- Tracked-content edits (e.g., renaming a user-section marker
  namespace across every ``tracked/`` markdown file).
- Host-local state migrations (e.g., transition-record format changes
  under ``~/.local/share/setforge/``, cache-format bumps).
- Filesystem reorganizations (e.g., move ``~/.config/setforge/`` →
  ``~/.config/setforge/v2/``).

Every Migration declares its full set of :meth:`Migration.affected_paths`
so the ``migrate`` CLI's backup + multi-file diff preview + atomic
rollback cover the whole footprint, not just ``setforge.yaml``.

The current registry :data:`MIGRATIONS` is empty — the Protocol +
registry shape work today; the first real migration ships in v0.3.0
and is appended here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from ruamel.yaml import YAML

from setforge.migrations._yaml_ops import atomic_write_yaml, yaml_rt

__all__ = [
    "MIGRATIONS",
    "ManifestEntry",
    "ManifestType",
    "Migration",
    "MigrationRoots",
    "VersionStampMigration",
    "current_expected_schema_version",
    "detect_current_schema",
    "find_migration_path",
]


current_expected_schema_version: Final[str] = "1.1"
"""Schema version this build of setforge expects.

When the user's ``setforge.yaml`` declares (or defaults to) a different
version, ``setforge migrate --check`` lists the chain in
:data:`MIGRATIONS` that bridges the gap. Bumped manually when a
breaking schema change ships; the matching :class:`Migration` is
appended to :data:`MIGRATIONS` in the same release.
"""


_DEFAULT_SCHEMA_VERSION: Final[str] = "1.0"
"""Default returned by :func:`detect_current_schema` when the YAML
file has no ``schema_version:`` key (every pre-1.1 setforge.yaml)."""


class ManifestType(StrEnum):
    """One-character render symbol for a :class:`ManifestEntry`.

    Used by the ``setforge migrate --check`` report and the ``--apply``
    diff-preview header to mark the kind of change each entry
    represents. ``+`` add and ``~`` rename pair with the dedicated
    Unicode MINUS SIGN (U+2212) for remove — distinct from the ASCII
    HYPHEN-MINUS so a glance at the manifest separates removals from
    bullet-style hyphens or rename markers.
    """

    ADD = "+"
    """New field / new file."""

    RENAME = "~"
    """Field rename / file move."""

    REMOVE = "−"  # noqa: RUF001 — U+2212 MINUS SIGN, intentional (see docstring).
    """Field removed / file deleted."""

    EDIT = "M"
    """In-place content edit (e.g. tracked-file sentinel rewrite)."""

    NOTE = " "
    """Informational entry — no concrete file change attached."""


@dataclass(slots=True, frozen=True)
class ManifestEntry:
    """One line in a :meth:`Migration.manifest` listing.

    Rendered verbatim under ``setforge migrate --check`` as
    ``<type-symbol> <description>``. ``affected_path`` is set when the
    entry pertains to a specific file the migration will mutate —
    powers per-file backups and the multi-file diff preview.
    """

    type: ManifestType
    description: str
    affected_path: Path | None = None


@dataclass(slots=True, frozen=True)
class MigrationRoots:
    """Filesystem roots a :class:`Migration` may touch.

    Passed to every method on the Protocol so migrations can reach any
    of the local-file surfaces enumerated in the module docstring
    without re-resolving paths.

    Attributes:
        cfg_path: the resolved ``setforge.yaml`` (typically
            ``<repo_root>/setforge.yaml``).
        repo_root: the user's setforge-config repo (parent of
            ``cfg_path``). Tracked content lives under this root.
        home: ``Path.home()`` — for ``~/.config/setforge/``,
            ``~/.local/share/setforge/``, ``~/.claude/``, and any
            other host-local state a migration touches.
    """

    cfg_path: Path
    repo_root: Path
    home: Path


@runtime_checkable
class Migration(Protocol):
    """Structural Protocol every concrete migration implements.

    Implementations are expected to be ``@dataclass(slots=True,
    frozen=True)`` instances appended to :data:`MIGRATIONS`. The
    Protocol covers the FULL set of local-file changes for a single
    version bump — schema YAML edits, ``local.yaml`` edits,
    tracked-content edits, host-local state migrations — not just
    ``setforge.yaml``.
    """

    @property
    def from_version(self) -> str:
        """Source schema version this migration upgrades from."""
        ...

    @property
    def to_version(self) -> str:
        """Target schema version this migration upgrades to."""
        ...

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        """Per-migration manifest. Powers ``migrate --check``.

        Implementations MUST list every file the :meth:`apply` step
        will mutate (one entry per affected file, type ``EDIT`` /
        ``RENAME`` / ``REMOVE``). The ``ADD`` / ``NOTE`` symbols cover
        non-file-bound entries (new fields, informational lines).
        """
        ...

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        """Every absolute path this migration will read / write / delete.

        Drives three pieces of the ``migrate --apply`` flow:

        1. Pre-state snapshot for atomic rollback on partial failure.
        2. Per-file backups (``<file>.pre-<to_version>.bak``) when the
           user picks ``APPLY_WITH_BACKUP``.
        3. The multi-file diff preview shown before confirmation.

        Order matters only for the diff preview.
        """
        ...

    def apply(self, *, roots: MigrationRoots) -> None:
        """Mutate every necessary file for full ``v(to_version)`` compatibility.

        Includes setforge.yaml edits, local.yaml edits, tracked-content
        edits, host-local state migrations — whatever the target
        version needs. Implementations MUST use
        :mod:`setforge.migrations._yaml_ops` for YAML edits (preserves
        comments + key order per research brief §4) and atomic per-file
        writes via :func:`setforge.migrations._fs_ops.atomic_replace`.
        Implementations SHOULD be idempotent on partial replay (a
        half-applied migration that gets re-run converges).
        """
        ...


@dataclass(slots=True, frozen=True)
class VersionStampMigration:
    """Thin version-stamp migration — the first real schema bump (1.0 → 1.1).

    EXPAND step, not the breaking 1.0 → 2.0 contract. ``apply`` stamps a
    single ``schema_version`` key into ``setforge.yaml`` and changes
    nothing else: the disposition / spans surfaces are already additive
    and optional under 1.0, so no data reshape is needed
    (identity-on-data). The write goes through a SINGLE
    :func:`atomic_write_yaml`, so a partial-write never leaves a
    version-bump-without-reshape skew on disk.

    The stamp is overwrite-or-insert and therefore idempotent on replay:
    re-applying converges (no ``rename_key``-style raise-on-absent).

    Reverse: :attr:`reverse` returns the inverse 1.1 → 1.0 migration,
    which simply removes the ``schema_version`` key. The reverse is
    intentionally NOT registered in :data:`MIGRATIONS` (which
    :func:`find_migration_path` walks FORWARD) — a 1.1 → 1.0 forward
    entry would create a 1.0 ↔ 1.1 cycle. Because ``down`` removes the
    very key ``up`` inserts, ``down → up → down`` on a config that had no
    ``schema_version`` key restores its absence byte-for-byte.
    """

    from_version: str = "1.0"
    to_version: str = "1.1"

    @property
    def reverse(self) -> _VersionStampReverse:
        """The inverse 1.1 → 1.0 migration that strips the stamp."""
        return _VersionStampReverse(
            from_version=self.to_version, to_version=self.from_version
        )

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        """Single-file stamp: an ADD of the ``schema_version`` key."""
        return (
            ManifestEntry(
                type=ManifestType.ADD,
                description=f"stamp schema_version: {self.to_version!r}",
                affected_path=roots.cfg_path,
            ),
        )

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        """Only ``setforge.yaml`` is touched."""
        return (roots.cfg_path,)

    def apply(self, *, roots: MigrationRoots) -> None:
        """Stamp ``schema_version: <to_version>`` via a single atomic write."""
        yaml = yaml_rt()
        with roots.cfg_path.open("r", encoding="utf-8") as fh:
            data = yaml.load(fh)
        # Overwrite-or-insert — idempotent on replay (B-M2). Writes
        # exactly the schema_version key (extra="forbid"-safe, B-M3).
        data["schema_version"] = self.to_version
        atomic_write_yaml(roots.cfg_path, data)


@dataclass(slots=True, frozen=True)
class _VersionStampReverse:
    """Inverse of :class:`VersionStampMigration` — strips the ``schema_version`` key.

    NOT a forward-registry entry (see :class:`VersionStampMigration`).
    ``apply`` removes the ``schema_version`` key when present and is a
    no-op when absent, so the original key-absent baseline is restored.
    """

    from_version: str = "1.1"
    to_version: str = "1.0"

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        return (
            ManifestEntry(
                type=ManifestType.REMOVE,
                description="strip schema_version stamp",
                affected_path=roots.cfg_path,
            ),
        )

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        return (roots.cfg_path,)

    def apply(self, *, roots: MigrationRoots) -> None:
        """Remove the ``schema_version`` key via a single atomic write.

        No-op on absence (never raise-on-absent), so down→up→down on a
        key-absent config restores its absence.
        """
        yaml = yaml_rt()
        with roots.cfg_path.open("r", encoding="utf-8") as fh:
            data = yaml.load(fh)
        if "schema_version" in data:
            del data["schema_version"]
        atomic_write_yaml(roots.cfg_path, data)


MIGRATIONS: Final[tuple[Migration, ...]] = (VersionStampMigration(),)
"""Ordered registry of available FORWARD migrations.

Holds the first real migration (version-stamp 1.0 → 1.1). Future
migrations are appended in ``from_version`` order so
:func:`find_migration_path` can walk the chain forward. The reverse of
each migration is attached to the forward instance (e.g.
:attr:`VersionStampMigration.reverse`) and is deliberately NOT a member
of this tuple — a backward entry would make the forward walk cycle.
"""


def detect_current_schema(yaml_path: Path) -> str:
    """Read ``schema_version:`` from ``yaml_path``; default ``"1.0"`` on absence.

    Uses a round-trip ruamel load so the call works against the same
    YAML shape the rest of setforge round-trips. Missing file, empty
    file, or YAML without a top-level ``schema_version`` key all
    resolve to :data:`_DEFAULT_SCHEMA_VERSION` (= ``"1.0"``) — the
    pre-versioning baseline every existing user is implicitly on.
    """
    if not yaml_path.exists():
        return _DEFAULT_SCHEMA_VERSION
    yaml = YAML(typ="rt")
    with yaml_path.open("r", encoding="utf-8") as fh:
        data = yaml.load(fh)
    if data is None:
        return _DEFAULT_SCHEMA_VERSION
    raw = data.get("schema_version")
    if raw is None:
        return _DEFAULT_SCHEMA_VERSION
    return str(raw)


def find_migration_path(*, from_v: str, to_v: str) -> tuple[Migration, ...]:
    """Walk :data:`MIGRATIONS` to find a chain from ``from_v`` to ``to_v``.

    Returns an empty tuple when ``from_v == to_v`` (nothing to do) or
    when no chain bridges the two versions in the current registry
    (the v0.2.0 empty-registry steady state).

    Implementation: greedy forward walk — at each step, pick the
    migration whose ``from_version`` matches the current cursor. The
    registry is expected to be ordered linearly; branching version
    graphs are explicitly out of scope until a real migration ships
    that needs them.
    """
    if from_v == to_v:
        return ()
    chain: list[Migration] = []
    cursor = from_v
    for _ in range(len(MIGRATIONS) + 1):
        if cursor == to_v:
            return tuple(chain)
        match = next(
            (m for m in MIGRATIONS if m.from_version == cursor),
            None,
        )
        if match is None:
            return ()
        chain.append(match)
        cursor = match.to_version
    return ()
