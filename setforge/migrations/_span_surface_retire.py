"""The span-surface-retire cutover — schema 3.0 -> 4.0.

Retires the host-local span-declaration surface carried in the ``local.yaml``
overlay (the per-tracked-file ``host_local_sections`` block + OVERLAY ``spans``),
folding whatever host-local intent survives into the unified per-unit reconcile
store and stripping the retired keys from ``local.yaml``.

The forward :meth:`SpanSurfaceRetireMigration.apply` is fully implemented:

* **Headingless refuse.** A pre-flight enumerates every residual
  host-local section and aborts BEFORE any mutation if a body lacks a markdown
  heading — the 4.0 store identity is heading-based (a stable ``reloc_anchor``
  minted from the body's own heading), so a headingless body has no identity to
  fold onto (see :func:`_validate_headings`).
* **Fold.** Each ``(profile, fid)`` carrying a residual section MERGES the
  section body into its unit's LOCAL store, preserving the recorded ``local``
  bytes AND every existing hunk classification — SHARED / LOCAL / SHARED_DRAFTED
  and its draft bytes — so no shared drift or blessed draft is clobbered
  (INV-1 / INV-8; see :func:`_fold_sections`).
* **Stamp + strip.** Advances ``schema_version`` to 4.0 and drops the retired
  ``host_local_sections`` / overlay-``spans`` keys from ``local.yaml``.
* **Transition.** Commits ONE durable ``MIGRATE`` transition (text patch for
  ``setforge.yaml`` + ``local.yaml`` PLUS byte-exact state snapshots of every
  mutated reconcile leg) so a crash or ``setforge revert --profile=migrate``
  restores the pre-cutover state exactly.

Idempotent: a section already present as a LOCAL+reloc unit is skipped, so a
re-run drains to an empty residual and no-ops. No ``local.yaml`` (the
frozen-fixture case) ⇒ nothing to fold: the schema is advanced and ``apply``
returns without a transition.

Modeled structurally on :mod:`setforge.migrations._disposition_retire` (the
2.1 -> 3.0 cutover): a ``writes_own_transition`` forward migration whose reverse
refuses cleanly and points at the transition-based
``setforge revert --profile=migrate`` recovery.
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
    from setforge.source import HostLocalSection
    from setforge.transitions import StateSnapshotEntry, TransitionDir

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

    Folds the residual host-local section surface into the per-unit reconcile
    store, stamps schema 4.0, strips the retired ``local.yaml`` keys, and commits
    one durable transition — all in :meth:`apply`.
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
        """Read-only ``--check`` inventory: schema-stamp EDIT + retired-surface NOTE.

        Deliberately coarse — one EDIT (the ``schema_version`` stamp) plus one NOTE
        naming the retired ``local.yaml`` span-declaration surface — rather than a
        per-``(profile, fid)`` section enumeration.
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
        """Every path the cutover reads/writes/strips (drives backup + rollback).

        The two definite top-level surfaces — ``setforge.yaml`` (schema stamp) and
        the host-local ``local.yaml`` (span-declaration strip) — plus each folded
        ``(profile, fid)``'s reconcile legs (base / local-content / local-absent /
        drafts) and each touched profile's reconcile index, mirroring the
        disposition-retire cutover. Existence is NOT filtered (a missing path
        snapshots as absent and restores by deletion); the union is deduped
        preserving order. No spans / scalar-base legacy stores appear — those were
        already retired by the disposition cutover; this migration only folds the
        residual host-local SECTION surface into the unified store.
        """
        from setforge import base_store
        from setforge.reconcile import store as reconcile_store

        seen: dict[Path, None] = {roots.cfg_path: None, _local_yaml_path(roots): None}
        folds = _build_section_folds(roots)
        for fold in folds:
            key = str(fold.fid)
            for path in (
                base_store.base_path(fold.profile, key),
                reconcile_store.local_content_path(fold.profile, key),
                reconcile_store.local_absent_path(fold.profile, key),
                reconcile_store.drafts_manifest_path(fold.profile, key),
            ):
                seen.setdefault(path, None)
        for profile in {fold.profile for fold in folds}:
            seen.setdefault(reconcile_store.index_manifest_path(profile), None)
        return tuple(seen)

    def apply(self, *, roots: MigrationRoots) -> None:
        """Fold the residual host-local section surface, then stamp + strip.

        Pass 1 enumerates every ``(profile, fid)`` that carries a residual
        ``local.yaml`` host-local section and pre-flight-aborts if ANY section
        body lacks a markdown heading — the 4.0 store identity is heading-based, so
        a headingless body cannot mint a stable ``reloc_anchor`` (raises HERE,
        before any mutation). Pass 2 holds ALL profile locks across one arc: capture
        pre-state snapshots (reconcile legs + index), MERGE each residual section
        into its unit's LOCAL store (preserving the recorded ``local`` bytes AND the
        existing hunk classifications — INV-1 / INV-8), stamp schema 4.0, strip the
        retired ``host_local_sections`` / overlay-``spans`` from ``local.yaml``, then
        COMMIT the one durable transition (state_snapshots + the cfg/local.yaml text
        patch). Idempotent: a section already present as a LOCAL+reloc unit is
        skipped, so a re-run drains to an empty residual and no-ops.

        No ``local.yaml`` (the frozen-fixture case) ⇒ nothing to fold: advance
        the schema to 4.0 and record a stamp-only transition. Unlike the
        disposition cutover's empty-records branch (which could return without a
        transition because a LATER cutover owned the chain's record), THIS is the
        chain's TERMINAL ``writes_own_transition`` step, and the driver skips its
        own text transition whenever any chain step owns one
        (:func:`setforge.cli.migrate._chain_owns_transition`). So a bare early
        return would leave the 3.0 -> 4.0 stamp outside every transition, and a
        single ``revert`` would reverse an earlier step's ``-> 3.0`` patch against
        the on-disk 4.0 config and fail (INV-5: one revert reaches the origin).
        The recorded transition threads the chain-origin ``setforge.yaml`` (and
        ``local.yaml``) from ``pre_chain_snapshot`` and carries NO state_snapshots
        (nothing folded) — so it never overlaps an earlier cutover's store
        snapshots.
        """
        import contextlib

        from setforge import locking, transitions

        folds = _build_section_folds(roots)
        _validate_headings(folds)

        if not folds:
            self._record_stamp_only_transition(roots)
            return

        profiles = sorted({fold.profile for fold in folds})
        local_yaml = _local_yaml_path(roots)
        cfg_pre = roots.cfg_path.read_text(encoding="utf-8")
        local_pre = (
            local_yaml.read_text(encoding="utf-8") if local_yaml.exists() else None
        )

        with contextlib.ExitStack() as locks:
            for profile in profiles:
                locks.enter_context(locking.profile_lock(profile))

            snapshots = _capture_span_snapshots(folds, profiles)

            for fold in folds:
                _fold_sections(fold)

            _stamp_schema_version(roots.cfg_path, self.to_version)
            cfg_post = roots.cfg_path.read_text(encoding="utf-8")
            # Compute (don't write) the post-strip image so it can be
            # COMMITTED before the destructive strip lands (INV-5).
            local_post = _stripped_local_yaml_text(local_yaml)

            # A chain-threaded pre_chain_snapshot becomes file_pre so the
            # reverse delta reaches the chain's ORIGIN (INV-5), not just here.
            pre = roots.pre_chain_snapshot
            file_pre: dict[Path, str | None]
            file_post: dict[Path, str | None]
            if pre is not None:
                file_pre = dict(pre)
                file_post = dict(transitions.snapshot_paths(tuple(pre)))
                file_post[local_yaml] = local_post
            else:
                file_pre = {roots.cfg_path: cfg_pre, local_yaml: local_pre}
                file_post = {roots.cfg_path: cfg_post, local_yaml: local_post}

            _write_span_retire_transition(
                file_pre=file_pre,
                file_post=file_post,
                state_snapshots=snapshots,
            )

            # Destructive strip AFTER the transition commit (INV-5): must not
            # run until the transition that can restore it is durable.
            _write_stripped_local_yaml(local_yaml, local_post)

    def _record_stamp_only_transition(self, roots: MigrationRoots) -> None:
        """Stamp schema 4.0 and record the terminal cutover's stamp transition.

        The no-fold path (see :meth:`apply`). Covers ONLY ``setforge.yaml`` (and
        ``local.yaml`` when the chain touched it) — never a reconcile-store leg —
        so the text patch cannot overlap an earlier cutover's binary
        state_snapshots. Threads the chain-origin image from
        ``pre_chain_snapshot`` so a single ``revert`` reaches the chain's ORIGIN,
        not the intermediate 3.0 state (INV-5).
        """
        from setforge import transitions

        local_yaml = _local_yaml_path(roots)
        cfg_pre = roots.cfg_path.read_text(encoding="utf-8")
        _stamp_schema_version(roots.cfg_path, self.to_version)

        pre = roots.pre_chain_snapshot
        file_pre: dict[Path, str | None]
        if pre is not None:
            # Restrict the threaded image to the user-facing config files; a
            # store leg in pre_chain is restored by an earlier cutover's
            # state_snapshots, not by this text patch.
            file_pre = {roots.cfg_path: pre.get(roots.cfg_path, cfg_pre)}
            if local_yaml in pre:
                file_pre[local_yaml] = pre[local_yaml]
        else:
            file_pre = {roots.cfg_path: cfg_pre}
        file_post = dict(transitions.snapshot_paths(tuple(file_pre)))
        _write_span_retire_transition(
            file_pre=file_pre,
            file_post=file_post,
            state_snapshots=(),
        )


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


