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

The registry :data:`MIGRATIONS` holds the first real migration
(version-stamp 1.0 → 1.1, :class:`VersionStampMigration`). Future
migrations are appended in ``from_version`` order so
:func:`find_migration_path` can walk the chain forward.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from setforge.errors import ConfigError
from setforge.migrations._yaml_ops import atomic_write_yaml, yaml_rt

# A schema version is exactly ``MAJOR.MINOR`` — two non-negative integer
# components. This is stricter than the ``--pin`` token (which tolerates
# ``X.Y.Z``): schema versions are the engine's own contract surface, so
# the format is pinned. Rejecting ``"1"`` / ``"1.2.3"`` / ``""`` / ``"v2"``
# here is what keeps the forward-tolerant gate and the migration
# path-finder from leaking a ``ValueError``/``IndexError`` traceback on a
# malformed ``schema_version``.
_SCHEMA_VERSION_RE: Final = re.compile(r"^\d+\.\d+$")


def parse_schema_version(raw: str) -> tuple[int, int]:
    """Parse a ``MAJOR.MINOR`` schema version into an ``(int, int)`` tuple.

    The tuple form is what makes version comparison *semantic* rather than
    lexical: ``parse_schema_version("1.10") > parse_schema_version("1.9")``
    is ``True`` (a string sort gets this wrong). ``[0]`` is the major.

    Raises :class:`ConfigError` — never a bare ``ValueError`` /
    ``IndexError`` — on any value that is not exactly two dot-separated
    integers (``"1"``, ``"1.2.3"``, ``""``, ``"v2"``, ``"2.0.0"`` …), so
    callers get a clean, traceback-free message.
    """
    if _SCHEMA_VERSION_RE.fullmatch(raw) is None:
        raise ConfigError(
            f"malformed schema_version {raw!r}: expected MAJOR.MINOR "
            f"(two integers, e.g. '1.0')"
        )
    major, minor = raw.split(".")
    return (int(major), int(minor))


def _require_mapping_root(data: object, yaml_path: Path) -> CommentedMap:
    """Return ``data`` as a mapping or raise :class:`ConfigError`.

    ``setforge.yaml`` is hand-editable, so a syntactically-valid YAML
    document whose root is a list or bare scalar can reach the apply /
    detect call sites. Indexing such a root with ``data[...]`` /
    ``data.get(...)`` would leak an unwrapped ``TypeError`` /
    ``AttributeError``; this guard converts that into a domain
    :class:`ConfigError` naming the file and the problem.
    """
    if not isinstance(data, CommentedMap):
        raise ConfigError(
            f"setforge.yaml root must be a mapping, got "
            f"{type(data).__name__}: {yaml_path}"
        )
    return data


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
    "known_versions",
    "parse_schema_version",
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

    @property
    def reverse(self) -> Migration:
        """The inverse migration (``to_version`` → ``from_version``).

        Required on every registered migration so a downgrade
        (``migrate --to=<older>``) can walk the chain backward. The
        registry :data:`MIGRATIONS` stays forward-only — the reverse is
        attached to its forward instance, never registered, so the
        forward walk cannot cycle. ``runtime_checkable`` does NOT enforce
        property presence, so :func:`_validate_registry` checks it at
        import time instead.
        """
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
    very key ``up`` inserts, the ``up → down`` pair adds-then-removes
    nothing net, so key-absence is restored — byte-identity holds vs the
    post-ruamel-normalization document (the first load→dump normalizes
    the hand-written source), not vs the original bytes. See the
    reverse test for the precise round-trip wording.
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
        data = _require_mapping_root(data, roots.cfg_path)
        # Overwrite-or-insert — idempotent on replay; re-applying
        # converges instead of raising on an already-present key. Writes
        # exactly the schema_version key, so the post-migration config
        # still loads under the schema's extra="forbid".
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

    @property
    def reverse(self) -> VersionStampMigration:
        """The forward 1.0 → 1.1 stamp — keeps the Protocol symmetric.

        A reverse-of-a-reverse is the original forward migration. Defined
        so ``_VersionStampReverse`` satisfies the ``reverse``-bearing
        :class:`Migration` Protocol when it is used as a chain element in
        a downgrade walk.
        """
        return VersionStampMigration(
            from_version=self.to_version, to_version=self.from_version
        )

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
        data = _require_mapping_root(data, roots.cfg_path)
        if "schema_version" in data:
            del data["schema_version"]
        atomic_write_yaml(roots.cfg_path, data)


