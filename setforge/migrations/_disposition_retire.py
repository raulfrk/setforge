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
from typing import TYPE_CHECKING, Final

from setforge.errors import ConfigError
from setforge.migrations import ManifestEntry, ManifestType, MigrationRoots

if TYPE_CHECKING:
    from setforge.config import Disposition
    from setforge.reconcile import FileId
    from setforge.spans import SpanEntry

#: ``local.yaml`` relpath under ``roots.home`` (host-local overlay source),
#: copied from the marker-retire migration so this module stays self-contained
#: after ``source``/``spans`` internals are retired around it.
_LOCAL_YAML_RELPATH: Final = (".config", "setforge", "local.yaml")


def _local_yaml_path(roots: MigrationRoots) -> Path:
    """The host-local ``local.yaml`` under ``roots.home``."""
    path = roots.home
    for part in _LOCAL_YAML_RELPATH:
        path = path / part
    return path


def _is_structured(dst: Path) -> bool:
    """Whether ``dst`` is a structured (YAML/JSON/JSONC) file.

    Copied from the retiring ``disposition_merge.is_structural`` so the cutover
    still picks the key-unit vs line-hunk extractor after that module's routing
    is removed. Structured units diff per key; everything else per line.
    """
    from setforge import jsonc

    return jsonc.is_jsonc_file(dst) or dst.suffix in {".yaml", ".yml"}


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


@dataclass(frozen=True, slots=True)
class _FidLegacy:
    """One ``(profile, tracked-file)`` legacy record the cutover migrates.

    Per ``(profile, fid)`` because the reconcile / base / scalar-base stores are
    profile-scoped, even though the disposition + span INTENT is file-level
    (folded from the shared config + the host-local overlay). ``tracked_bytes``
    (=the DL1 base seed, verbatim tracked/upstream bytes) and ``live_bytes``
    (=the local overlay) are the whole-file byte pair the per-unit classifier
    diffs; the legacy ``scalar_base_store`` ancestor is intentionally NOT read —
    base is reseeded from tracked bytes (DL1), and present-null vs absent is
    ground-truth-derivable from ``live_bytes`` itself.
    """

    profile: str
    name: str
    fid: FileId
    src: Path
    dst: Path
    disposition: Disposition | None
    spans: tuple[SpanEntry, ...]
    tracked_bytes: bytes
    live_bytes: bytes
    dst_exists: bool
    is_structured: bool


def _build_legacy_records(roots: MigrationRoots) -> list[_FidLegacy]:
    """Enumerate every ``(profile, tracked-file)`` with its effective legacy state.

    Read-only. Loads the config, folds the host-local overlay so ``disposition``
    / ``spans`` are effective (shared config + ``local.yaml`` override), then per
    profile x resolved tracked file records the disposition, spans, and the
    tracked (base seed) + live (local) bytes. Symlink tracked files carry no
    reconcile/disposition state and are skipped. A missing src/dst reads as empty
    bytes with ``dst_exists`` flagged, so a fresh / undeployed host is a clean
    no-op downstream rather than a crash.
    """
    from setforge.compare import resolve_dst, resolve_src
    from setforge.config import (
        apply_host_local_tracked_file_overrides,
        load_config,
        resolve_profile,
    )
    from setforge.reconcile import file_id

    config = load_config(roots.cfg_path)
    apply_host_local_tracked_file_overrides(
        config, local_config_path=_local_yaml_path(roots)
    )

    records: list[_FidLegacy] = []
    for profile_name in config.profiles:
        for name in resolve_profile(config, profile_name).tracked_files:
            tracked_file = config.tracked_files[name]
            if tracked_file.symlink is not None:
                continue
            src = resolve_src(tracked_file, roots.repo_root)
            dst = resolve_dst(tracked_file)
            dst_exists = dst.exists()
            records.append(
                _FidLegacy(
                    profile=profile_name,
                    name=name,
                    fid=file_id(name),
                    src=src,
                    dst=dst,
                    disposition=tracked_file.disposition,
                    spans=tuple(tracked_file.spans),
                    tracked_bytes=src.read_bytes() if src.exists() else b"",
                    live_bytes=dst.read_bytes() if dst_exists else b"",
                    dst_exists=dst_exists,
                    is_structured=_is_structured(dst),
                )
            )
    return records