@dataclass(frozen=True, slots=True)
class _SectionFold:
    """One ``(profile, tracked-file)`` whose residual host-local sections fold in.

    Per ``(profile, fid)`` because the reconcile store is profile-scoped, even
    though the host-local section INTENT is declared once per tracked-file in
    ``local.yaml``. ``tracked_bytes`` is the DL1 base seed (verbatim tracked
    source, only used when the unit has no store entry yet); ``sections`` is the
    unified host-local section projection (legacy ``host_local_sections`` block
    PLUS migrated OVERLAY ``spans``) read via
    :func:`setforge.source.load_local_host_local_sections`.
    """

    profile: str
    name: str
    fid: FileId
    tracked_bytes: bytes
    sections: dict[str, HostLocalSection]


def _build_section_folds(roots: MigrationRoots) -> list[_SectionFold]:
    """Enumerate every ``(profile, fid)`` carrying a host-local section (read-only).

    Reads the unified host-local section projection from ``local.yaml`` (legacy
    ``host_local_sections`` + migrated OVERLAY ``spans``) via
    :func:`setforge.source.load_local_host_local_sections`, then walks the real
    profile-inheritance so a tracked-file shared across N profiles folds into each
    profile's store. Symlink tracked-files are skipped (they carry no section
    surface). No ``local.yaml`` (the frozen-fixture case) short-circuits to ``[]``
    before touching setforge.yaml, so the no-op stamp path never parses a config.
    """
    from setforge.compare import resolve_src
    from setforge.config import Config, resolve_profile
    from setforge.migrations._yaml_ops import yaml_rt
    from setforge.reconcile import file_id
    from setforge.source import load_local_host_local_sections

    local_yaml = _local_yaml_path(roots)
    if not local_yaml.exists():
        return []
    sections_by_tf = load_local_host_local_sections(local_yaml)
    if not sections_by_tf:
        return []

    yaml = yaml_rt()
    with roots.cfg_path.open("r", encoding="utf-8") as fh:
        raw = yaml.load(fh)
    if not isinstance(raw, dict):
        return []
    config = Config.model_validate(raw)

    folds: list[_SectionFold] = []
    for profile_name in config.profiles:
        for name in resolve_profile(config, profile_name).tracked_files:
            if name not in sections_by_tf:
                continue
            tracked_file = config.tracked_files[name]
            if tracked_file.symlink is not None:
                continue
            src = resolve_src(tracked_file, roots.repo_root)
            folds.append(
                _SectionFold(
                    profile=profile_name,
                    name=name,
                    fid=file_id(name),
                    tracked_bytes=src.read_bytes() if src.exists() else b"",
                    sections={str(k): v for k, v in sections_by_tf[name].items()},
                )
            )
    return folds


