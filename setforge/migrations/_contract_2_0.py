"""The breaking 1.2 -> 2.0 CONTRACT migration (full-parity preserve_* drop).

This module is the SINGLE place legacy ``preserve_*`` knowledge survives. The
migration translates every surviving legacy file-preservation field into the
unified span model, drops the legacy keys, and stamps ``schema_version: "2.0"``:

- ``preserve_user_keys: [P, ...]`` -> one PINNED host-local span per path
  ``P`` (live-wins re-assert; the PINNED kind re-imposes live bytes every
  install + excludes the region from capture, exactly the legacy semantics).
- ``preserve_user_keys_deep: [P, ...]`` -> the same, with ``deep=True`` (the
  re-assert deep-merges instead of whole-replacing).
- ``preserve_user_sections: true`` -> one section span per marked section in
  the TRACKED src file (enumerated via :func:`setforge.sections.extract_sections`
  with ``allow_legacy=True``), each carrying ``capture_mode`` = the file's
  ``preserve_user_sections_mode``. No markers in src -> emit no span, still
  drop the flag (a clean converge).

It is CROSS-DOC: it also retires the ``local.yaml``
``tracked_files.<id>.preserve_user_keys`` add/remove overlay into span
overlays, so both ``setforge.yaml`` and ``local.yaml`` appear in
:meth:`Contract20Migration.affected_paths`.

The destructive drop is GATED behind an operator-declared
``minimum_version >= 2.0`` floor (the frozen
:data:`setforge.migrations.preserve_contract_schema_version`, via the shared
:func:`setforge.migrations._meets_floor`). Below the floor — or with no floor
declared at all — the migration refuses cleanly with :class:`ConfigError` and
mutates nothing.

The apply is ALL-OR-NOTHING: a full in-memory plan for every affected path is
built and the legacy translation completed before any write; the writes are then
performed under a snapshot+rollback guard so a mid-apply failure restores every
touched file to its pre-migration bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from setforge.errors import ConfigError, MarkerError
from setforge.migrations import (
    ManifestEntry,
    ManifestType,
    MigrationRoots,
    _meets_floor,
    _require_mapping_root,
    parse_schema_version,
    preserve_contract_schema_version,
)
from setforge.migrations._yaml_ops import atomic_write_yaml, yaml_rt
from setforge.sections import (
    SectionSemantics,
    extract_sections,
    section_semantics,
)

__all__ = ["Contract20Migration"]

_FROM_VERSION = "1.2"
_TO_VERSION = "2.0"

# local.yaml lives under ~/.config/setforge/ — derived from roots.home so the
# migration touches the same path the source layer reads (mirrors
# setforge.source.LOCAL_CONFIG_PATH but rooted on MigrationRoots.home).
_LOCAL_YAML_RELPATH = (".config", "setforge", "local.yaml")


def _local_yaml_path(roots: MigrationRoots) -> Path:
    """Return the host-local ``local.yaml`` path under ``roots.home``."""
    path = roots.home
    for part in _LOCAL_YAML_RELPATH:
        path = path / part
    return path


def _upsert_span(spans: CommentedSeq, entry: CommentedMap) -> None:
    """Append ``entry`` to ``spans`` unless an entry with its anchor exists.

    Anchor-keyed upsert (mirrors the override writer's dedup): the span
    ``anchor`` is the stable identity, so re-running the migration never
    appends a duplicate — the membership check makes the translation
    idempotent on replay.
    """
    anchor = entry["anchor"]
    for existing in spans:
        if isinstance(existing, CommentedMap) and existing.get("anchor") == anchor:
            return
    spans.append(entry)


def _spans_seq(tracked_file: CommentedMap) -> CommentedSeq:
    """Return ``tracked_file``'s ``spans`` sequence, creating it if absent."""
    spans = tracked_file.get("spans")
    if not isinstance(spans, CommentedSeq):
        spans = CommentedSeq()
        tracked_file["spans"] = spans
    return spans


def _structural_span(anchor: str, *, deep: bool) -> CommentedMap:
    """Build a PINNED host-local structural span for a preserve_user_keys path."""
    entry = CommentedMap()
    entry["anchor"] = anchor
    entry["kind"] = "pinned"
    entry["semantics"] = "host-local"
    if deep:
        entry["deep"] = True
    return entry


def _section_span(anchor: str, semantics: str, capture_mode: str) -> CommentedMap:
    """Build a section span carrying the file's capture_mode + marker semantics."""
    entry = CommentedMap()
    entry["anchor"] = anchor
    entry["kind"] = "pinned"
    entry["semantics"] = semantics
    entry["capture_mode"] = capture_mode
    return entry


def _del_if_present(node: CommentedMap, key: str) -> None:
    """Delete ``key`` from ``node`` only when present (idempotent guard)."""
    if key in node:
        del node[key]


def _set_disposition(tracked_file: CommentedMap, value: str) -> None:
    """Set ``disposition`` on ``tracked_file`` unless already present.

    Approach A: the translated spans are inert at install unless a file-level
    ``disposition`` is set (deploy consumes spans only on the disposition path
    + the base auto-seeds from live on first install). A pre-existing
    ``disposition`` (a natively-authored one) is left untouched so the migration
    never re-points an explicit operator choice; the conflict guard upstream
    rejects an incompatible legacy combination first.
    """
    if "disposition" not in tracked_file:
        tracked_file["disposition"] = value


def _translate_shallow_keys(tracked_file: CommentedMap) -> bool:
    """Translate ``preserve_user_keys`` -> PINNED host-local spans; drop the key.

    Returns ``True`` when at least one key was translated (the file must then
    take ``disposition: forked`` so the PINNED spans are consumed at install).
    """
    raw = tracked_file.get("preserve_user_keys")
    translated = False
    if isinstance(raw, list) and raw:
        spans = _spans_seq(tracked_file)
        for path in raw:
            _upsert_span(spans, _structural_span(str(path), deep=False))
        translated = True
    _del_if_present(tracked_file, "preserve_user_keys")
    return translated


def _translate_deep_keys(tracked_file: CommentedMap) -> bool:
    """Translate ``preserve_user_keys_deep`` -> PINNED+deep spans; drop the key.

    Returns ``True`` when at least one deep key was translated (the file must
    then take ``disposition: forked``).
    """
    raw = tracked_file.get("preserve_user_keys_deep")
    translated = False
    if isinstance(raw, list) and raw:
        spans = _spans_seq(tracked_file)
        for path in raw:
            _upsert_span(spans, _structural_span(str(path), deep=True))
        translated = True
    _del_if_present(tracked_file, "preserve_user_keys_deep")
    return translated


def _translate_sections(tracked_file: CommentedMap, roots: MigrationRoots) -> bool:
    """Translate ``preserve_user_sections`` -> spans by semantics; drop the flags.

    Section anchors are enumerated from the TRACKED src file's markers via
    :func:`setforge.sections.extract_sections` (``allow_legacy=True`` so a
    pre-hash tracked source still enumerates). No markers -> no span, still drop
    the flag (a clean converge). Each marked section is split by its semantics:

    * **shared** -> a section :class:`SpanEntry` (``kind: pinned``) carrying the
      file's ``capture_mode``; the file takes ``disposition: shared`` so the
      shared 3-way merge (with base auto-seed) is active at install.
    * **host-local** -> a markerless host-local OVERLAY span sourced from the
      TRACKED marker body (the shipped default), built via
      :func:`setforge.host_local_marker_migration.build_overlay_span_node`
      (``at-end-of-file`` anchor). OVERLAY spans are consumed on the
      ``disposition=None`` deploy path, so a host-local-only file gets NO
      disposition.

    Returns ``True`` when at least one SHARED section span was emitted (the
    signal that the file needs ``disposition: shared``). Host-local OVERLAY
    spans never set a disposition.
    """
    flag = tracked_file.get("preserve_user_sections")
    mode_raw = tracked_file.get("preserve_user_sections_mode")
    capture_mode = str(mode_raw) if mode_raw is not None else "keep_defaults"
    has_shared = False
    if flag is True:
        src_raw = tracked_file.get("src")
        if src_raw is not None:
            src = roots.repo_root / str(src_raw)
            if src.exists():
                has_shared = _translate_section_markers(tracked_file, src, capture_mode)
    _del_if_present(tracked_file, "preserve_user_sections")
    _del_if_present(tracked_file, "preserve_user_sections_mode")
    return has_shared


def _translate_section_markers(
    tracked_file: CommentedMap, src: Path, capture_mode: str
) -> bool:
    """Emit one span per marked section in ``src``; return whether any was shared."""
    text = src.read_text(encoding="utf-8")
    try:
        bodies = extract_sections(text, allow_legacy=True)
        semantics = section_semantics(text, allow_legacy=True)
    except MarkerError as exc:
        raise ConfigError(
            f"cannot enumerate user-sections in tracked src {src}: {exc}"
        ) from exc
    if not bodies:
        return False
    # Local import: build_overlay_span_node pulls the host-local inject stack
    # (overlay_inject -> host_local_inject -> source -> config), which would
    # form an import cycle if loaded at module scope (this module is imported
    # at the tail of setforge.migrations, itself imported by setforge.config).
    from setforge.host_local_marker_migration import build_overlay_span_node

    spans = _spans_seq(tracked_file)
    has_shared = False
    for key, body in bodies.items():
        sem = semantics.get(key, SectionSemantics.SHARED)
        if sem is SectionSemantics.HOST_LOCAL:
            _upsert_span(spans, build_overlay_span_node(key, body))
        else:
            _upsert_span(spans, _section_span(f"## {key}", sem.value, capture_mode))
            has_shared = True
    return has_shared


def _migrate_tracked_file_node(
    tracked_file: CommentedMap, roots: MigrationRoots
) -> None:
    """Translate one tracked_file's legacy preserve_* + set its disposition.

    The mapping (Approach A):

    * ``preserve_user_keys`` / ``preserve_user_keys_deep`` -> PINNED structural
      spans + ``disposition: forked``.
    * ``preserve_user_sections`` shared sections -> section spans +
      ``disposition: shared``; host-local sections -> OVERLAY spans (no
      disposition).

    A file carrying ``preserve_user_keys``/_deep AND a SHARED ``preserve_user_sections``
    section would need BOTH ``forked`` and ``shared`` on one file — an
    unrepresentable conflict. The author's real config never mixes them, so the
    migration REFUSES with a clear :class:`ConfigError` naming the file rather
    than silently picking one disposition.
    """
    has_keys = _translate_shallow_keys(tracked_file)
    has_keys = _translate_deep_keys(tracked_file) or has_keys
    has_shared_sections = _translate_sections(tracked_file, roots)
    if has_keys and has_shared_sections:
        raise ConfigError(
            "cannot migrate a tracked_file that combines preserve_user_keys / "
            "preserve_user_keys_deep (-> disposition: forked) with a SHARED "
            "preserve_user_sections section (-> disposition: shared): one file "
            "cannot carry both dispositions. Split the keys and the shared "
            "section across two tracked_files, then re-run the migration."
        )
    if has_keys:
        _set_disposition(tracked_file, "forked")
    elif has_shared_sections:
        _set_disposition(tracked_file, "shared")


def _migrate_setforge_yaml(data: CommentedMap, roots: MigrationRoots) -> None:
    """Translate every tracked_file's legacy preserve_* + stamp 2.0 in place."""
    tracked_files = data.get("tracked_files")
    if isinstance(tracked_files, CommentedMap):
        for tracked_file in tracked_files.values():
            if not isinstance(tracked_file, CommentedMap):
                continue
            _migrate_tracked_file_node(tracked_file, roots)
    # Overwrite-in-place so the key keeps its document position (idempotent).
    data["schema_version"] = _TO_VERSION


def _overlay_span(anchor: str) -> CommentedMap:
    """Build a PINNED host-local span overlay for a local.yaml preserved key."""
    return _structural_span(anchor, deep=False)


def _migrate_local_yaml(data: CommentedMap) -> None:
    """Retire local.yaml ``preserve_user_keys`` add/remove overlays -> span overlays.

    Each ``tracked_files.<id>.preserve_user_keys.add`` path becomes a PINNED
    host-local span overlay on that tracked_file's ``spans`` list. The ``remove``
    list has no span equivalent (it un-declares a key); it is dropped with the
    overlay block, since at 2.0 the host simply omits the span instead. The
    ``preserve_user_keys`` overlay block is then removed.
    """
    tracked_files = data.get("tracked_files")
    if not isinstance(tracked_files, CommentedMap):
        return
    for tracked_file in tracked_files.values():
        if not isinstance(tracked_file, CommentedMap):
            continue
        overlay = tracked_file.get("preserve_user_keys")
        if isinstance(overlay, CommentedMap):
            add = overlay.get("add")
            if isinstance(add, list):
                spans = _spans_seq(tracked_file)
                for path in add:
                    _upsert_span(spans, _overlay_span(str(path)))
        _del_if_present(tracked_file, "preserve_user_keys")


def _gate_floor(data: CommentedMap, path: Path) -> None:
    """Refuse the destructive drop unless minimum_version >= 2.0.

    Reads the RAW ``minimum_version`` and requires it to satisfy the frozen
    :data:`preserve_contract_schema_version` floor (a FULL major.minor compare
    via :func:`_meets_floor`). An absent floor — no operator attestation —
    refuses too: the contraction is irreversible on un-upgraded hosts, so it
    proceeds only on an explicit floor. Mirrors
    :func:`setforge.config._refuse_below_floor` but gates on the contract
    version, not the build's expected version.
    """
    raw_floor = data.get("minimum_version")
    if raw_floor is None:
        raise ConfigError(
            f"{path}: the 1.x -> {_TO_VERSION} contract drops the legacy "
            f"preserve_* fields irreversibly on hosts still reading the legacy "
            f"shape. Declare minimum_version >= {preserve_contract_schema_version} "
            f"to attest every host is upgraded before applying this migration."
        )
    floor = str(raw_floor)
    if not _meets_floor(floor, preserve_contract_schema_version):
        raise ConfigError(
            f"{path}: minimum_version {floor!r} is below the contract floor "
            f"{preserve_contract_schema_version!r} required to drop the legacy "
            f"preserve_* fields; raise minimum_version to "
            f">= {preserve_contract_schema_version} first."
        )


def _registry_min_version() -> str:
    """Return the lowest schema version the migration registry can resolve to.

    A lazy local import sidesteps the import cycle: this module is imported at
    the tail of :mod:`setforge.migrations` (before ``known_versions`` is even
    defined), but ``_lower_floor`` only ever runs from the reverse ``apply`` —
    long after the package finished initializing — so the symbol resolves.
    """
    from setforge.migrations import known_versions

    return min(known_versions(), key=parse_schema_version)


def _lower_floor(data: CommentedMap, to_version: str) -> None:
    """Lower a stale ``minimum_version`` floor so the down-migrated config loads.

    The forward contract GATES on ``minimum_version >= 2.0`` (the operator
    attestation that every host is upgraded) but never touched the floor when
    re-stamping. The reverse therefore left a config carrying
    ``schema_version: 1.2`` AND ``minimum_version: 2.0`` — which the very 1.2
    engine the downgrade exists to serve refuses, because its expected schema
    (1.2) is below the floor (2.0). The floor is an operator attestation, not
    user data, so the reverse owns restoring the floor state the config
    plausibly had before the forward contraction.

    This reverse is the ONLY chain step that touches the floor: the deeper
    same-major reverses (1.2 -> 1.1 -> 1.0) re-stamp ``schema_version`` but
    never the floor. So when this single step does NOT lower the floor low
    enough, a cross-major downgrade to 1.1 / 1.0 ends carrying a floor that
    still exceeds the target engine's expected schema, and that engine refuses
    the very config the downgrade existed to serve. Because this step cannot
    see the chain's ULTIMATE target, the lowering must clear every 1.x target:

    * A floor that satisfies the CONTRACT floor (``>= 2.0``) is the stale
      cross-major attestation. The reverse just restored a 1.x-loadable shape,
      so that attestation is wholesale invalid — lower it to the registry's
      LOWEST schema version, which can never lock out any 1.x target the
      downgrade serves (a target engine always satisfies a floor at or below
      the registry minimum).
    * A floor strictly between ``to_version`` and the contract floor (e.g. a
      hand-authored same-major ``1.5``) is ALSO lowered to the registry
      minimum. Lowering it only to ``to_version`` (this step's 1.2) would
      still lock out a chain whose ultimate target is 1.1 / 1.0, because the
      deeper same-major reverses never lower the floor further — the exact
      cross-major-downgrade lockout this helper exists to prevent. Since this
      step cannot see the ultimate target, the registry minimum is the only
      value guaranteed to satisfy every reachable 1.x target. (The residual
      cost is a floor that under-states the down-translated content's needs
      when the chain stops above the registry minimum; that only ever
      under-warns a sub-target engine, never blocks the intended target — the
      strictly safer failure direction. The fully precise fix would clamp the
      floor to the chain's final target in the chain driver, which this single
      step has no visibility into.)
    * A floor AT or BELOW ``to_version`` — including a hand-authored low
      same-major floor — is left untouched, since it never blocks the target.
    """
    raw_floor = data.get("minimum_version")
    if raw_floor is None:
        return
    floor = str(raw_floor)
    # Any floor ABOVE this step's to_version (whether the cross-major contract
    # floor or a hand-authored same-major floor) is lowered to the registry
    # minimum — see the docstring for why to_version alone is insufficient. A
    # floor at/below to_version never blocks the target and is left untouched.
    if parse_schema_version(floor) > parse_schema_version(to_version):
        data["minimum_version"] = _registry_min_version()


@dataclass(slots=True, frozen=True)
class Contract20Migration:
    """The breaking 1.2 -> 2.0 contract: translate preserve_* -> spans, then drop.

    See the module docstring for the full translation table, the cross-doc
    local.yaml retirement, the floor gate, and the all-or-nothing apply.
    """

    from_version: str = _FROM_VERSION
    to_version: str = _TO_VERSION

    @property
    def reverse(self) -> _Contract20Reverse:
        """The inverse 2.0 -> 1.2 migration (untranslate 2.0-exclusive spans)."""
        return _Contract20Reverse(
            from_version=self.to_version, to_version=self.from_version
        )

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        """Two EDIT entries: the setforge.yaml + local.yaml in-place rewrites."""
        return (
            ManifestEntry(
                type=ManifestType.EDIT,
                description=(
                    "translate legacy preserve_* -> spans and drop them; "
                    f"stamp schema_version {self.to_version!r}"
                ),
                affected_path=roots.cfg_path,
            ),
            ManifestEntry(
                type=ManifestType.EDIT,
                description="retire local.yaml preserve_user_keys overlay -> spans",
                affected_path=_local_yaml_path(roots),
            ),
        )

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        """Both the setforge.yaml and the host-local local.yaml."""
        return (roots.cfg_path, _local_yaml_path(roots))

    def apply(self, *, roots: MigrationRoots) -> None:
        """Translate + drop legacy preserve_* across both docs, all-or-nothing.

        Builds the full in-memory plan for every affected path (gating the
        destructive drop on the minimum_version floor BEFORE any mutation),
        then writes each planned document under a snapshot+rollback guard so a
        mid-apply failure restores every touched file to its pre-migration bytes.
        """
        yaml = yaml_rt()
        local_path = _local_yaml_path(roots)

        # --- Build the in-memory plan (no writes yet). ---
        plan: list[tuple[Path, CommentedMap]] = []

        with roots.cfg_path.open("r", encoding="utf-8") as fh:
            cfg_data = yaml.load(fh)
        cfg_data = _require_mapping_root(cfg_data, roots.cfg_path)
        # Floor gate FIRST — refuse + mutate nothing below the contract floor.
        _gate_floor(cfg_data, roots.cfg_path)
        # Validate the declared version parses cleanly (clean ConfigError, not
        # a raw traceback, on a malformed schema_version).
        raw_version = cfg_data.get("schema_version")
        if raw_version is not None:
            parse_schema_version(str(raw_version))
        _migrate_setforge_yaml(cfg_data, roots)
        plan.append((roots.cfg_path, cfg_data))

        if local_path.exists():
            with local_path.open("r", encoding="utf-8") as fh:
                local_data = yaml.load(fh)
            if isinstance(local_data, CommentedMap):
                _migrate_local_yaml(local_data)
                plan.append((local_path, local_data))

        # --- Write the plan under a snapshot+rollback guard. ---
        snapshots: dict[Path, bytes | None] = {
            path: (path.read_bytes() if path.exists() else None) for path, _ in plan
        }
        written: list[Path] = []
        try:
            for path, doc in plan:
                atomic_write_yaml(path, doc)
                written.append(path)
        except BaseException:
            for path in written:
                snapshot = snapshots[path]
                if snapshot is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(snapshot)
            raise


@dataclass(slots=True, frozen=True)
class _Contract20Reverse:
    """The inverse 2.0 -> 1.2 migration: untranslate ONLY 2.0-exclusive spans.

    Re-adds the legacy preserve_* fields as needed and untranslates the spans
    the forward migration created — but ONLY the 2.0-exclusive ones (deep
    structural spans, and section spans carrying a ``capture_mode``). Plain
    PINNED / FORKED / OVERLAY spans (which were valid under 1.2 already) STAY as
    spans: they are behavior-equivalent at 1.2, and there is no provenance field
    to tell a migration-origin plain span from a natively-authored one, so the
    reverse leaves them alone. Stamps ``schema_version: "1.2"``.

    The round-trip restores schema SHAPE + behavior-equivalence, NOT byte
    identity (ruamel normalizes on the first load->dump).
    """

    from_version: str = _TO_VERSION
    to_version: str = _FROM_VERSION

    @property
    def reverse(self) -> Contract20Migration:
        """The forward 1.2 -> 2.0 migration — keeps the Protocol symmetric."""
        return Contract20Migration(
            from_version=self.to_version, to_version=self.from_version
        )

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        return (
            ManifestEntry(
                type=ManifestType.EDIT,
                description=(
                    "untranslate 2.0-exclusive spans -> legacy preserve_*; "
                    f"stamp schema_version {self.to_version!r}"
                ),
                affected_path=roots.cfg_path,
            ),
        )

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        return (roots.cfg_path,)

    def apply(self, *, roots: MigrationRoots) -> None:
        """Untranslate 2.0-exclusive spans + stamp 1.2, all-or-nothing."""
        yaml = yaml_rt()
        with roots.cfg_path.open("r", encoding="utf-8") as fh:
            data = yaml.load(fh)
        data = _require_mapping_root(data, roots.cfg_path)
        tracked_files = data.get("tracked_files")
        if isinstance(tracked_files, CommentedMap):
            for tracked_file in tracked_files.values():
                if isinstance(tracked_file, CommentedMap):
                    _untranslate_tracked_file(tracked_file)
        data["schema_version"] = self.to_version
        _lower_floor(data, self.to_version)

        snapshot = roots.cfg_path.read_bytes() if roots.cfg_path.exists() else None
        try:
            atomic_write_yaml(roots.cfg_path, data)
        except BaseException:
            if snapshot is None:
                roots.cfg_path.unlink(missing_ok=True)
            else:
                roots.cfg_path.write_bytes(snapshot)
            raise


def _untranslate_tracked_file(tracked_file: CommentedMap) -> None:
    """Untranslate a tracked_file's 2.0-exclusive spans -> legacy preserve_*.

    A ``deep=True`` structural span becomes a ``preserve_user_keys_deep`` entry;
    a section span carrying ``capture_mode`` becomes ``preserve_user_sections:
    true`` (+ ``preserve_user_sections_mode`` when not the default). Both are
    removed from ``spans``. Plain spans are left untouched (behavior-equivalent
    at 1.2). An emptied ``spans`` list is dropped.
    """
    spans = tracked_file.get("spans")
    if not isinstance(spans, CommentedSeq):
        return
    deep_paths, section_mode, kept = _partition_untranslatable_spans(spans)
    _readd_deep_keys(tracked_file, deep_paths)
    _readd_section_flags(tracked_file, section_mode)
    # Drop the disposition the forward migration added in LOCKSTEP with the
    # legacy field it carried: a deep span rode disposition=forked; a
    # capture_mode section span rode disposition=shared. Re-adding the legacy
    # field restores the 1.2 behavior, so the migration-added disposition must
    # go (it has no representation at the legacy field's altitude). A disposition
    # on a file with NO untranslated span is left alone — it is either native or
    # rides a plain span that the reverse keeps (behavior-equivalent at 1.2).
    #
    # BUT a kept structural span (the PINNED span a shallow ``preserve_user_keys``
    # was translated into, when the file ALSO carried a deep key) is INERT at
    # install unless ``disposition: forked`` stays set — deploy consumes
    # structural spans only on the disposition path. So only drop the forked
    # disposition when no forked-requiring span remains in ``kept``; otherwise
    # the kept shallow span would survive but stop preserving its key.
    if deep_paths and not _kept_needs_forked(kept):
        _drop_disposition(tracked_file, "forked")
    if section_mode is not None:
        _drop_disposition(tracked_file, "shared")
    _replace_or_drop_spans(tracked_file, kept)


def _kept_needs_forked(kept: list[object]) -> bool:
    """Return ``True`` when a kept span still requires ``disposition: forked``.

    A kept structural span (a ``CommentedMap`` span that is not a section span —
    section spans carry ``capture_mode`` and are untranslated separately) is a
    PINNED/FORKED span that deploy consumes only on the disposition path, so it
    is inert at install unless the file keeps ``disposition: forked``.
    """
    return any(
        isinstance(span, CommentedMap) and "capture_mode" not in span for span in kept
    )


def _drop_disposition(tracked_file: CommentedMap, expected: str) -> None:
    """Remove ``disposition`` from ``tracked_file`` when it equals ``expected``.

    Guards on the value so the reverse never strips a disposition that does NOT
    match the one the forward migration would have set (a hand-authored
    disposition on a span the reverse untranslated stays put).
    """
    if tracked_file.get("disposition") == expected:
        del tracked_file["disposition"]


def _partition_untranslatable_spans(
    spans: CommentedSeq,
) -> tuple[list[str], str | None, list[object]]:
    """Split ``spans`` into deep paths, section capture_mode, and kept-plain spans.

    Returns the deep-span anchors (-> preserve_user_keys_deep), the section
    span's capture_mode (-> preserve_user_sections, ``None`` when no section
    span), and the plain spans that stay (behavior-equivalent at 1.2).
    """
    deep_paths: list[str] = []
    section_mode: str | None = None
    kept: list[object] = []
    for span in spans:
        if not isinstance(span, CommentedMap):
            kept.append(span)
        elif span.get("deep") is True:
            deep_paths.append(str(span.get("anchor")))
        elif "capture_mode" in span:
            section_mode = str(span.get("capture_mode"))
        else:
            kept.append(span)
    return deep_paths, section_mode, kept


def _readd_deep_keys(tracked_file: CommentedMap, deep_paths: list[str]) -> None:
    """Re-add ``deep_paths`` to ``preserve_user_keys_deep`` (dedup-guarded)."""
    if not deep_paths:
        return
    existing = tracked_file.get("preserve_user_keys_deep")
    bucket = existing if isinstance(existing, list) else CommentedSeq()
    for path in deep_paths:
        if path not in bucket:
            bucket.append(path)
    tracked_file["preserve_user_keys_deep"] = bucket


def _readd_section_flags(tracked_file: CommentedMap, section_mode: str | None) -> None:
    """Re-add ``preserve_user_sections`` (+ mode when non-default)."""
    if section_mode is None:
        return
    tracked_file["preserve_user_sections"] = True
    if section_mode != "keep_defaults":
        tracked_file["preserve_user_sections_mode"] = section_mode


def _replace_or_drop_spans(tracked_file: CommentedMap, kept: list[object]) -> None:
    """Replace ``spans`` with the kept plain spans, or drop it when none remain."""
    if kept:
        new_spans = CommentedSeq()
        for span in kept:
            new_spans.append(span)
        tracked_file["spans"] = new_spans
    elif "spans" in tracked_file:
        del tracked_file["spans"]
