"""The 5.0 -> 6.0 profile-fields-retire cutover (fold legacy fields -> packages).

This module is the SINGLE place the four legacy per-profile
package/reconcile fields survive. The migration folds each profile's

- ``cargo_binaries: [C, ...]`` -> a top-level ``packages`` entry
  ``C: {type: cargo, crate: C}`` + a ``C`` ref in the profile's ``packages``,
- ``claude_plugins: [P, ...]`` -> ``P: {type: plugin, plugin: P}`` + a ref,
- ``extensions.include: [E, ...]`` -> ``E: {type: extension, extension: E}`` +
  a ref,
- ``plugins_reconcile: <policy>`` -> ``reconcile.plugins.policy``,
- ``extensions.{exclude, reconcile}`` -> ``reconcile.extensions.{exclude,
  policy}``,

drops the four legacy keys, and stamps ``schema_version: '6.0'``. Each
profile's ``packages`` ref-list is ordered cargo -> plugin -> extension.

The minted top-level ``packages`` entries share the flat namespace, so two
profiles that mint the SAME key are checked: identical body -> the single
shared entry is reused (idempotent); a DIFFERENT body under the same key is a
collision the migration REFUSES with a clear :class:`~setforge.errors.ConfigError`
rather than silently merging.

The destructive drop is GATED behind an operator-declared ``minimum_version
>= 6.0`` floor (the frozen
:data:`setforge.migrations.package_contract_schema_version`, via the shared
:func:`setforge.migrations._meets_floor`). Below the floor — or with no floor
declared at all — the migration refuses cleanly with :class:`ConfigError` and
mutates nothing.

The apply is ALL-OR-NOTHING: a full in-memory plan is built and the fold +
collision guard completed before any write; the single write is then performed
under a snapshot+rollback guard so a mid-apply failure restores the file to its
pre-migration bytes.

Transition ownership (INV-5): this is a CHAIN-TERMINAL cutover. The forward
:meth:`apply` records its OWN durable transition threading the driver's
``pre_chain_snapshot`` as ``file_pre`` (so ONE ``revert`` reaches the chain's
byte-exact ORIGIN), re-snapshots the transformed image as ``file_post``, and
carries NO ``state_snapshots`` — this migration folds nothing into a binary
store, so the origin-threaded text patch is the sole reverse authority. So
``writes_own_transition`` is ``True`` on forward, ``False`` on reverse (a
single-step down-migration the migrate driver records for).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from setforge.errors import ConfigError
from setforge.migrations import (
    ManifestEntry,
    ManifestType,
    MigrationRoots,
    _meets_floor,
    _require_mapping_root,
    package_contract_schema_version,
    parse_schema_version,
)
from setforge.migrations._yaml_ops import atomic_write_yaml, yaml_rt

if TYPE_CHECKING:
    from setforge.transitions import TransitionDir

__all__ = ["ProfileFieldsRetireMigration"]

_FROM_VERSION = "5.0"
_TO_VERSION = "6.0"

# The three package kinds each legacy field folds into, in the order a
# profile's `packages` ref-list is built (cargo -> plugin -> extension).
_CARGO_KIND = "cargo"
_PLUGIN_KIND = "plugin"
_EXTENSION_KIND = "extension"

# The four legacy per-profile fields this cutover folds away and drops.
_LEGACY_FIELDS = (
    "cargo_binaries",
    "claude_plugins",
    "plugins_reconcile",
    "extensions",
)


def _package_body(kind: str, name: str) -> CommentedMap:
    """Build the minted top-level ``packages`` entry for one legacy value.

    Each kind carries its discriminator plus the single kind-specific field the
    live model requires: ``crate`` (cargo), ``plugin`` (plugin), ``extension``
    (extension) — always equal to ``name`` (the legacy list was already the
    bare identifier).
    """
    body = CommentedMap()
    body["type"] = kind
    body[
        {_CARGO_KIND: "crate", _PLUGIN_KIND: "plugin", _EXTENSION_KIND: "extension"}[
            kind
        ]
    ] = name
    return body


def _packages_registry(data: CommentedMap, cfg_path: Path) -> CommentedMap:
    """Return the top-level ``packages`` mapping, creating it if absent.

    A missing ``packages:`` is created empty. A present-but-non-mapping
    ``packages:`` (a hand-edited list/scalar) is refused with a clean
    :class:`ConfigError` rather than clobbered — replacing it with a fresh
    empty map would discard the user's data, violating the module's
    all-or-nothing / refuse-cleanly contract.
    """
    packages = data.get("packages")
    if packages is None:
        packages = CommentedMap()
        data["packages"] = packages
        return packages
    if not isinstance(packages, CommentedMap):
        raise ConfigError(
            f"cannot fold legacy profile fields: top-level 'packages' in "
            f"{cfg_path} must be a mapping, got {type(packages).__name__}. "
            f"Fix the 'packages:' block (or remove it) and re-run "
            f"(nothing was changed)."
        )
    return packages


def _mint_package(
    registry: CommentedMap, name: str, body: CommentedMap, cfg_path: Path
) -> None:
    """Register ``name -> body`` in ``registry``, deduping / guarding collisions.

    Identical body under an existing key -> reuse (idempotent across profiles).
    A DIFFERENT body under the same key is a collision across the shared flat
    namespace, refused with a :class:`ConfigError` naming the clash and telling
    the operator to rename one side rather than silently merging.
    """
    existing = registry.get(name)
    if existing is None:
        registry[name] = body
        return
    if dict(existing) != dict(body):
        raise ConfigError(
            f"{cfg_path}: cannot fold legacy profile fields — two profiles mint "
            f"the top-level packages key {name!r} with DIFFERENT bodies "
            f"({dict(existing)} vs {dict(body)}). The packages namespace is flat, "
            f"so rename one of the clashing entries and re-run (nothing was changed)."
        )


def _translate_profile(
    profile: CommentedMap, registry: CommentedMap, cfg_path: Path
) -> None:
    """Fold one profile's four legacy fields into packages + reconcile.

    Mints top-level ``packages`` entries (deduped + collision-guarded), builds
    the profile's ``packages`` ref-list in cargo -> plugin -> extension order,
    threads ``plugins_reconcile`` + ``extensions.{exclude,reconcile}`` into a
    ``reconcile`` block, then drops the four legacy keys. A profile carrying
    none of the four is left untouched.
    """
    refs: list[str] = []
    for kind, field in (
        (_CARGO_KIND, "cargo_binaries"),
        (_PLUGIN_KIND, "claude_plugins"),
    ):
        for name in _legacy_list(profile, field):
            _mint_package(registry, name, _package_body(kind, name), cfg_path)
            refs.append(name)

    extensions = profile.get("extensions")
    if isinstance(extensions, CommentedMap):
        for name in _seq_of_str(extensions.get("include")):
            _mint_package(
                registry, name, _package_body(_EXTENSION_KIND, name), cfg_path
            )
            refs.append(name)

    _thread_reconcile(profile, extensions)

    if refs:
        _extend_packages_ref(profile, refs)

    for legacy in _LEGACY_FIELDS:
        if legacy in profile:
            del profile[legacy]


def _thread_reconcile(profile: CommentedMap, extensions: object) -> None:
    """Fold ``plugins_reconcile`` + ``extensions.{exclude,reconcile}`` -> reconcile.

    ``plugins_reconcile`` -> ``reconcile.plugins.policy``;
    ``extensions.exclude`` / ``extensions.reconcile`` ->
    ``reconcile.extensions.{exclude, policy}``. Only writes a sub-block when the
    legacy source is present, so a profile with no plugin/extension reconcile
    intent grows no empty ``reconcile`` scaffolding.
    """
    plugins_policy = profile.get("plugins_reconcile")
    ext_exclude = (
        _seq_of_str(extensions.get("exclude"))
        if isinstance(extensions, CommentedMap)
        else []
    )
    ext_policy = (
        extensions.get("reconcile") if isinstance(extensions, CommentedMap) else None
    )

    if plugins_policy is None and not ext_exclude and ext_policy is None:
        return

    reconcile = profile.get("reconcile")
    if not isinstance(reconcile, CommentedMap):
        reconcile = CommentedMap()
        profile["reconcile"] = reconcile

    if plugins_policy is not None:
        plugins = CommentedMap()
        plugins["policy"] = plugins_policy
        reconcile["plugins"] = plugins

    if ext_exclude or ext_policy is not None:
        ext_block = CommentedMap()
        if ext_exclude:
            ext_block["exclude"] = _str_seq(ext_exclude)
        if ext_policy is not None:
            ext_block["policy"] = ext_policy
        reconcile["extensions"] = ext_block


def _extend_packages_ref(profile: CommentedMap, refs: list[str]) -> None:
    """Append ``refs`` to the profile's ``packages`` list (creating it, deduped)."""
    packages = profile.get("packages")
    if not isinstance(packages, CommentedSeq):
        packages = CommentedSeq()
        profile["packages"] = packages
    for ref in refs:
        if ref not in packages:
            packages.append(ref)


