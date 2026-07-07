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

    from setforge.reconcile import FileId
    from setforge.reconcile.types import Absent
    from setforge.transitions import StateSnapshotEntry, TransitionDir

#: Disposition string values (frozen here — the ``Disposition`` enum is removed
#: by this cutover, so the migration must not import it).
_FORKED: Final = "forked"
_PINNED: Final = "pinned"
#: Dispositions whose whole-file divergence classifies every unit LOCAL.
_LOCAL_DISPOSITIONS: Final = frozenset({_FORKED, _PINNED})

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

    A deliberate self-contained copy of the retired file-level disposition
    merge's structural-file dispatch, so the cutover still picks the key-unit
    vs line-hunk extractor after that routing is removed. Structured units diff
    per key; everything else per line.
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
        """Read-only ``--check`` inventory: the schema stamp + a per-file NOTE.

        Enumerates the legacy records (read-only) so ``--check`` reports what the
        cutover will migrate without mutating anything.
        """
        entries: list[ManifestEntry] = [
            ManifestEntry(
                type=ManifestType.EDIT,
                description=f"stamp schema_version {self.to_version!r}",
                affected_path=roots.cfg_path,
            )
        ]
        for rec in _build_legacy_records(roots):
            entries.append(
                ManifestEntry(
                    type=ManifestType.NOTE,
                    description=(
                        f"migrate {rec.profile}/{rec.name} "
                        f"(disposition={rec.disposition or 'none'}) "
                        "into the unified per-unit store"
                    ),
                    affected_path=rec.dst,
                )
            )
        return tuple(entries)

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        """Every path the cutover reads/writes/deletes (drives backup + rollback).

        setforge.yaml + each ``(profile, fid)``'s reconcile legs (base/local/
        local-absent/index) and legacy spans/scalar-base manifests. Existence is
        NOT filtered here (a missing path snapshots as absent and restores by
        deletion); the union is deduped preserving order.
        """
        from setforge import base_store, scalar_base_store
        from setforge.reconcile import store as reconcile_store
        from setforge.transitions import _spans_manifest_path

        seen: dict[Path, None] = {roots.cfg_path: None}
        records = _build_legacy_records(roots)
        for rec in records:
            key = str(rec.fid)
            for path in (
                base_store.base_path(rec.profile, key),
                reconcile_store.local_content_path(rec.profile, key),
                reconcile_store.local_absent_path(rec.profile, key),
                reconcile_store.drafts_manifest_path(rec.profile, key),
                _spans_manifest_path(rec.profile, key),
                scalar_base_store.manifest_path(rec.profile, key),
            ):
                seen.setdefault(path, None)
        for profile in {rec.profile for rec in records}:
            seen.setdefault(reconcile_store.index_manifest_path(profile), None)
        return tuple(seen)

    def apply(self, *, roots: MigrationRoots) -> None:
        """Run the cutover: validate -> seed -> stamp -> commit -> delete (see §2).

        Pass 1 enumerates + pre-flight-validates every structured base (D4 —
        raises here, before any mutation). Pass 2 holds ALL profile locks across
        one arc: capture pre-state snapshots, seed the unified store for
        not-yet-seeded fids (already-seeded fids are preserved, never clobbered),
        stamp schema 3.0, COMMIT the one durable transition (state_snapshots +
        setforge.yaml), then delete the legacy stores (after the commit — MS1).
        Idempotent: a re-run skips already-unified fids and no-ops the deletes.
        """
        import contextlib

        from setforge import locking, reconcile, transitions

        records = _build_legacy_records(roots)  # pass 1: enumerate (read-only)
        _validate_bases(records)  # D4 pre-flight abort — raises before any write

        if not records:
            # Fresh / all-symlink / no tracked files: nothing to migrate, just
            # advance the schema so the config reaches 3.0.
            _stamp_schema_version(roots.cfg_path, self.to_version)
            return

        profiles = sorted({rec.profile for rec in records})
        cfg_pre = roots.cfg_path.read_text(encoding="utf-8")

        with contextlib.ExitStack() as locks:
            for profile in profiles:
                locks.enter_context(locking.profile_lock(profile))

            snapshots = _capture_cutover_snapshots(records, profiles)

            for rec in records:
                if reconcile.read_base(rec.profile, rec.fid) is not None:
                    continue  # already in the unified store — preserve, never re-seed
                plan = _classify_fid(rec)
                reconcile.record(
                    rec.profile,
                    rec.fid,
                    base=plan.base,
                    local=plan.local,
                    hunks=plan.hunks,
                )

            _stamp_schema_version(roots.cfg_path, self.to_version)
            cfg_post = roots.cfg_path.read_text(encoding="utf-8")

            # When the migrate driver threads its pre-chain frozen image, use it
            # as file_pre so this single transition carries the FULL reverse
            # delta to the chain's ORIGIN (INV-5) — not just the pre-cutover
            # (e.g. 2.1) state. file_post re-snapshots the SAME paths now (schema
            # stamped, before the legacy delete = the 3.0 image). Applied outside
            # the driver (pre_chain_snapshot is None) keeps the prior behavior.
            pre = roots.pre_chain_snapshot
            file_pre: dict[Path, str | None]
            file_post: dict[Path, str | None]
            if pre is not None:
                file_pre = dict(pre)
                file_post = dict(transitions.snapshot_paths(tuple(pre)))
            else:
                file_pre = {roots.cfg_path: cfg_pre}
                file_post = {roots.cfg_path: cfg_post}

            # INV-5 here rests on a NON-OVERLAP between the two reverse
            # mechanisms: no path in the text patch's pre-chain image is also a
            # state_snapshot. On revert the text patch reverses FIRST, then
            # restore_state_snapshots runs — so for any shared path the snapshot
            # (a pre-CUTOVER image) would win and restore it to the intermediate
            # schema, not the origin. Holds today: the framework steps mutate
            # only cfg_path, never a snapshotted store. A future pre-cutover step
            # that edits a state-snapshotted store must extend the snapshot set
            # back to the chain origin, or revert would stop short of it.
            _write_cutover_transition(
                file_pre=file_pre,
                file_post=file_post,
                state_snapshots=snapshots,
            )
            _delete_legacy_stores(records, profiles)


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
class _SpanSpec:
    """A frozen, self-contained projection of one legacy span.

    The ``spans`` model is removed by this cutover, so the migration reads spans
    from the raw YAML and keeps only what the per-unit classifier needs:
    ``anchor`` (dotted-path / heading identity), ``deep`` (subtree coverage), and
    the ``kind`` / ``semantics`` strings that decide whether the span is a
    host-keep region (-> its units become LOCAL). ``host_keep`` is precomputed:
    a ``host-local`` semantics OR a ``pinned`` / ``overlay`` kind — a
    ``shared``-semantics forked span is NOT host-keep (its region stays shared).
    """

    anchor: str
    deep: bool
    kind: str
    semantics: str

    @property
    def host_keep(self) -> bool:
        return self.semantics == "host-local" or self.kind in ("pinned", "overlay")


