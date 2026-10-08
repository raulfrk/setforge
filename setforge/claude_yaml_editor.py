"""Ruamel.yaml editing helpers for ``setforge.yaml`` plugin blocks.

Targets the top-level ``claude_plugins:`` / ``marketplaces:`` / ``packages:``
blocks and each profile's ``packages:`` ref-list in ``setforge.yaml``.
Provides verb-shaped functions (``yaml_add_marketplace``,
``yaml_remove_marketplace``, ``yaml_add_plugin``,
``yaml_add_plugin_to_profile``, ``yaml_remove_plugin_from_profile``) that
read, mutate, and write back the setforge config YAML. A profile declares a
plugin through a ``packages`` ref to a minted top-level ``PluginPackage``.
Round-trip preserves comments and key ordering via ruamel.yaml's ``rt`` mode.
"""

from __future__ import annotations

import stat
from pathlib import Path

from ruamel.yaml.comments import (
    CommentedMap,
    CommentedSeq,
)

from setforge.atomicio import atomic_write_text
from setforge.config import (
    Config,
    MarketplaceSource,
    MarketplaceSourceKind,
    PluginPackage,
    load_config,
    validate_registry_name,
)
from setforge.errors import ConfigError, ProfileNotFound
from setforge.migrations._yaml_ops import render_yaml, yaml_rt

__all__ = [
    "require_plugin_package_available",
    "yaml_add_codex_marketplace",
    "yaml_add_codex_plugin",
    "yaml_add_codex_plugin_to_profile",
    "yaml_add_marketplace",
    "yaml_add_plugin",
    "yaml_add_plugin_to_profile",
    "yaml_remove_codex_marketplace",
    "yaml_remove_codex_plugin_from_profile",
    "yaml_remove_marketplace",
    "yaml_remove_plugin_from_profile",
]


def _codex_block(doc: CommentedMap) -> CommentedMap:
    return _ensure_top_level_block(doc, "codex")


def _require_codex_contract(cfg: Config, config_path: Path) -> None:
    def version(value: str | None) -> tuple[int, int]:
        try:
            major, minor = (value or "").split(".", 1)
            return int(major), int(minor)
        except ValueError:
            return 0, 0

    if version(cfg.schema_version) < (6, 4) or version(cfg.minimum_version) < (6, 4):
        raise ConfigError(
            f"{config_path}: Codex declarations require schema_version and "
            "minimum_version >= '6.4'; run `setforge migrate` first"
        )


def _validate_new_registry_name(name: str, *, label: str) -> None:
    try:
        validate_registry_name(name, label=label)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def yaml_add_codex_marketplace(
    config_path: Path, name: str, source: MarketplaceSource
) -> bool:
    _validate_new_registry_name(name, label="Marketplace")
    cfg = load_config(config_path)
    _require_codex_contract(cfg, config_path)
    if cfg.codex is not None and name in cfg.codex.marketplaces:
        return False
    doc = _load_yaml_doc(config_path)
    marketplaces = _ensure_top_level_block(_codex_block(doc), "marketplaces")
    marketplaces[name] = source.model_dump(mode="json", exclude_none=True)
    _atomic_yaml_dump(doc, config_path)
    return True


def yaml_remove_codex_marketplace(config_path: Path, name: str) -> bool:
    doc = _load_yaml_doc(config_path)
    codex = doc.get("codex")
    marketplaces = (
        codex.get("marketplaces") if isinstance(codex, CommentedMap) else None
    )
    if not isinstance(marketplaces, CommentedMap) or name not in marketplaces:
        return False
    del marketplaces[name]
    _atomic_yaml_dump(doc, config_path)
    return True


def yaml_add_codex_plugin(config_path: Path, name: str, marketplace: str) -> bool:
    _validate_new_registry_name(name, label="Plugin")
    cfg = load_config(config_path)
    _require_codex_contract(cfg, config_path)
    if cfg.codex is not None and name in cfg.codex.plugins:
        return False
    doc = _load_yaml_doc(config_path)
    plugins = _ensure_top_level_block(_codex_block(doc), "plugins")
    plugins[name] = CommentedMap({"marketplace": marketplace})
    _atomic_yaml_dump(doc, config_path)
    return True


def yaml_add_codex_plugin_to_profile(
    config_path: Path, profile: str, name: str
) -> bool:
    cfg = load_config(config_path)
    _require_codex_contract(cfg, config_path)
    if profile not in cfg.profiles:
        raise ProfileNotFound(profile)
    doc = _load_yaml_doc(config_path)
    profiles = doc["profiles"]
    profile_block = profiles[profile]
    codex = _ensure_top_level_block(profile_block, "codex")
    plugins = _ensure_list(codex, "plugins")
    if name in plugins:
        return False
    plugins.append(name)
    _atomic_yaml_dump(doc, config_path)
    return True