def _legacy_list(profile: CommentedMap, field: str) -> list[str]:
    """Return ``profile[field]`` as a list of strings (empty when absent)."""
    return _seq_of_str(profile.get(field))


def _seq_of_str(raw: object) -> list[str]:
    """Coerce a ruamel sequence to a plain ``list[str]`` (empty when not a list)."""
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return []


def _str_seq(items: list[str]) -> CommentedSeq:
    """Build a ``CommentedSeq`` from ``items`` (a fresh minted sequence)."""
    seq = CommentedSeq()
    for item in items:
        seq.append(item)
    return seq


def _gate_floor(data: CommentedMap, path: Path) -> None:
    """Refuse the destructive drop unless minimum_version >= 6.0.

    Reads the RAW ``minimum_version`` and requires it to satisfy the frozen
    :data:`package_contract_schema_version` floor (a FULL major.minor compare
    via :func:`_meets_floor`). An absent floor — no operator attestation —
    refuses too: the contraction is irreversible on un-upgraded hosts, so it
    proceeds only on an explicit floor. Mirrors
    :func:`setforge.migrations._contract_2_0._gate_floor` but gates on the
    package-contract version, not the build's expected version.
    """
    raw_floor = data.get("minimum_version")
    if raw_floor is None:
        raise ConfigError(
            f"{path}: the 5.x -> {_TO_VERSION} contract drops the legacy "
            f"per-profile package/reconcile fields irreversibly on hosts still "
            f"reading the legacy shape. Declare minimum_version >= "
            f"{package_contract_schema_version} to attest every host is upgraded "
            f"before applying this migration."
        )
    floor = str(raw_floor)
    if not _meets_floor(floor, package_contract_schema_version):
        raise ConfigError(
            f"{path}: minimum_version {floor!r} is below the contract floor "
            f"{package_contract_schema_version!r} required to drop the legacy "
            f"per-profile package/reconcile fields; raise minimum_version to "
            f">= {package_contract_schema_version} first."
        )