@dataclass(frozen=True, slots=True)
class _FidLegacy:
    """One ``(profile, tracked-file)`` legacy record the cutover migrates.

    Per ``(profile, fid)`` because the reconcile / base / scalar-base stores are
    profile-scoped, even though the disposition + span INTENT is file-level
    (folded from the shared config + the host-local overlay). Read fully from the
    raw YAML (Contract20 pattern) so the migration survives the removal of the
    ``Disposition`` enum + ``spans`` model — ``disposition`` is the raw string,
    ``spans`` a tuple of :class:`_SpanSpec`. ``tracked_bytes`` (=the DL1 base
    seed, verbatim tracked/upstream bytes) and ``live_bytes`` (=the local
    overlay) are the whole-file byte pair the classifier diffs; the legacy
    ``scalar_base_store`` ancestor is intentionally NOT read — base is reseeded
    from tracked bytes (DL1), and present-null vs absent is ground-truth-
    derivable from ``live_bytes`` itself.
    """

    profile: str
    name: str
    fid: FileId
    src: Path
    dst: Path
    disposition: str | None
    spans: tuple[_SpanSpec, ...]
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
    local: bytes | Absent
    hunks: list[dict[str, object]] | None


def _structured_span_covered(path: str, spans: tuple[_SpanSpec, ...]) -> bool:
    """Whether a structured leaf ``path`` is covered by a host-keep span.

    Only host-keep spans (see :attr:`_SpanSpec.host_keep`) force a key LOCAL; a
    shared-semantics span leaves its region shared. A span whose dotted anchor
    equals ``path`` covers it; a ``deep`` span covers the whole subtree under its
    anchor. (Line/heading-anchored markdown spans are handled at the file level.)
    """
    for span in spans:
        if not span.host_keep:
            continue
        if path == span.anchor or (span.deep and path.startswith(span.anchor + ".")):
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

    from setforge.reconcile import hunks as line_hunks
    from setforge.reconcile import structured_units
    from setforge.reconcile.types import HunkClass

    if not rec.dst_exists:
        return None  # local is ABSENT — no divergence to classify

    local_disp = rec.disposition in _LOCAL_DISPOSITIONS

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


