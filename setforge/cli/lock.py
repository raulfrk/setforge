"""The ``lock`` subcommand — resolve a profile's packages → ``setforge.lock``.

``setforge lock --profile=X`` enumerates every LOCKABLE package the resolved
profile X declares, resolves each to a concrete version + integrity via the
resolver registry, and writes the pins into the committed ``setforge.lock``.
``local`` packages are NOT lockable (nothing resolves upstream) and are skipped.

The write is FAIL-CLOSED (spec §C): all pins are resolved FIRST; if ANY package
raises, the whole write aborts and no partial lock ever lands. It is also a
per-profile MERGE — an existing lock's other-profile entries are preserved, this
profile is added to a shared ``(type, key)``'s back-ref, and a shared package
that resolves to a DIFFERENT version across profiles is a hard
:class:`~setforge.errors.LockConflict` naming both versions.

``lock --update <key>`` re-resolves ONLY the named package (by lock key) and
rewrites just its entry, preserving the rest of the lock verbatim.

The resolvers hit the network; the resolution boundary is the registry
(:func:`~setforge.provision.resolve.registry.get_resolver`), so unit tests
register stub resolvers rather than touching the wire. Every resolver module is
imported for its self-registration side effect (see ``_resolvers`` below).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import typer

# Import each resolver module for the @register side effect so get_resolver can
# find them. Mirrors provision/dispatch.py's provisioner-import block.
import setforge.provision.resolve.cargo as _cargo_resolver  # noqa: F401
import setforge.provision.resolve.extension as _extension_resolver  # noqa: F401
import setforge.provision.resolve.github_release as _ghr_resolver  # noqa: F401
import setforge.provision.resolve.go as _go_resolver  # noqa: F401
import setforge.provision.resolve.plugin as _plugin_resolver  # noqa: F401
import setforge.provision.resolve.python as _python_resolver  # noqa: F401
from setforge.cli import _CONFIG_OPTION, _resolve_config_arg, app
from setforge.cli._help_examples import LOCK_EXAMPLES
from setforge.config import (
    Config,
    LocalPackage,
    ResolvedProfile,
    load_config,
    resolve_and_expand,
)
from setforge.errors import ConfigError, LockConflict, ResolveError
from setforge.lockfile import LockFile, lock_path, parse_lock, write_lock
from setforge.provision.resolve.extension import ExtensionResolveItem
from setforge.provision.resolve.plugin import PluginResolveItem, marketplace_git_url
from setforge.provision.resolve.protocol import PackageType, ResolvedPin
from setforge.provision.resolve.registry import get_resolver


@dataclass(frozen=True, slots=True)
class _LockItem:
    """One lockable unit: the resolver key + the already-built resolver input.

    ``pkg_type`` selects the resolver via :func:`get_resolver`; ``resolve_input``
    is exactly what that resolver's ``resolve`` expects (a Package model, a
    :class:`PluginResolveItem`, or an :class:`ExtensionResolveItem`).
    """

    pkg_type: PackageType
    resolve_input: object


def enumerate_lock_items(cfg: Config, resolved: ResolvedProfile) -> list[_LockItem]:
    """Return every LOCKABLE item the resolved profile declares.

    Three surfaces (bundles are intentionally out of scope for this verb):

    - ``resolved.packages`` — name refs into ``cfg.packages`` (typed Package
      models). ``local`` packages resolve nothing upstream, so they are SKIPPED
      (spec §B3: no lock entry). Each other Package model is passed to its
      resolver verbatim.
    - ``resolved.claude_plugins`` — bare plugin names resolved through
      ``cfg.claude_plugins`` → ``cfg.marketplaces`` into a
      :class:`PluginResolveItem` (lock key ``name@marketplace``, git URL derived
      once via :func:`marketplace_git_url`).
    - ``resolved.extensions.include`` — ``publisher.name`` ids wrapped in an
      :class:`ExtensionResolveItem`.

    A plugin naming an undeclared plugin / marketplace raises
    :class:`~setforge.errors.ConfigError` — a lockable package with no way to
    resolve is an error, never a silent skip (spec §C).
    """
    items: list[_LockItem] = []

    for ref in resolved.packages:
        pkg = cfg.packages[ref]
        if isinstance(pkg, LocalPackage):
            continue  # not lockable — nothing upstream to pin
        items.append(_LockItem(PackageType(pkg.type.value), pkg))

    for bare_name in resolved.claude_plugins:
        items.append(_LockItem(PackageType.PLUGIN, _plugin_item(cfg, bare_name)))

    for ext_id in resolved.extensions.include:
        items.append(_LockItem(PackageType.EXTENSION, ExtensionResolveItem(key=ext_id)))

    return items


def _plugin_item(cfg: Config, bare_name: str) -> PluginResolveItem:
    """Build the :class:`PluginResolveItem` for one bare profile plugin name.

    Resolves ``bare_name`` → its marketplace via ``cfg.claude_plugins``, then the
    marketplace's git URL via ``cfg.marketplaces`` + :func:`marketplace_git_url`.
    The lock key is ``name@marketplace`` (matching the plugin id shape used
    elsewhere). An undeclared plugin or an unknown marketplace is a
    :class:`~setforge.errors.ConfigError` (load_config already cross-checks these,
    but re-guarding keeps this helper total).
    """
    ref = cfg.claude_plugins.get(bare_name)
    if ref is None:
        raise ConfigError(
            f"profile references undeclared plugin: {bare_name!r} "
            f"(add it to top-level claude_plugins:)"
        )
    mp_source = cfg.marketplaces.get(ref.marketplace)
    if mp_source is None:
        raise ConfigError(
            f"plugin {bare_name!r} references unknown marketplace "
            f"{ref.marketplace!r} (add it to top-level marketplaces:)"
        )
    return PluginResolveItem(
        key=f"{bare_name}@{ref.marketplace}",
        git_url=marketplace_git_url(mp_source),
    )


def resolve_pins(items: list[_LockItem], profile: str) -> list[ResolvedPin]:
    """Resolve every item to a pin, FAIL-CLOSED, tagging each with ``profile``.

    Resolves ALL items first (spec §C): a single resolver failure aborts the
    whole batch by propagating the :class:`~setforge.errors.ResolveError`, so the
    caller never writes a partial lock. Each returned pin carries this run's
    ``profile`` as its sole back-ref entry; the merge step unions it with any
    other-profile back-refs already in the lock.
    """
    pins: list[ResolvedPin] = []
    for item in items:
        resolver = get_resolver(item.pkg_type)
        pin = resolver.resolve(item.resolve_input)
        pins.append(pin.model_copy(update={"profiles": (profile,)}))
    return pins


def merge_lock(existing: LockFile | None, new_pins: list[ResolvedPin]) -> LockFile:
    """Merge freshly-resolved ``new_pins`` into ``existing`` (or a fresh lock).

    Keyed on ``(type, key)``. A pin present only in one side survives as-is; a
    ``(type, key)`` present in both must resolve to the SAME version — the merged
    pin unions the two ``profiles`` back-refs. A version mismatch is a hard
    :class:`~setforge.errors.LockConflict` naming both versions (spec §C: never
    silently drop one profile's pin). ``dump_lock`` re-imposes deterministic
    ordering, so the merged pin insertion order does not affect the written
    bytes.
    """
    merged: dict[tuple[str, str], ResolvedPin] = {}
    if existing is not None:
        for pin in existing.packages:
            merged[pin.sort_key()] = pin

    for pin in new_pins:
        key = pin.sort_key()
        prior = merged.get(key)
        if prior is None:
            merged[key] = pin
            continue
        if prior.version != pin.version:
            raise LockConflict(
                f"package {pin.key!r} (type {pin.type.value!r}) resolves to "
                f"{pin.version!r} for profile(s) {sorted(pin.profiles)} but the "
                f"lock already pins {prior.version!r} for profile(s) "
                f"{sorted(prior.profiles)}; a shared package must resolve to one "
                f"version across profiles"
            )
        merged[key] = prior.model_copy(
            update={"profiles": _union_profiles(prior.profiles, pin.profiles)}
        )

    return LockFile(packages=tuple(merged.values()))


def _union_profiles(a: tuple[str, ...], b: tuple[str, ...]) -> tuple[str, ...]:
    """Return the sorted-unique union of two ``profiles`` back-ref tuples."""
    return tuple(sorted(set(a) | set(b)))


def apply_update(existing: LockFile, updated: ResolvedPin) -> LockFile:
    """Rewrite ONLY ``updated``'s ``(type, key)`` entry, preserving the rest.

    Preserves the existing entry's ``profiles`` back-ref (``--update`` re-resolves
    a package's version/integrity, not which profiles declare it). A key absent
    from the lock keeps the freshly-resolved pin's back-ref as-is.
    """
    key = updated.sort_key()
    packages: list[ResolvedPin] = []
    replaced = False
    for pin in existing.packages:
        if pin.sort_key() == key:
            packages.append(updated.model_copy(update={"profiles": pin.profiles}))
            replaced = True
        else:
            packages.append(pin)
    if not replaced:
        packages.append(updated)
    return LockFile(packages=tuple(packages))


def _load_existing_lock(path: Path) -> LockFile | None:
    """Return the parsed lock at ``path``, or ``None`` when it does not exist.

    A malformed on-disk lock propagates
    :class:`~setforge.errors.MalformedLockError` (caught at the CLI boundary),
    never a silent overwrite.
    """
    if not path.exists():
        return None
    return parse_lock(path.read_text(encoding="utf-8"))


@app.command("lock", epilog=LOCK_EXAMPLES)
def lock(
    profile: str = typer.Option(..., "--profile", "-p", help="Profile to lock."),
    update: str | None = typer.Option(
        None,
        "--update",
        help="Re-resolve ONLY this package (by its lock key), preserving the "
        "rest of the lock.",
    ),
    config: Path = _CONFIG_OPTION,
) -> None:
    """Resolve a profile's lockable packages and write ``setforge.lock``.

    Enumerates the resolved profile's cargo / python / go / github_release
    packages plus its plugins and extensions (``local`` is skipped), resolves
    each to a concrete version + integrity, and merges the pins into the
    committed ``setforge.lock`` beside ``setforge.yaml``. Fail-closed: any
    resolution failure aborts before the single atomic write.
    """
    config = _resolve_config_arg(config)
    cfg = load_config(config)
    repo_root = config.resolve().parent
    resolved = resolve_and_expand(cfg, profile, repo_root)

    path = lock_path(config)
    existing = _load_existing_lock(path)
    items = enumerate_lock_items(cfg, resolved)

    if update is not None:
        _run_update(items, existing, update, profile, path)
        return

    typer.echo(f"resolving {len(items)} package(s) for profile {profile!r}…")
    new_pins = resolve_pins(items, profile)
    lockfile = merge_lock(existing, new_pins)
    write_lock(lockfile, path)
    typer.echo(f"wrote {path.name} ({len(lockfile.packages)} pin(s))")


def _run_update(
    items: list[_LockItem],
    existing: LockFile | None,
    update_key: str,
    profile: str,
    path: Path,
) -> None:
    """Handle ``lock --update <key>``: re-resolve one package, rewrite its entry.

    ``update_key`` is matched against each item's RESOLVED pin key (the same key
    the lock stores). No existing lock, or a key that matches no declared
    package, is a clean :class:`~setforge.errors.ResolveError` (exit 1) rather
    than a silent no-op.
    """
    if existing is None:
        raise ResolveError(
            f"cannot --update {update_key!r}: no {path.name} exists yet; run "
            f"'setforge lock --profile={profile}' first"
        )
    for item in items:
        resolver = get_resolver(item.pkg_type)
        pin = resolver.resolve(item.resolve_input)
        if pin.key == update_key:
            updated = pin.model_copy(update={"profiles": (profile,)})
            lockfile = apply_update(existing, updated)
            write_lock(lockfile, path)
            typer.echo(f"updated {update_key!r} in {path.name}")
            return
    raise ResolveError(
        f"cannot --update {update_key!r}: no package with that lock key is "
        f"declared in profile {profile!r}"
    )