def _migrate_setforge_yaml(data: CommentedMap, roots: MigrationRoots) -> None:
    """Fold every profile's legacy fields + stamp 6.0 in place."""
    registry = _packages_registry(data, roots.cfg_path)
    profiles = data.get("profiles")
    if isinstance(profiles, CommentedMap):
        for profile in profiles.values():
            if isinstance(profile, CommentedMap):
                _translate_profile(profile, registry, roots.cfg_path)
    # A registry created but never minted into is dropped so a legacy-free
    # config does not grow an empty `packages: {}` key.
    if not registry and "packages" in data:
        del data["packages"]
    # Overwrite-in-place so the key keeps its document position (idempotent).
    data["schema_version"] = _TO_VERSION


def _build_stamp_transition_images(
    roots: MigrationRoots, cfg_pre: str
) -> tuple[dict[Path, str | None], dict[Path, str | None]]:
    """Compute the terminal transition's ``file_pre`` / ``file_post`` images.

    Threads the driver's chain-origin image (``pre_chain_snapshot``) as
    ``file_pre`` so a single ``revert`` reaches the chain's ORIGIN (INV-5), not
    the intermediate 5.0 state; ``file_post`` re-snapshots the SAME paths now
    (schema 6.0). Applied OUTSIDE the driver (``pre_chain_snapshot is None``) it
    is a plain ``cfg_pre`` -> current-image restamp of ``setforge.yaml`` alone.
    Pure — reads the on-disk transformed doc but writes nothing.
    """
    from setforge import transitions

    pre = roots.pre_chain_snapshot
    if pre is not None:
        file_pre = dict(pre)
        file_post = dict(transitions.snapshot_paths(tuple(pre)))
    else:
        file_pre = {roots.cfg_path: cfg_pre}
        file_post = {roots.cfg_path: roots.cfg_path.read_text(encoding="utf-8")}
    return file_pre, file_post


def _write_stamp_transition(roots: MigrationRoots, cfg_pre: str) -> TransitionDir:
    """Record the terminal cutover's origin-threading stamp transition.

    Carries NO ``state_snapshots`` — with none to override it, the threaded text
    patch is the sole reverse authority, so ``patch -R`` restores the config to
    its pre-chain origin (see the module docstring's INV-5 note). Returns the
    committed transition directory.
    """
    import sys

    from setforge import transitions
    from setforge._redact import redact_argv

    file_pre, file_post = _build_stamp_transition_images(roots, cfg_pre)
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
        state_snapshots=(),
    )