def _stamp_schema_version(cfg_path: Path, to_version: str) -> None:
    """Stamp ``schema_version`` AND strip the retired ``disposition``/``spans``
    tracked-file keys from setforge.yaml (comment-safe).

    Rewrites ``schema_version: <to_version>`` and removes every tracked_file's
    now-retired ``disposition:`` / ``spans:`` keys, so a migrated config is a
    clean 3.0 document that ``validate`` accepts (the 3.0 model would otherwise
    warn-and-ignore the leftover keys, and ``validate`` rejects them). Overwrite-
    in-place preserves surviving key positions + comments; the single atomic
    write is crash-safe and captured in the cutover transition for byte-exact
    revert. Raises :class:`ConfigError` on a non-mapping root.
    """
    from collections.abc import MutableMapping as _MutMap

    from setforge.migrations import _require_mapping_root
    from setforge.migrations._yaml_ops import atomic_write_yaml, yaml_rt

    yaml = yaml_rt()
    with cfg_path.open("r", encoding="utf-8") as fh:
        data = yaml.load(fh)
    data = _require_mapping_root(data, cfg_path)
    data["schema_version"] = to_version
    tracked_files = data.get("tracked_files")
    if isinstance(tracked_files, _MutMap):
        for entry in tracked_files.values():
            if isinstance(entry, _MutMap):
                entry.pop("disposition", None)
                entry.pop("spans", None)
    atomic_write_yaml(cfg_path, data)


def _capture_cutover_snapshots(
    records: list[_FidLegacy], profiles: list[str]
) -> tuple[StateSnapshotEntry, ...]:
    """Snapshot every store the cutover mutates, BEFORE any mutation (MS1/MS5).

    Per ``(profile, fid)``: the byte BASE (shared with the unified store — a
    not-yet-seeded fid captures ``payload=None`` so revert deletes the seed) and
    the legacy SPANS + SCALAR_BASE manifests (captured so revert restores them
    after the cutover deletes them), plus the reconcile local/absent/drafts legs.
    The per-profile INDEX is captured ONCE, outside the fid loop, so a 2nd+ fid
    never records post-mutation index state.
    """
    from setforge import transitions

    entries: list[StateSnapshotEntry] = []
    for rec in records:
        key = str(rec.fid)
        entries.append(
            transitions.snapshot_store_state(
                transitions.SnapshotStore.BASE, rec.profile, key
            )
        )
        entries.append(
            transitions.snapshot_store_state(
                transitions.SnapshotStore.SPANS, rec.profile, key
            )
        )
        entries.append(
            transitions.snapshot_store_state(
                transitions.SnapshotStore.SCALAR_BASE, rec.profile, key
            )
        )
        entries.extend(transitions.reconcile_file_snapshots(rec.profile, key))
    for profile in profiles:
        entries.append(
            transitions.snapshot_store_state(
                transitions.SnapshotStore.INDEX, profile, profile
            )
        )
    return tuple(entries)


def _delete_legacy_stores(records: list[_FidLegacy], profiles: list[str]) -> None:
    """Delete the legacy-only stores AFTER the transition is committed (idempotent).

    Removes each ``(profile, fid)``'s spans + scalar-base manifests, and each
    profile's scalar-base format-version sidecar. The byte BASE store is NOT
    touched — it is now the unified store's base. Every unlink is ``missing_ok``
    so a re-run (or a resumed post-crash run) is a clean no-op.
    """
    from setforge import scalar_base_store
    from setforge.base_store_format import SIDECAR_NAME
    from setforge.transitions import _spans_manifest_path

    for rec in records:
        key = str(rec.fid)
        _spans_manifest_path(rec.profile, key).unlink(missing_ok=True)
        scalar_base_store.manifest_path(rec.profile, key).unlink(missing_ok=True)
    for profile in profiles:
        # The profile root is the manifest's parent dir; drop its dangling
        # scalar-base format-version sidecar so no reader points at a format
        # whose store is gone.
        root = scalar_base_store.manifest_path(profile, "_fmt_probe_").parent
        (root / SIDECAR_NAME).unlink(missing_ok=True)


