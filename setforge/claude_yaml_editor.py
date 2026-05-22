"""Ruamel.yaml editing helpers for ``setforge.yaml`` plugin blocks.

Targets the ``claude_plugins:`` and ``marketplaces:`` blocks in
``setforge.yaml``. Provides verb-shaped functions
(``yaml_add_marketplace``, ``yaml_remove_marketplace``,
``yaml_add_plugin``, ``yaml_add_plugin_to_profile``,
``yaml_remove_plugin_from_profile``) that read, mutate, and write
back the setforge config YAML. Round-trip preserves comments and
key ordering via ruamel.yaml's ``rt`` mode.
"""

from __future__ import annotations

from pathlib import Path

# ruamel.yaml ships py.typed without resolvable annotations; no stub pkg on PyPI.
from ruamel.yaml import YAML  # type: ignore[import-not-found]
from ruamel.yaml.comments import (  # type: ignore[import-not-found]
    CommentedMap,
    CommentedSeq,
)

from setforge.config import MarketplaceSource, MarketplaceSourceKind, load_config
from setforge.errors import ConfigError, ProfileNotFound

__all__ = [
    "yaml_add_marketplace",
    "yaml_add_plugin",
    "yaml_add_plugin_to_profile",
    "yaml_remove_marketplace",
    "yaml_remove_plugin_from_profile",
]


def _load_yaml_doc(config_path: Path) -> tuple[YAML, CommentedMap]:
    """Load ``config_path`` in ruamel.yaml round-trip mode.

    Returns ``(yaml_instance, doc)`` so the caller can modify ``doc``
    and write it back via ``yaml_instance.dump(doc, fh)``.
    Raises :class:`ConfigError` when the file does not exist.
    """
    if not config_path.exists():
        raise ConfigError(f"config file not found: {config_path}")
    yaml = YAML(typ="rt")
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.preserve_quotes = True
    with config_path.open("r", encoding="utf-8") as fh:
        return yaml, yaml.load(fh)


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
    cfg = load_config(config_path)
    if name in cfg.marketplaces:
        return False

    yaml, doc = _load_yaml_doc(config_path)
    mps = _ensure_top_level_block(doc, "marketplaces")
    entry = CommentedMap()
    entry["source"] = source.source.value
    if source.source is MarketplaceSourceKind.GITHUB:
        entry["repo"] = source.repo or ""
    else:
        entry["path"] = str(source.path or "")
    mps[name] = entry
    with config_path.open("w", encoding="utf-8") as fh:
        yaml.dump(doc, fh)
    return True


def yaml_remove_marketplace(config_path: Path, name: str) -> bool:
    """Remove a marketplace from the top-level ``marketplaces:`` block.

    Idempotent: returns ``False`` if ``name`` is not present.
    """
    cfg = load_config(config_path)
    if name not in cfg.marketplaces:
        return False

    yaml, doc = _load_yaml_doc(config_path)
    mps = doc.get("marketplaces")
    if mps and name in mps:
        del mps[name]
    with config_path.open("w", encoding="utf-8") as fh:
        yaml.dump(doc, fh)
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
    cfg = load_config(config_path)
    if plugin_name in cfg.claude_plugins:
        return False

    yaml, doc = _load_yaml_doc(config_path)
    plugins_block = _ensure_top_level_block(doc, "claude_plugins")
    entry = CommentedMap()
    entry["marketplace"] = marketplace
    plugins_block[plugin_name] = entry
    with config_path.open("w", encoding="utf-8") as fh:
        yaml.dump(doc, fh)
    return True


def yaml_add_plugin_to_profile(
    config_path: Path,
    profile_name: str,
    plugin_ref: str,
) -> bool:
    """Append ``plugin_ref`` to ``profiles.<profile>.claude_plugins``.

    Idempotent: returns ``False`` if already present.
    Raises :class:`ProfileNotFound` when the profile does not exist.
    """
    cfg = load_config(config_path)
    if profile_name not in cfg.profiles:
        raise ProfileNotFound(f"profile not found: {profile_name}")
    if plugin_ref in cfg.profiles[profile_name].claude_plugins:
        return False

    yaml, doc = _load_yaml_doc(config_path)
    profiles = doc.get("profiles", {})
    if profile_name not in profiles:
        raise ProfileNotFound(f"profile not found: {profile_name}")
    profile_block = profiles[profile_name]
    cp_list = _ensure_list(profile_block, "claude_plugins")
    cp_list.append(plugin_ref)
    with config_path.open("w", encoding="utf-8") as fh:
        yaml.dump(doc, fh)
    return True


def yaml_remove_plugin_from_profile(
    config_path: Path,
    profile_name: str,
    plugin_ref: str,
) -> bool:
    """Remove ``plugin_ref`` from ``profiles.<profile>.claude_plugins``.

    Idempotent: returns ``False`` if not present.
    Raises :class:`ProfileNotFound` when the profile does not exist.
    """
    cfg = load_config(config_path)
    if profile_name not in cfg.profiles:
        raise ProfileNotFound(f"profile not found: {profile_name}")
    if plugin_ref not in cfg.profiles[profile_name].claude_plugins:
        return False

    yaml, doc = _load_yaml_doc(config_path)
    profiles = doc.get("profiles", {})
    if profile_name not in profiles:
        return False
    profile_block = profiles[profile_name]
    cp_list = profile_block.get("claude_plugins", [])
    if plugin_ref in cp_list:
        cp_list.remove(plugin_ref)
    with config_path.open("w", encoding="utf-8") as fh:
        yaml.dump(doc, fh)
    return True