@dataclass(slots=True, frozen=True)
class ProfileFieldsRetireMigration:
    """The breaking 5.0 -> 6.0 contract: fold legacy profile fields -> packages.

    See the module docstring for the full fold table, the collision guard, the
    floor gate, the all-or-nothing apply, and the terminal-transition ownership.
    """

    from_version: str = _FROM_VERSION
    to_version: str = _TO_VERSION

    @property
    def writes_own_transition(self) -> bool:
        """Terminal step: records its own origin-threading stamp transition."""
        return True

    @property
    def reverse(self) -> _ProfileFieldsRetireReverse:
        """The inverse 6.0 -> 5.0 migration (unfold packages -> legacy fields)."""
        return _ProfileFieldsRetireReverse(
            from_version=self.to_version, to_version=self.from_version
        )

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        """Single EDIT: the setforge.yaml fold + schema stamp."""
        return (
            ManifestEntry(
                type=ManifestType.EDIT,
                description=(
                    "fold legacy per-profile cargo_binaries / claude_plugins / "
                    "plugins_reconcile / extensions -> packages + reconcile; "
                    f"stamp schema_version {self.to_version!r}"
                ),
                affected_path=roots.cfg_path,
            ),
        )

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        """Only ``setforge.yaml`` is touched."""
        return (roots.cfg_path,)

    def apply(self, *, roots: MigrationRoots) -> None:
        """Fold the legacy profile fields, stamp 6.0, then commit the transition.

        Builds the full in-memory plan (gating the destructive drop on the
        minimum_version floor BEFORE any mutation, running the collision guard
        during the fold), writes the transformed document under a
        snapshot+rollback guard, then records its own terminal transition
        (INV-5).
        """
        yaml = yaml_rt()
        cfg_pre = roots.cfg_path.read_text(encoding="utf-8")

        # --- Build the in-memory plan (no writes yet). ---
        with roots.cfg_path.open("r", encoding="utf-8") as fh:
            data = yaml.load(fh)
        data = _require_mapping_root(data, roots.cfg_path)
        # Floor gate FIRST — refuse + mutate nothing below the contract floor.
        _gate_floor(data, roots.cfg_path)
        raw_version = data.get("schema_version")
        if raw_version is not None:
            parse_schema_version(str(raw_version))
        # Collision guard runs inside the fold (raises before any write).
        _migrate_setforge_yaml(data, roots)

        # --- Write under a snapshot+rollback guard. ---
        snapshot = roots.cfg_path.read_bytes() if roots.cfg_path.exists() else None
        try:
            atomic_write_yaml(roots.cfg_path, data)
        except BaseException:
            if snapshot is None:
                roots.cfg_path.unlink(missing_ok=True)
            else:
                roots.cfg_path.write_bytes(snapshot)
            raise

        _write_stamp_transition(roots, cfg_pre)


@dataclass(slots=True, frozen=True)
class _ProfileFieldsRetireReverse:
    """The inverse 6.0 -> 5.0 migration: unfold packages -> legacy profile fields.

    For each profile, only the ``packages`` refs whose top-level entry has the
    canonical MINTED shape the forward produces (:func:`_minted_field`) are
    reconstructed into ``cargo_binaries`` / ``claude_plugins`` /
    ``extensions.include`` and dropped from the top-level ``packages`` registry;
    ``reconcile.plugins.policy`` -> ``plugins_reconcile`` and
    ``reconcile.extensions.{exclude, policy}`` -> ``extensions.{exclude,
    reconcile}``. Because the ``packages`` model already existed at 5.0, a profile
    may legitimately carry NATIVE refs (``python`` / ``go`` / ``github_release`` /
    ``local``, or a native cargo/plugin/extension whose body isn't the identity
    form) — those are PRESERVED in place with their top-level entries intact, so
    the reverse NEVER silently drops data it did not mint. ``schema_version`` is
    RESTAMPED to ``5.0`` in place (never stripped — a stripped key would misread
    as the 1.0 baseline).

    A stale ``minimum_version`` floor above 5.0 is lowered so the down-migrated
    config loads on the 5.x engine the downgrade serves (mirrors
    :func:`setforge.migrations._contract_2_0._lower_floor`).

    The round-trip restores schema SHAPE + values, NOT byte identity (ruamel
    normalizes on the first load->dump).
    """

    from_version: str = _TO_VERSION
    to_version: str = _FROM_VERSION

    @property
    def writes_own_transition(self) -> bool:
        """Single-step down-migration: the migrate driver records the transition."""
        return False

    @property
    def reverse(self) -> ProfileFieldsRetireMigration:
        """The forward 5.0 -> 6.0 migration — keeps the Protocol symmetric."""
        return ProfileFieldsRetireMigration(
            from_version=self.to_version, to_version=self.from_version
        )

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        return (
            ManifestEntry(
                type=ManifestType.EDIT,
                description=(
                    "unfold packages + reconcile -> legacy per-profile fields; "
                    f"stamp schema_version {self.to_version!r}"
                ),
                affected_path=roots.cfg_path,
            ),
        )

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        return (roots.cfg_path,)

    def apply(self, *, roots: MigrationRoots) -> None:
        """Unfold packages -> legacy fields + restamp 5.0, all-or-nothing."""
        yaml = yaml_rt()
        with roots.cfg_path.open("r", encoding="utf-8") as fh:
            data = yaml.load(fh)
        data = _require_mapping_root(data, roots.cfg_path)

        registry = data.get("packages")
        registry = registry if isinstance(registry, CommentedMap) else CommentedMap()
        minted: set[str] = set()
        profiles = data.get("profiles")
        if isinstance(profiles, CommentedMap):
            for profile in profiles.values():
                if isinstance(profile, CommentedMap):
                    _untranslate_profile(profile, registry, minted)
        _drop_minted_packages(data, registry, minted)

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


