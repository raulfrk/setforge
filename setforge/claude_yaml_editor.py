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

import contextlib
import os
import stat
import tempfile
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import (
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


def _atomic_yaml_dump(yaml: YAML, doc: CommentedMap, config_path: Path) -> None:
    """Dump ``doc`` to ``config_path`` atomically (temp file + ``os.replace``).

    ``open("w")`` truncates in place — a crash mid-dump corrupts the
    config. Writing to a sibling temp file and renaming makes the swap
    atomic: a SIGTERM leaves the original intact. Mirrors
    :func:`setforge.section_reconcile._atomic_write_text`.
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
    fd, tmp_name = tempfile.mkstemp(
        dir=str(config_path.parent), prefix=f".{config_path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml.dump(doc, fh)
            fh.flush()
            os.fchmod(fh.fileno(), original_mode)
        os.replace(tmp_path, config_path)
    finally:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)


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
    _atomic_yaml_dump(yaml, doc, config_path)
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
        # Drop the now-empty block so removal restores the document to its
        # pre-add shape (``yaml_add_marketplace`` re-materializes it on
        # demand). A leftover ``marketplaces: {}`` breaks byte-parity for
        # add-then-rollback flows.
        if not mps:
            del doc["marketplaces"]
    _atomic_yaml_dump(yaml, doc, config_path)
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
    _atomic_yaml_dump(yaml, doc, config_path)
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
    _atomic_yaml_dump(yaml, doc, config_path)
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
    _atomic_yaml_dump(yaml, doc, config_path)
    return True