def _validate_headings(folds: list[_SectionFold]) -> None:
    """Pre-flight: every host-local section body must yield a heading, or abort.

    Part of plan-building, BEFORE any mutation — the 4.0 store identity is the
    body's markdown heading (``reloc_anchor``), so a headingless legacy
    ``host_local_sections`` body has no stable identity to fold onto (it could not
    round-trip out of the store, and a re-run could not tell it was already
    folded). A single offender raises here listing every one, so the cutover
    mutates nothing and no ``local.yaml`` is stripped half-folded. Deduped by
    ``(tracked_file, section_name)`` since a file shared across profiles repeats.
    """
    from setforge import reconcile
    from setforge.body_canon import canonical_body
    from setforge.host_local_inject import _read_body

    bad: dict[tuple[str, str], None] = {}
    for fold in folds:
        for section_name, section in fold.sections.items():
            body = canonical_body(_read_body(section)).encode("utf-8")
            if reconcile.section_heading_of_body(body) is None:
                bad.setdefault((fold.name, section_name), None)
    if bad:
        listing = "\n".join(
            f"  {tf}: host-local section {name!r} has no markdown heading"
            for tf, name in bad
        )
        raise ConfigError(
            "cannot migrate to schema 4.0: "
            f"{len(bad)} host-local section(s) have no markdown heading. The 4.0 "
            "store identity is heading-based (a stable reloc_anchor minted from the "
            "body's own heading), so a headingless section cannot be folded. Give "
            "each a leading markdown heading (e.g. '## My Notes') or remove it, then "
            "re-run (nothing was changed):\n" + listing
        )