def _minted_field(entry: object, ref: str) -> str | None:
    """Return the legacy kind for ``entry`` IFF it has the canonical minted shape.

    The forward mints exactly the identity body ``{type: cargo, crate: <key>}`` /
    ``{type: plugin, plugin: <key>}`` / ``{type: extension, extension: <key>}``
    where the inner field value equals the registry key (== the ref id). A ref is
    treated as minted — reconstructed into a legacy field and dropped from the
    top-level registry — ONLY when its entry matches that exact two-key shape.

    Anything else is PRESERVED in place: a native ``python`` / ``go`` /
    ``github_release`` / ``local`` package, a native cargo/plugin/extension whose
    body carries extra fields or a non-identity value, or a dangling ref with no
    top-level entry. Returning ``None`` for those is what keeps the reverse
    lossless — it never reconstructs (and so never drops) a ref it did not mint.
    """
    if not isinstance(entry, CommentedMap):
        return None
    kind = entry.get("type")
    field = {
        _CARGO_KIND: "crate",
        _PLUGIN_KIND: "plugin",
        _EXTENSION_KIND: "extension",
    }.get(str(kind))
    if field is None:
        return None
    # Exact identity shape: exactly {type, <field>} and <field> == the ref id.
    if set(entry) != {"type", field} or entry.get(field) != ref:
        return None
    return str(kind)


def _classify_refs(
    profile: CommentedMap, registry: CommentedMap, minted: set[str]
) -> tuple[dict[str, list[str]], list[str]]:
    """Split a profile's ``packages`` refs into minted-by-kind + surviving.

    A ref whose top-level entry has the canonical minted shape
    (:func:`_minted_field`) is bucketed under its kind and recorded in
    ``minted``; every other ref (native / non-identity / dangling) is returned as
    surviving, preserving the original order within each partition.
    """
    by_kind: dict[str, list[str]] = {
        _CARGO_KIND: [],
        _PLUGIN_KIND: [],
        _EXTENSION_KIND: [],
    }
    surviving: list[str] = []
    for ref in _seq_of_str(profile.get("packages")):
        kind = _minted_field(registry.get(ref), ref)
        if kind is None:
            surviving.append(ref)
        else:
            by_kind[kind].append(ref)
            minted.add(ref)
    return by_kind, surviving