def yaml_remove_codex_plugin_from_profile(
    config_path: Path, profile: str, name: str
) -> bool:
    cfg = load_config(config_path)
    if profile not in cfg.profiles:
        raise ProfileNotFound(profile)
    doc = _load_yaml_doc(config_path)
    profile_block = doc["profiles"][profile]
    codex = profile_block.get("codex")
    plugins = codex.get("plugins") if isinstance(codex, CommentedMap) else None
    if not isinstance(plugins, CommentedSeq) or name not in plugins:
        return False
    plugins.remove(name)
    _atomic_yaml_dump(doc, config_path)
    return True


def _load_yaml_doc(config_path: Path) -> CommentedMap:
    """Load ``config_path`` in ruamel.yaml round-trip mode.

    Raises :class:`ConfigError` when the file does not exist.
    """
    if not config_path.exists():
        raise ConfigError(f"config file not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as fh:
        return yaml_rt().load(fh)


def _atomic_yaml_dump(doc: CommentedMap, config_path: Path) -> None:
    """Dump ``doc`` to ``config_path`` atomically (temp file + ``os.replace``).

    ``open("w")`` truncates in place — a crash mid-dump corrupts the
    config. Writing to a sibling temp file and renaming makes the swap
    atomic: a SIGTERM leaves the original intact. Mirrors
    :func:`setforge.deploy._atomic_write`.
    """
    # Resolve symlinks first: os.replace swaps the link itself for a
    # regular file, whereas the prior open("w") wrote THROUGH the link.
    # Resolving to the real target preserves that "replace target, never
    # the link" semantics, matching deploy._atomic_write's real_dst.
    config_path = config_path.resolve()
    # os.replace swaps the inode, so the new file would otherwise inherit
    # mkstemp's 0o600 and silently drop the config's group/other access.
    # Carry the existing perm bits over (config_path is guaranteed to
    # exist — every caller loads it first). fchmod on the temp fd before
    # replace closes the TOCTOU window, matching deploy._atomic_write.
    original_mode = stat.S_IMODE(config_path.stat().st_mode)
    text = render_yaml(
        doc, config_path.read_bytes().decode("utf-8"), fallback=(2, 4, 2)
    )
    atomic_write_text(config_path, text, mode=original_mode)


def _ensure_top_level_block(doc: CommentedMap, key: str) -> CommentedMap:
    """Return ``doc[key]``, creating an empty mapping if absent."""
    if key not in doc:
        doc[key] = CommentedMap()
    return doc[key]


def _ensure_list(block: CommentedMap, key: str) -> CommentedSeq:
    """Return ``block[key]`` as a sequence, creating it if absent."""
    if key not in block:
        block[key] = CommentedSeq()
    return block[key]


def yaml_add_marketplace(
    config_path: Path,
    name: str,
    source: MarketplaceSource,
) -> bool:
    """Append a marketplace entry to the top-level ``marketplaces:`` block.

    Idempotent: returns ``False`` if ``name`` is already present.
    Comments and key order in the YAML document are preserved via
    ruamel.yaml round-trip mode.
    """
    _validate_new_registry_name(name, label="Marketplace")
    cfg = load_config(config_path)
    if name in cfg.marketplaces:
        return False

    doc = _load_yaml_doc(config_path)
    mps = _ensure_top_level_block(doc, "marketplaces")
    entry = CommentedMap()
    entry["source"] = source.source.value
    if source.source is MarketplaceSourceKind.GITHUB:
        entry["repo"] = source.repo or ""
    else:
        entry["path"] = str(source.path or "")
    mps[name] = entry
    _atomic_yaml_dump(doc, config_path)
    return True


def yaml_remove_marketplace(config_path: Path, name: str) -> bool:
    """Remove a marketplace from the top-level ``marketplaces:`` block.

    Idempotent: returns ``False`` if ``name`` is not present.
    """
    cfg = load_config(config_path)
    if name not in cfg.marketplaces:
        return False

    doc = _load_yaml_doc(config_path)
    mps = doc.get("marketplaces")
    if mps and name in mps:
        del mps[name]
        # Drop the now-empty block so removal restores the document to its
        # pre-add shape (``yaml_add_marketplace`` re-materializes it on
        # demand). A leftover ``marketplaces: {}`` breaks byte-parity for
        # add-then-rollback flows.
        if not mps:
            del doc["marketplaces"]
    _atomic_yaml_dump(doc, config_path)
    return True


def yaml_add_plugin(
    config_path: Path,
    plugin_name: str,
    marketplace: str,
) -> bool:
    """Declare a plugin in the top-level ``claude_plugins:`` block.

    Idempotent: returns ``False`` if ``plugin_name`` is already present.
    Does NOT add it to any profile's ``claude_plugins:`` list — the CLI
    caller is responsible for that via :func:`yaml_add_plugin_to_profile`.
    """
    _validate_new_registry_name(plugin_name, label="Plugin")
    cfg = load_config(config_path)
    if plugin_name in cfg.claude_plugins:
        return False

    doc = _load_yaml_doc(config_path)
    plugins_block = _ensure_top_level_block(doc, "claude_plugins")
    entry = CommentedMap()
    entry["marketplace"] = marketplace
    plugins_block[plugin_name] = entry
    _atomic_yaml_dump(doc, config_path)
    return True


def _profile_plugin_refs(cfg: Config, profile_name: str, plugin_ref: str) -> list[str]:
    """Return the profile's ``packages`` refs that resolve to plugin ``plugin_ref``.

    A profile declares a plugin through a ``packages`` ref whose top-level
    entry is a :class:`PluginPackage` for that bare name. Returns every such
    ref (usually zero or one) so callers can test membership and prune.
    """
    profile = cfg.profiles[profile_name]
    out: list[str] = []
    for ref in profile.packages:
        pkg = cfg.packages.get(ref)
        if isinstance(pkg, PluginPackage) and pkg.plugin == plugin_ref:
            out.append(ref)
    return out


def require_plugin_package_available(
    cfg: Config, profile_name: str, plugin_ref: str
) -> None:
    """Raise :class:`ConfigError` if adding ``plugin_ref`` would reuse a package key.

    :func:`yaml_add_plugin_to_profile` binds the plugin through a top-level
    ``packages`` entry keyed by the bare plugin name. A key already held by a
    different package would be bound as if it were the plugin, silently
    pointing the profile at the wrong package. A profile that already binds the
    plugin, or a key holding a plugin package for this very plugin, is fine.
    """
    if _profile_plugin_refs(cfg, profile_name, plugin_ref):
        return
    existing = cfg.packages.get(plugin_ref)
    if existing is None:
        return
    if isinstance(existing, PluginPackage):
        if existing.plugin == plugin_ref:
            return
        detail = f"declares plugin {existing.plugin!r}"
    else:
        detail = f"type {existing.type.value}"
    raise ConfigError(
        f"package {plugin_ref!r} already exists ({detail}), so plugin "
        f"{plugin_ref!r} cannot be added to profile {profile_name!r} under that "
        "name; rename or remove that package first"
    )


def yaml_add_plugin_to_profile(
    config_path: Path,
    profile_name: str,
    plugin_ref: str,
) -> bool:
    """Declare ``plugin_ref`` on ``profiles.<profile>`` via the packages surface.

    Mints a top-level ``packages`` entry (``type: plugin``) when absent and
    appends the ref to the profile's ``packages`` list. Idempotent: returns
    ``False`` when the profile already references the plugin. Raises
    :class:`ProfileNotFound` when the profile does not exist.
    """
    cfg = load_config(config_path)
    if profile_name not in cfg.profiles:
        raise ProfileNotFound(f"profile not found: {profile_name}")
    if _profile_plugin_refs(cfg, profile_name, plugin_ref):
        return False

    doc = _load_yaml_doc(config_path)
    profiles = doc.get("profiles", {})
    if profile_name not in profiles:
        raise ProfileNotFound(f"profile not found: {profile_name}")
    packages_block = _ensure_top_level_block(doc, "packages")
    if plugin_ref not in packages_block:
        entry = CommentedMap()
        entry["type"] = "plugin"
        entry["plugin"] = plugin_ref
        packages_block[plugin_ref] = entry
    pkg_list = _ensure_list(profiles[profile_name], "packages")
    if plugin_ref not in pkg_list:
        pkg_list.append(plugin_ref)
    _atomic_yaml_dump(doc, config_path)
    return True


def yaml_remove_plugin_from_profile(
    config_path: Path,
    profile_name: str,
    plugin_ref: str,
) -> bool:
    """Drop ``plugin_ref``'s packages ref from ``profiles.<profile>``.

    Removes every profile ``packages`` ref whose top-level entry is a
    :class:`PluginPackage` for ``plugin_ref``. Idempotent: returns ``False``
    when the profile does not reference the plugin. Raises
    :class:`ProfileNotFound` when the profile does not exist. Leaves the
    top-level ``packages`` entry in place — it may be shared by other profiles.
    """
    cfg = load_config(config_path)
    if profile_name not in cfg.profiles:
        raise ProfileNotFound(f"profile not found: {profile_name}")
    refs = _profile_plugin_refs(cfg, profile_name, plugin_ref)
    if not refs:
        return False

    doc = _load_yaml_doc(config_path)
    profiles = doc.get("profiles", {})
    if profile_name not in profiles:
        return False
    pkg_list = profiles[profile_name].get("packages", [])
    for ref in refs:
        if ref in pkg_list:
            pkg_list.remove(ref)
    _atomic_yaml_dump(doc, config_path)
    return True
