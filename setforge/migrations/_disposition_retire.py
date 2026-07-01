"""The disposition-retire cutover — schema 2.1 -> 3.0.

Retires the legacy per-file ``Disposition`` (SHARED/FORKED/PINNED) + ``spans`` +
``scalar_base_store`` model, migrating every deployed profile's state into the
unified per-unit SHARED/LOCAL reconcile store, then deleting the legacy stores.

Reversibility is transition-based: the forward migration records a binary
state-snapshot transition (committed BEFORE any legacy delete) so ``setforge
revert --profile=migrate`` restores the pre-cutover bytes byte-exact. The
schema-chain reverse (3.0 -> 2.1) REFUSES cleanly — collapsing PINNED/FORKED
into the binary SHARED/LOCAL model is lossy and cannot be faithfully
re-derived (a one-way contract step; see COMPATIBILITY.md).

The forward ``apply`` (and its ``manifest`` / ``affected_paths``) is built up
across the cutover implementation waves; the reverse refusal is complete here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from setforge.errors import ConfigError
from setforge.migrations import ManifestEntry, ManifestType, MigrationRoots


@dataclass(frozen=True, slots=True)
class DispositionRetireMigration:
    """The 2.1 -> 3.0 forward cutover (see the module docstring)."""

    from_version: str = "2.1"
    to_version: str = "3.0"

    @property
    def reverse(self) -> _DispositionRetireReverse:
        """The one-way inverse — refuses (the collapse is lossy)."""
        return _DispositionRetireReverse(
            from_version=self.to_version, to_version=self.from_version
        )

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        raise NotImplementedError  # built in a later cutover wave

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        raise NotImplementedError  # built in a later cutover wave

    def apply(self, *, roots: MigrationRoots) -> None:
        raise NotImplementedError  # built in a later cutover wave


@dataclass(frozen=True, slots=True)
class _DispositionRetireReverse:
    """The one-way inverse of :class:`DispositionRetireMigration`.

    The cutover collapses the three-valued ``Disposition`` + span model into the
    binary per-unit SHARED/LOCAL store — a lossy transform. A stateless schema
    reverse cannot regenerate the original disposition/spans/scalar-base intent,
    so ``apply`` refuses with a clear :class:`~setforge.errors.ConfigError`
    rather than restamping to 2.1 over a store it cannot reconstruct. The
    reversible path is the transition-based ``setforge revert --profile=migrate``
    (which restores the snapshotted pre-cutover bytes), not this schema reverse.
    """

    from_version: str = "3.0"
    to_version: str = "2.1"

    @property
    def reverse(self) -> DispositionRetireMigration:
        """The forward 2.1 -> 3.0 cutover (keeps the Protocol symmetric)."""
        return DispositionRetireMigration(
            from_version=self.to_version, to_version=self.from_version
        )

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        """A single NOTE: the down-migration is refused, nothing is mutated."""
        return (
            ManifestEntry(
                type=ManifestType.NOTE,
                description=(
                    "disposition/spans retirement is irreversible — "
                    "down-migration refuses"
                ),
                affected_path=roots.cfg_path,
            ),
        )

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        """Nothing is touched: the reverse refuses before any write."""
        return ()

    def apply(self, *, roots: MigrationRoots) -> None:
        """Refuse: the SHARED/LOCAL collapse cannot be reversed to disposition."""
        raise ConfigError(
            "cannot down-migrate from schema 3.0 to 2.1: the disposition / spans / "
            "scalar-base model was retired and collapsed into the per-unit "
            "SHARED/LOCAL store, a lossy transform that cannot be regenerated. Use "
            "'setforge revert --profile=migrate' to restore the pre-cutover state "
            "from the recorded transition."
        )