def _untranslate_profile(
    profile: CommentedMap, registry: CommentedMap, minted: set[str]
) -> None:
    """Rebuild one profile's legacy fields from its MINTED ``packages`` refs.

    Classifies each ``packages`` ref by looking up its top-level entry: a ref
    whose entry has the canonical minted shape (:func:`_minted_field`) is
    reconstructed into ``cargo_binaries`` / ``claude_plugins`` /
    ``extensions.include`` and recorded in ``minted`` (so the caller drops its
    now-orphaned top-level entry); EVERY other ref — native
    python/go/github_release/local, a native cargo/plugin/extension whose body
    isn't the identity form, or a dangling ref — is PRESERVED in place with its
    top-level entry intact. Also folds ``reconcile.plugins.policy`` ->
    ``plugins_reconcile`` and ``reconcile.extensions.{exclude, policy}`` ->
    ``extensions.{exclude, reconcile}``. The profile ``packages`` list is rebuilt
    from only the surviving refs (original order), dropped only when none survive.
    """
    by_kind, surviving = _classify_refs(profile, registry, minted)
    cargo = by_kind[_CARGO_KIND]
    plugins = by_kind[_PLUGIN_KIND]
    extensions_include = by_kind[_EXTENSION_KIND]

    if cargo:
        profile["cargo_binaries"] = _str_seq(cargo)
    if plugins:
        profile["claude_plugins"] = _str_seq(plugins)

    reconcile = profile.get("reconcile")
    ext_block = _rebuild_extensions(profile, extensions_include, reconcile)
    if ext_block is not None:
        profile["extensions"] = ext_block
    _rebuild_plugins_reconcile(profile, reconcile)

    if "reconcile" in profile:
        del profile["reconcile"]
    if surviving:
        profile["packages"] = _str_seq(surviving)
    elif "packages" in profile:
        del profile["packages"]


def _rebuild_extensions(
    profile: CommentedMap, include: list[str], reconcile: object
) -> CommentedMap | None:
    """Rebuild the profile's ``extensions`` block from include + reconcile.

    Reassembles ``include`` (extension-kind refs), ``exclude`` +
    ``reconcile`` (the policy) from ``reconcile.extensions``. Returns ``None``
    when there is nothing to rebuild (no include and no extension reconcile
    intent), so a profile that never carried extensions grows no empty block.
    """
    ext_reconcile = (
        reconcile.get("extensions") if isinstance(reconcile, CommentedMap) else None
    )
    exclude = (
        _seq_of_str(ext_reconcile.get("exclude"))
        if isinstance(ext_reconcile, CommentedMap)
        else []
    )
    policy = (
        ext_reconcile.get("policy") if isinstance(ext_reconcile, CommentedMap) else None
    )
    if not include and not exclude and policy is None:
        return None

    block = CommentedMap()
    if include:
        block["include"] = _str_seq(include)
    if exclude:
        block["exclude"] = _str_seq(exclude)
    if policy is not None:
        block["reconcile"] = policy
    return block


def _rebuild_plugins_reconcile(profile: CommentedMap, reconcile: object) -> None:
    """Fold ``reconcile.plugins.policy`` -> ``plugins_reconcile`` when present."""
    if not isinstance(reconcile, CommentedMap):
        return
    plugins = reconcile.get("plugins")
    if isinstance(plugins, CommentedMap) and plugins.get("policy") is not None:
        profile["plugins_reconcile"] = plugins["policy"]


def _drop_minted_packages(
    data: CommentedMap, registry: CommentedMap, minted: set[str]
) -> None:
    """Drop the minted cargo/plugin/extension top-level entries; prune empties.

    Only the entries the reverse consumed back into legacy fields are removed —
    a natively-authored ``packages`` entry (never referenced as a minted legacy
    fold) stays. An emptied ``packages`` registry key is removed entirely.
    """
    for name in minted:
        if name in registry:
            del registry[name]
    if not registry and "packages" in data:
        del data["packages"]


def _registry_min_version() -> str:
    """Return the lowest schema version the migration registry can resolve to.

    A lazy local import sidesteps the import cycle: this module is imported at
    the tail of :mod:`setforge.migrations`, but ``_lower_floor`` only runs from
    the reverse ``apply`` — long after the package finished initializing.
    """
    from setforge.migrations import known_versions

    return min(known_versions(), key=parse_schema_version)


def _lower_floor(data: CommentedMap, to_version: str) -> None:
    """Lower a stale ``minimum_version`` floor above ``to_version``.

    The forward contract GATES on ``minimum_version >= 6.0`` but never touched
    the floor when stamping, so the reverse leaves a config carrying
    ``schema_version: 5.0`` AND a >5.0 floor — which the very 5.x engine the
    downgrade serves refuses. The floor is an operator attestation, not user
    data, so the reverse lowers any floor above ``to_version`` to the registry's
    LOWEST schema version (which can never lock out a served target). Mirrors
    :func:`setforge.migrations._contract_2_0._lower_floor`.
    """
    raw_floor = data.get("minimum_version")
    if raw_floor is None:
        return
    floor = str(raw_floor)
    if parse_schema_version(floor) > parse_schema_version(to_version):
        data["minimum_version"] = _registry_min_version()
