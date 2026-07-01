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
    from collections.abc import Mapping

    from setforge.config import Disposition
    from setforge.reconcile import FileId
    from setforge.spans import SpanEntry
    from setforge.transitions import StateSnapshotEntry, TransitionDir

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
    def writes_own_transition(self) -> bool:
        """This migration records its OWN durable transition inside ``apply``.

        The cutover captures binary ``state_snapshots`` of every mutated store +
        the setforge.yaml text and commits ONE transition BEFORE any legacy
        delete (MS1 commit-before-unlink) — something the migrate driver's
        after-apply, text-only transition cannot express. So the driver must NOT
        also write its transition for any chain containing this migration;
        ``cli.migrate`` keys that skip off this flag. See
        :func:`setforge.cli.migrate._chain_owns_transition`.
        """
        return True

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


@dataclass(frozen=True, slots=True)
class _SeedPlan:
    """The unified-store seed for one NOT-yet-seeded ``(profile, fid)``.

    ``base`` is the DL1 tracked-byte seed; ``local`` is the live bytes (or
    ``ABSENT`` when the file is undeployed). ``hunks`` is the pre-classified
    index row list (``kind:key`` / line rows with ``cls=local``), or ``None`` to
    leave the index entry's hunks empty so the staging layer classifies the
    divergence lazily (PENDING) at the next install/compare — the ``shared``
    behaviour.
    """

    profile: str
    fid: FileId
    base: bytes
    local: object  # bytes | Absent
    hunks: list[dict[str, object]] | None


def _structured_span_covered(path: str, spans: tuple[SpanEntry, ...]) -> bool:
    """Whether a structured leaf ``path`` is covered by a host-keep span.

    A span whose dotted anchor equals ``path`` covers it; a ``deep`` span covers
    the whole subtree under its anchor. (Line/heading-anchored markdown spans are
    handled at the file level, not here.)
    """
    for span in spans:
        if path == span.anchor:
            return True
        if span.deep and path.startswith(span.anchor + "."):
            return True
    return False


def _classified_hunks(rec: _FidLegacy) -> list[dict[str, object]] | None:
    """Pre-classified index rows for ``rec``'s divergence, or ``None`` for lazy.

    ``forked`` / ``pinned`` -> every divergent unit is ``LOCAL`` (the host copy
    wins, never re-prompted). ``shared`` structured file -> only span-covered
    key-units are ``LOCAL``; the rest stay lazy (PENDING). ``shared`` markdown ->
    lazy (``None``): a partial host-local markdown region on a NOT-yet-seeded file
    is left PENDING for a one-time re-classify (data-safe; such files normally
    arrive already-seeded and take the preserve branch instead).
    """
    from dataclasses import replace

    from setforge.config import Disposition
    from setforge.reconcile import hunks as line_hunks
    from setforge.reconcile import structured_units
    from setforge.reconcile.types import HunkClass

    if not rec.dst_exists:
        return None  # local is ABSENT — no divergence to classify

    local_disp = rec.disposition in (Disposition.FORKED, Disposition.PINNED)

    if rec.is_structured:
        fmt = structured_units.structured_format(rec.dst)
        if fmt is None:  # defensive: is_structured said yes, format says no
            return None
        units = structured_units.extract_structured_units(
            rec.tracked_bytes, rec.live_bytes, fmt
        )
        if local_disp:
            picked = units
        else:  # shared: only span-covered key-units become LOCAL
            picked = [u for u in units if _structured_span_covered(u.path, rec.spans)]
        rows = structured_units.serialize_structured(
            [replace(u, cls=HunkClass.LOCAL) for u in picked]
        )
        return rows or None

    # Markdown / line-based file.
    if not local_disp:
        return None  # shared markdown -> lazy PENDING (see docstring)
    hunks = line_hunks.extract_hunks(rec.tracked_bytes, rec.live_bytes)
    rows = line_hunks.serialize([replace(h, cls=HunkClass.LOCAL) for h in hunks])
    return rows or None


def _validate_bases(records: list[_FidLegacy]) -> None:
    """Pre-flight (D4): every structured base must parse + round-trip, or abort.

    Part of plan-building, BEFORE any mutation — a single malformed structured
    base raises here listing every offender, so the cutover mutates nothing and a
    profile is never left half-migrated (fully-legacy XOR fully-unified). Validates
    the tracked-byte base seed each not-yet-seeded structured file will carry; the
    apply pass additionally re-validates an already-seeded fid's existing reconcile
    base under the profile lock.
    """
    from setforge.reconcile import structured_units

    bad: list[str] = []
    for rec in records:
        if not rec.is_structured:
            continue
        fmt = structured_units.structured_format(rec.dst)
        if fmt is None:
            continue
        try:
            model = structured_units._load_model(rec.tracked_bytes, fmt)
            structured_units._dump_model(model, fmt)
        except Exception as err:
            bad.append(f"  {rec.profile}/{rec.name} ({rec.dst}): {err}")
    if bad:
        raise ConfigError(
            "cannot migrate to schema 3.0: "
            f"{len(bad)} structured base(s) failed parse/round-trip validation. "
            "The cutover refuses rather than migrate a corrupt base (nothing was "
            "changed). Fix or remove these files, then re-run:\n" + "\n".join(bad)
        )


def _write_cutover_transition(
    *,
    file_pre: Mapping[Path, str | None],
    file_post: Mapping[Path, str | None],
    state_snapshots: tuple[StateSnapshotEntry, ...],
) -> TransitionDir:
    """Record the cutover's single durable transition (MS1 commit-before-unlink).

    A ``MIGRATE``-labelled transition carrying BOTH a text patch for
    ``setforge.yaml`` (``file_pre`` -> ``file_post``; the schema_version flip,
    reversed by ``patch -R``) AND the binary ``state_snapshots`` of every mutated
    store (restored byte-exact by ``restore_state_snapshots``). ``apply`` calls
    this AFTER capturing the pre-state + writing the additive unified store, but
    BEFORE unlinking any legacy artifact — so a crash or ``setforge revert`` after
    the commit restores the pre-cutover state exactly. Returns the transition dir.
    """
    import sys

    from setforge import transitions
    from setforge._redact import redact_argv

    return transitions.write_transition(
        transitions.make_meta(
            transitions.TransitionCommand.MIGRATE,
            transitions.MIGRATE_TRANSITION_PROFILE,
            end_timestamp=transitions.now_utc().isoformat(),
            command_line=redact_argv(sys.argv[1:]),
        ),
        file_pre,
        file_post,
        None,
        state_snapshots=state_snapshots,
    )


def _classify_fid(rec: _FidLegacy) -> _SeedPlan:
    """Map one legacy record to its unified-store seed (base + local + hunks).

    Base is the verbatim tracked bytes (DL1 — never live, never a merge result);
    local is the live bytes, or ``ABSENT`` when the file is undeployed.
    """
    from setforge.reconcile.types import ABSENT

    return _SeedPlan(
        profile=rec.profile,
        fid=rec.fid,
        base=rec.tracked_bytes,
        local=rec.live_bytes if rec.dst_exists else ABSENT,
        hunks=_classified_hunks(rec),
    )


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