def _fold_sections(fold: _SectionFold) -> None:
    """MERGE one unit's residual host-local sections into its LOCAL store.

    The merge (call inside the profile lock):

    1. ``already`` = sections already present as LOCAL+reloc units;
       ``residual`` = declared sections whose heading is not yet folded.
       Empty residual ⇒ return (idempotent — a re-run drains to nothing).
    2. ``base`` = the recorded merge base (or the tracked seed for a
       never-seeded unit); ``start`` = the recorded ``local`` bytes (preserving
       any shared drift the host edited but has not promoted) or ``base``.
    3. Inject each residual body (canonical) at its anchor into ``start`` →
       ``new_local``.
    4. :func:`~setforge.reconcile.record_local_reloc_sections` re-classifies
       ``base -> new_local`` against the existing stored rows (carrying every
       prior SHARED / LOCAL / SHARED_DRAFTED class forward untouched), marks only
       the freshly-injected section hunks LOCAL so their ``reloc_anchor`` is
       minted, and records the merged base+local+hunks (drafts preserved). Shared
       with the install-time template seed so the two mint identically.
    """
    from setforge import reconcile
    from setforge.anchors import Anchor
    from setforge.body_canon import canonical_body, inject_body_at_anchor
    from setforge.host_local_inject import _read_body
    from setforge.reconcile.host_local_view import host_local_sections_from_store

    profile, fid = fold.profile, fold.fid
    proj = host_local_sections_from_store(profile, fid)
    already_headings = set(proj.get(str(fid), {}))

    residual: list[tuple[str, str, Anchor]] = []
    for section in fold.sections.values():
        cbody = canonical_body(_read_body(section))
        heading = reconcile.section_heading_of_body(cbody.encode("utf-8"))
        # Defensive: _validate_headings already refused a headingless body.
        if heading is None or heading in already_headings:
            continue
        residual.append((heading, cbody, section.anchor))
    if not residual:
        return

    stored_base = reconcile.read_base(profile, fid)
    base = stored_base if stored_base is not None else fold.tracked_bytes
    stored_local = reconcile.read_local(profile, fid)
    start = stored_local if isinstance(stored_local, bytes) else base

    entry = reconcile.read_index(profile).files.get(str(fid))
    existing_hunks = list(entry.hunks) if entry is not None else []

    text = start.decode("utf-8")
    for _heading, cbody, anchor in residual:
        text = inject_body_at_anchor(text, anchor, cbody)
    new_local = text.encode("utf-8")

    residual_headings = {heading for heading, _, _ in residual}
    reconcile.record_local_reloc_sections(
        profile,
        fid,
        base=base,
        new_local=new_local,
        existing_hunks=existing_hunks,
        residual_headings=residual_headings,
    )


