"""The span-surface-retire cutover — schema 3.0 -> 4.0 (SKELETON).

Retires the host-local span-declaration surface carried in the ``local.yaml``
overlay (the per-tracked-file span/disposition overlay fields), folding whatever
host-local intent survives into the unified per-unit reconcile store and
stripping the retired keys from ``local.yaml``.

This module currently ships the migration's IDENTITY + registration surface
only: :attr:`SpanSurfaceRetireMigration.from_version` / ``to_version`` /
``reverse`` / ``manifest`` / ``affected_paths``, plus the one-way reverse
refusal (the fold is lossy — see :class:`_SpanSurfaceRetireReverse`). The heavy
forward ``apply`` fold/strip body is implemented in a SEPARATE later task; until
then the forward ``apply`` raises :class:`NotImplementedError` so no
half-migration can run.

Modeled structurally on :mod:`setforge.migrations._disposition_retire` (the
2.1 -> 3.0 cutover): a ``writes_own_transition`` forward migration whose reverse
refuses cleanly and points at the transition-based
``setforge revert --profile=migrate`` recovery.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from setforge.errors import ConfigError
from setforge.migrations import ManifestEntry, ManifestType, MigrationRoots

#: ``local.yaml`` relpath under ``roots.home`` (host-local overlay source) —
#: the surface this cutover retires the span-declaration fields from. Copied
#: from the disposition-retire migration so this module stays self-contained.
_LOCAL_YAML_RELPATH: Final = (".config", "setforge", "local.yaml")


def _local_yaml_path(roots: MigrationRoots) -> Path:
    """The host-local ``local.yaml`` under ``roots.home``."""
    path = roots.home
    for part in _LOCAL_YAML_RELPATH:
        path = path / part
    return path


@dataclass(frozen=True, slots=True)
class SpanSurfaceRetireMigration:
    """The 3.0 -> 4.0 forward cutover (see the module docstring).

    SKELETON: identity + registration surface only. ``apply`` is deferred to a
    later task and raises :class:`NotImplementedError` for now.
    """

    from_version: str = "3.0"
    to_version: str = "4.0"

    @property
    def writes_own_transition(self) -> bool:
        """This migration records its OWN durable transition inside ``apply``.

        Like the disposition-retire cutover it modifies, the fold captures binary
        state snapshots and commits ONE transition BEFORE any legacy strip, which
        the migrate driver's after-apply text-only transition cannot express. So
        the driver must NOT also write its transition for any chain containing
        this migration; ``cli.migrate`` keys that skip off this flag.
        """
        return True

    @property
    def reverse(self) -> _SpanSurfaceRetireReverse:
        """The one-way inverse — refuses (the fold is lossy)."""
        return _SpanSurfaceRetireReverse(
            from_version=self.to_version, to_version=self.from_version
        )

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        """Read-only ``--check`` inventory: the schema stamp + retired-surface NOTEs.

        The concrete per-``(profile, fid)`` enumeration lands with the fold body;
        the skeleton lists the schema stamp EDIT plus a NOTE naming the retired
        ``local.yaml`` span-declaration surface.
        """
        return (
            ManifestEntry(
                type=ManifestType.EDIT,
                description=f"stamp schema_version {self.to_version!r}",
                affected_path=roots.cfg_path,
            ),
            ManifestEntry(
                type=ManifestType.NOTE,
                description=(
                    "retire the host-local span-declaration surface "
                    "(per-file span/disposition overlay) from local.yaml"
                ),
                affected_path=_local_yaml_path(roots),
            ),
        )

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        """The paths the cutover reads/writes/strips (drives backup + rollback).

        The skeleton lists the two definite top-level surfaces — ``setforge.yaml``
        (schema stamp) and the host-local ``local.yaml`` (span-declaration strip).
        The fold body extends this with each migrated ``(profile, fid)``'s
        reconcile legs, mirroring the disposition-retire cutover.
        """
        return (roots.cfg_path, _local_yaml_path(roots))

    def apply(self, *, roots: MigrationRoots) -> None:
        """NOT YET IMPLEMENTED — the fold/strip body lands in a later task."""
        raise NotImplementedError("SpanSurfaceRetire.apply is implemented in task C2")


@dataclass(frozen=True, slots=True)
class _SpanSurfaceRetireReverse:
    """The one-way inverse of :class:`SpanSurfaceRetireMigration`.

    The cutover folds the host-local span-declaration surface into the binary
    per-unit SHARED/LOCAL store and strips the declarations — a lossy transform.
    A stateless schema reverse cannot regenerate the original span/disposition
    intent, so ``apply`` refuses with a clear :class:`~setforge.errors.ConfigError`
    rather than restamping to 3.0 over a store it cannot reconstruct. The
    reversible path is the transition-based ``setforge revert --profile=migrate``
    (which restores the snapshotted pre-cutover bytes), not this schema reverse.
    """

    from_version: str = "4.0"
    to_version: str = "3.0"

    @property
    def reverse(self) -> SpanSurfaceRetireMigration:
        """The forward 3.0 -> 4.0 cutover (keeps the Protocol symmetric)."""
        return SpanSurfaceRetireMigration(
            from_version=self.to_version, to_version=self.from_version
        )

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        """A single NOTE: the down-migration is refused, nothing is mutated."""
        return (
            ManifestEntry(
                type=ManifestType.NOTE,
                description=(
                    "span-declaration-surface retirement is irreversible — "
                    "down-migration refuses"
                ),
                affected_path=roots.cfg_path,
            ),
        )

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        """Nothing is touched: the reverse refuses before any write."""
        return ()

    def apply(self, *, roots: MigrationRoots) -> None:
        """Refuse: the span-surface fold cannot be reversed by a schema restamp."""
        raise ConfigError(
            "cannot down-migrate from schema 4.0 to 3.0: the host-local "
            "span-declaration surface was retired and folded into the per-unit "
            "SHARED/LOCAL store, a lossy transform that cannot be regenerated. Use "
            "'setforge revert --profile=migrate' to restore the pre-cutover state "
            "from the recorded transition."
        )
