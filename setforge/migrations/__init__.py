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

# ruamel.yaml ships py.typed without resolvable annotations; no stub pkg on PyPI.
from ruamel.yaml import YAML  # type: ignore[import-not-found]

__all__ = [
    "MIGRATIONS",
    "ManifestEntry",
    "ManifestType",
    "Migration",
    "MigrationRoots",
    "current_expected_schema_version",
    "detect_current_schema",
    "find_migration_path",
]


current_expected_schema_version: Final[str] = "1.0"
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
    represents. Mirrors git-diff conventions (``+`` add, ``−`` remove)
    plus dedicated symbols for renames and in-place content edits.
    """

    ADD = "+"
    """New field / new file."""

    RENAME = "~"
    """Field rename / file move."""

    REMOVE = "−"
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


MIGRATIONS: Final[tuple[Migration, ...]] = ()
"""Ordered registry of available migrations.

Empty in v0.2.0 — the Protocol + registry shape ship first; the first
real migration appends here in v0.3.0. Future migrations are appended
in ``from_version`` order so :func:`find_migration_path` can walk the
chain forward.
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