def _stamp_schema_version(cfg_path: Path, to_version: str) -> None:
    """Stamp ``schema_version: <to_version>`` in setforge.yaml (comment-safe).

    Overwrite-in-place preserves surviving key positions + comments; the single
    atomic write is crash-safe and captured in the cutover transition for
    byte-exact revert. Unlike the disposition cutover, this cutover strips NO
    setforge.yaml tracked-file keys — the retired host-local surface lives in
    ``local.yaml`` (see :func:`_stripped_local_yaml_text`), so this only advances
    the stamp. Raises
    :class:`ConfigError` on a non-mapping root.
    """
    from setforge.migrations import _require_mapping_root
    from setforge.migrations._yaml_ops import atomic_write_yaml, yaml_rt

    yaml = yaml_rt()
    with cfg_path.open("r", encoding="utf-8") as fh:
        data = yaml.load(fh)
    data = _require_mapping_root(data, cfg_path)
    data["schema_version"] = to_version
    atomic_write_yaml(cfg_path, data)


def _stripped_local_yaml_text(local_yaml: Path) -> str | None:
    """The text ``local.yaml`` WILL hold once the section surface is stripped.

    Pure (no write): loads the current document, drops the retired host-local
    SECTION surface from an in-memory ruamel round-trip copy, and returns the
    serialized result — so :meth:`SpanSurfaceRetireMigration.apply` can capture it
    as the transition's ``file_post`` and COMMIT before the destructive write
    lands (INV-5). ``None`` when the file is absent; the CURRENT text verbatim when
    nothing needs stripping (a section-free ``local.yaml`` — so ``file_post``
    matches the untouched on-disk bytes and the strip no-ops).

    Per tracked-file overlay: drops the legacy ``host_local_sections`` block and any
    OVERLAY (``kind: overlay``) ``spans`` entry — exactly the surface
    :func:`setforge.source._host_local_sections_from_raw` reads and this cutover
    folds into the store. Non-overlay spans (if any) and every other overlay knob
    (mode / dst / symlink_target) are left untouched. The ruamel round-trip
    preserves surrounding comments + key order.
    """
    import io
    from collections.abc import MutableMapping

    from setforge.migrations._yaml_ops import yaml_rt

    if not local_yaml.exists():
        return None
    current = local_yaml.read_text(encoding="utf-8")
    data = yaml_rt().load(current)
    if not isinstance(data, MutableMapping):
        return current
    tracked_files = data.get("tracked_files")
    if not isinstance(tracked_files, MutableMapping):
        return current

    changed = False
    for entry in tracked_files.values():
        if not isinstance(entry, MutableMapping):
            continue
        if entry.pop("host_local_sections", None) is not None:
            changed = True
        if _strip_overlay_spans(entry):
            changed = True
    if not changed:
        return current
    buf = io.StringIO()
    yaml_rt().dump(data, buf)
    return buf.getvalue()