MIGRATIONS: Final[tuple[Migration, ...]] = (VersionStampMigration(),)
"""Ordered registry of available FORWARD migrations.

Holds the first real migration (version-stamp 1.0 → 1.1). Future
migrations are appended in ``from_version`` order so
:func:`find_migration_path` can walk the chain forward. Each
migration's reverse is attached to its forward instance, never added
here — that would make the forward walk cycle (see
:class:`VersionStampMigration`).
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
    data = _require_mapping_root(data, yaml_path)
    raw = data.get("schema_version")
    if raw is None:
        return _DEFAULT_SCHEMA_VERSION
    return str(raw)


def known_versions() -> frozenset[str]:
    """Every schema version the current registry can resolve to.

    The build's :data:`current_expected_schema_version` plus every
    ``from_version`` / ``to_version`` in :data:`MIGRATIONS`. The
    ``migrate --to`` / ``--pin`` CLI validates a user-supplied target
    against this set so an unknown version errors cleanly instead of
    falling through a string-range "reachable" check.
    """
    versions = {current_expected_schema_version}
    for m in MIGRATIONS:
        versions.add(m.from_version)
        versions.add(m.to_version)
    return frozenset(versions)


def find_migration_path(*, from_v: str, to_v: str) -> tuple[Migration, ...]:
    """Find a chain from ``from_v`` to ``to_v`` — walking forward OR backward.

    Direction is decided **semantically** (:func:`parse_schema_version`
    → ``(int, int)``), never by string sort, so the 1.9 ↔ 1.10 boundary
    is correct.

    - ``to_v == from_v`` → ``()`` (nothing to do).
    - ``to_v`` newer → forward chain via :data:`MIGRATIONS` (each step
      picks the migration whose ``from_version`` matches the cursor).
    - ``to_v`` older → reverse chain: at each step pick the forward
      migration whose ``to_version`` matches the cursor and append its
      ``.reverse`` (the registry itself stays forward-only).

    Returns ``()`` when no chain bridges the two versions. The walk is
    bounded by ``len(MIGRATIONS) + 1`` in BOTH directions, so an
    unreachable target terminates with ``()`` instead of looping.

    Raises :class:`ConfigError` (never a bare ``ValueError`` /
    ``IndexError``) when either version is not a valid ``MAJOR.MINOR``
    token.
    """
    from_t = parse_schema_version(from_v)
    to_t = parse_schema_version(to_v)
    if from_t == to_t:
        return ()
    chain: list[Migration] = []
    cursor = from_v
    bound = len(MIGRATIONS) + 1
    forward = to_t > from_t
    for _ in range(bound):
        if parse_schema_version(cursor) == to_t:
            return tuple(chain)
        if forward:
            match = next((m for m in MIGRATIONS if m.from_version == cursor), None)
            if match is None:
                return ()
            chain.append(match)
            cursor = match.to_version
        else:
            match = next((m for m in MIGRATIONS if m.to_version == cursor), None)
            if match is None:
                return ()
            chain.append(match.reverse)
            cursor = match.from_version
    return ()


def _validate_registry() -> None:
    """Assert every registered migration carries a correctly-swapped ``reverse``.

    ``@runtime_checkable`` Protocols verify attribute *names* at
    isinstance time but do NOT check property presence or behavior, so a
    migration appended to :data:`MIGRATIONS` without a ``reverse`` (or
    with a mis-swapped one) would crash only at downgrade time, deep in
    the reverse walk. This import-time guard turns that latent failure
    into a loud one at module load.
    """
    for m in MIGRATIONS:
        rev = m.reverse
        if rev.from_version != m.to_version or rev.to_version != m.from_version:
            raise ConfigError(
                f"migration {type(m).__name__} has a mis-swapped reverse: "
                f"forward {m.from_version}->{m.to_version}, "
                f"reverse {rev.from_version}->{rev.to_version}"
            )


_validate_registry()