def _span_specs_from_raw(raw_spans: object) -> tuple[_SpanSpec, ...]:
    """Project a raw YAML ``spans:`` list into frozen :class:`_SpanSpec`.

    Missing / non-list / malformed entries are skipped (a span with no string
    anchor cannot be matched). Defaults mirror the retired ``SpanEntry`` model:
    ``kind`` -> ``pinned``, ``semantics`` -> ``host-local`` (so a bare-anchor span
    is host-keep, exactly as the model defaulted).
    """
    if not isinstance(raw_spans, list):
        return ()
    specs: list[_SpanSpec] = []
    for entry in raw_spans:
        if not isinstance(entry, dict):
            continue
        anchor = entry.get("anchor")
        if not isinstance(anchor, str):
            continue
        specs.append(
            _SpanSpec(
                anchor=anchor,
                deep=bool(entry.get("deep", False)),
                kind=str(entry.get("kind", "pinned")),
                semantics=str(entry.get("semantics", "host-local")),
            )
        )
    return tuple(specs)


def _raw_legacy_by_id(
    tracked_files: object,
) -> dict[str, tuple[str | None, tuple[_SpanSpec, ...]]]:
    """Extract ``(disposition, spans)`` per tracked-file id from a raw map.

    Reads the removed fields straight from the ruamel document (Contract20
    pattern), so it works after the ``Disposition`` enum + ``spans`` model are
    gone. A non-mapping ``tracked_files`` yields an empty map.
    """
    out: dict[str, tuple[str | None, tuple[_SpanSpec, ...]]] = {}
    if not isinstance(tracked_files, dict):
        return out
    for tf_id, tf in tracked_files.items():
        if not isinstance(tf, dict):
            continue
        disp = tf.get("disposition")
        out[str(tf_id)] = (
            disp if isinstance(disp, str) else None,
            _span_specs_from_raw(tf.get("spans")),
        )
    return out


def _build_legacy_records(roots: MigrationRoots) -> list[_FidLegacy]:
    """Enumerate every ``(profile, tracked-file)`` with its effective legacy state.

    Read-only, and SELF-CONTAINED (Contract20 pattern): ``disposition`` / ``spans``
    are read from the RAW ruamel YAML (setforge.yaml + the ``local.yaml`` overlay)
    so the migration survives their removal from the strict config model. Those
    keys are then STRIPPED from an in-memory copy and the rest is model-validated,
    reusing real profile-inheritance + src/dst resolution (which never depended on
    the removed fields). The overlay's disposition OVERRIDES and its spans EXTEND
    the shared config. Symlink tracked files are skipped; a missing src/dst reads
    as empty bytes with ``dst_exists`` flagged (a fresh host is a clean no-op).

    Note: an overlay dst/mode/symlink override is NOT folded here (only
    disposition/spans) — an already-deployed such file is normally already in the
    unified store and takes the preserve branch, so this is a documented, benign
    limitation for the rare not-yet-seeded case.
    """
    import copy

    from setforge.compare import resolve_dst, resolve_src
    from setforge.config import Config, resolve_profile
    from setforge.migrations._yaml_ops import yaml_rt
    from setforge.reconcile import file_id

    yaml = yaml_rt()
    with roots.cfg_path.open("r", encoding="utf-8") as fh:
        raw = yaml.load(fh)
    if not isinstance(raw, dict):
        return []
    shared_legacy = _raw_legacy_by_id(raw.get("tracked_files"))

    overlay_legacy: dict[str, tuple[str | None, tuple[_SpanSpec, ...]]] = {}
    local_yaml = _local_yaml_path(roots)
    if local_yaml.exists():
        with local_yaml.open("r", encoding="utf-8") as fh:
            raw_local = yaml.load(fh)
        if isinstance(raw_local, dict):
            overlay_legacy = _raw_legacy_by_id(raw_local.get("tracked_files"))

    stripped = copy.deepcopy(raw)
    stripped_tf = stripped.get("tracked_files")
    if isinstance(stripped_tf, dict):
        for tf in stripped_tf.values():
            if isinstance(tf, dict):
                tf.pop("disposition", None)
                tf.pop("spans", None)
    config = Config.model_validate(stripped)

    records: list[_FidLegacy] = []
    for profile_name in config.profiles:
        for name in resolve_profile(config, profile_name).tracked_files:
            tracked_file = config.tracked_files[name]
            if tracked_file.symlink is not None:
                continue
            sh_disp, sh_spans = shared_legacy.get(name, (None, ()))
            ov_disp, ov_spans = overlay_legacy.get(name, (None, ()))
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
                    disposition=ov_disp or sh_disp,
                    spans=sh_spans + ov_spans,
                    tracked_bytes=src.read_bytes() if src.exists() else b"",
                    live_bytes=dst.read_bytes() if dst_exists else b"",
                    dst_exists=dst_exists,
                    is_structured=_is_structured(dst),
                )
            )
    return records