def _write_stripped_local_yaml(local_yaml: Path, stripped_text: str | None) -> None:
    """Write the precomputed post-strip ``local.yaml`` text atomically (destructive).

    Called AFTER the transition commit so a crash in the strip window is
    recoverable from the just-written transition (INV-5). No-op when the file is
    absent (``stripped_text is None``) or unchanged (a section-free ``local.yaml``,
    so it is never churned). The destination permission bits are preserved.
    """
    import stat as stat_mod

    from setforge import atomicio

    if stripped_text is None or not local_yaml.exists():
        return
    if stripped_text == local_yaml.read_text(encoding="utf-8"):
        return
    dst_mode = stat_mod.S_IMODE(local_yaml.stat().st_mode)
    atomicio.atomic_write_text(local_yaml, stripped_text, mode=dst_mode)


def _strip_overlay_spans(entry: object) -> bool:
    """Drop every OVERLAY (``kind: overlay``) span from one overlay ``entry``.

    Deletes in-place (bottom-up so indices stay valid), removing the whole
    ``spans`` key when nothing survives. Returns whether anything changed.
    """
    from collections.abc import MutableMapping

    if not isinstance(entry, MutableMapping):
        return False
    spans = entry.get("spans")
    if not isinstance(spans, list):
        return False
    changed = False
    for idx in range(len(spans) - 1, -1, -1):
        span = spans[idx]
        if isinstance(span, MutableMapping) and span.get("kind") == "overlay":
            del spans[idx]
            changed = True
    if changed and not spans:
        entry.pop("spans", None)
    return changed


def _capture_span_snapshots(
    folds: list[_SectionFold], profiles: list[str]
) -> tuple[StateSnapshotEntry, ...]:
    """Snapshot every reconcile leg the fold mutates, BEFORE any mutation.

    Per ``(profile, fid)``: the byte BASE (shared with the unified store) plus the
    reconcile local-content / local-absent / drafts legs. The per-profile INDEX is
    captured ONCE, outside the fid loop, so a 2nd+ fid never records a
    post-mutation index. A never-seeded leg captures ``payload=None`` so revert
    deletes the seed; an already-present leg captures its bytes so revert restores
    them byte-exact (winning over the text patch for any overlapping path).
    """
    from setforge import transitions

    entries: list[StateSnapshotEntry] = []
    for fold in folds:
        key = str(fold.fid)
        entries.append(
            transitions.snapshot_store_state(
                transitions.SnapshotStore.BASE, fold.profile, key
            )
        )
        entries.extend(transitions.reconcile_file_snapshots(fold.profile, key))
    for profile in profiles:
        entries.append(
            transitions.snapshot_store_state(
                transitions.SnapshotStore.INDEX, profile, profile
            )
        )
    return tuple(entries)


def _write_span_retire_transition(
    *,
    file_pre: Mapping[Path, str | None],
    file_post: Mapping[Path, str | None],
    state_snapshots: tuple[StateSnapshotEntry, ...],
) -> TransitionDir:
    """Record the cutover's single durable ``MIGRATE`` transition.

    Carries BOTH a text patch for ``setforge.yaml`` + ``local.yaml``
    (``file_pre`` -> ``file_post``; the schema flip + section strip, reversed by
    ``patch -R``) AND the binary ``state_snapshots`` of every mutated reconcile
    leg (restored byte-exact by ``restore_state_snapshots``). ``apply`` calls this
    AFTER folding + stamping + stripping, so a crash or ``setforge revert`` after
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
