"""Per-host plugin / extension / marketplace overlay resolvers (setforge-5z11).

Resolves a profile's effective plugin / extension / marketplace lists
from the profile chain and the optional ``local.yaml`` overlays
(``profile UNION local.add MINUS local.remove``), tagging each entry
with its origin so compare/install output can display provenance per
SPEC 2's mockup (``[from local.yaml]`` for adds; a U+2212-prefixed
``removed via local.yaml`` tag for removes — see :func:`display_tag`
for the verbatim wording, kept in exactly one place).

Three resolvers, each raising :class:`LocalOverlayError` on
``add ∩ remove`` collision OR remove-of-not-in-profile-resolved:

- :func:`resolve_plugin_overlay`
- :func:`resolve_extension_overlay`
- :func:`resolve_marketplace_overlay`

The resolvers are loader-driven — see
:func:`setforge.config.apply_local_overlay`. This module is
intentionally free of import-time references to :mod:`setforge.source`
or :mod:`setforge.config`, so the loader can lazy-import the overlay
models without circularity (mirrors :mod:`setforge.preserved_keys`
discipline).

The :func:`display_tag` function is the single source of truth for tag
wording — callers MUST never construct ``[from local.yaml]`` or the
SPEC-2 remove-tag strings via f-string anywhere in install / compare
/ validate output paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, assert_never

from setforge.errors import ConfigError

if TYPE_CHECKING:
    from setforge.source import (
        ExtensionOverlay,
        MarketplaceOverlay,
        PluginOverlay,
    )


class OverlayOrigin(StrEnum):
    """Provenance of one entry in a resolved plugin/extension/marketplace list.

    Wire shape for compare/install output formatters — display tags
    (e.g. ``[from local.yaml]``) are derived from these members via
    :func:`display_tag`, never parsed from strings. The three members
    exhaust SPEC 2's display surface.
    """

    PROFILE = "profile"
    LOCAL_ADD = "local_add"
    LOCAL_REMOVE = "local_remove"


@dataclass(frozen=True, slots=True)
class ResolvedPlugin:
    """One entry in a profile's resolved plugin list with provenance.

    ``value`` is the bare plugin reference (e.g. ``"superpowers"`` or
    ``"secure-code-review@work-internal"``). ``origin`` carries the
    provenance per :class:`OverlayOrigin`.
    """

    value: str
    origin: OverlayOrigin


@dataclass(frozen=True, slots=True)
class ResolvedExtension:
    """One entry in a profile's resolved extension list with provenance."""

    value: str
    origin: OverlayOrigin


@dataclass(frozen=True, slots=True)
class ResolvedMarketplace:
    """One entry in a profile's resolved marketplace list with provenance.

    ``value`` is the marketplace name (the key in
    :attr:`setforge.config.Config.marketplaces`). ``origin`` carries the
    provenance. Marketplace source details (``repo`` / ``path``) live in
    the source mappings (``cfg.marketplaces`` / overlay
    ``_MarketplaceLocalDecl``) — the resolver only tracks origin per
    name.
    """

    value: str
    origin: OverlayOrigin


class LocalOverlayError(ConfigError):
    """Raised when a ``local.yaml`` overlay is self-contradictory or
    references an entry absent from the profile-resolved set.

    Subclasses :class:`ConfigError` so the existing
    :func:`setforge.cli.validate` config-error handling path catches it
    uniformly. The message carries the canonical phrases compare /
    install / validate output assertions key on:

    - ``"in both add and remove"`` for the add ∩ remove collision case.
    - ``"not in profile-resolved set"`` for the unknown-remove case.
    """


class LocalOverlayLoadError(ConfigError):
    """Raised when ``local.yaml`` cannot be loaded or parsed for overlays.

    Sentinel subclass of :class:`ConfigError` used by
    :func:`setforge.config._load_overlay_blocks` to distinguish
    load-phase failures (YAML parse error, non-mapping top level,
    Pydantic shape error in an overlay block) from cross-ref-phase
    failures (which still raise bare :class:`ConfigError`). The
    validate CLI's overlay-check wrapper uses this distinction to
    decide whether the marketplace cross-ref ran:

    - :class:`LocalOverlayLoadError` → load failed BEFORE the
      cross-ref check; the standalone Check 6 must still run as a
      fallback to surface pre-existing marketplace inconsistencies.
    - bare :class:`ConfigError` from
      :func:`setforge.config.apply_local_overlay` → cross-ref ran
      and reported; skip Check 6 to avoid duplicate failure rows.
    """


def _check_overlay_lists(
    *,
    overlay_kind: str,
    profile_name: str,
    profile_values: list[str],
    overlay_add: list[str],
    overlay_remove: list[str],
) -> None:
    """Run the add ∩ remove + unknown-remove checks for any overlay kind.

    Shared body for the three resolvers. ``overlay_kind`` is the noun
    used in error messages (``"plugin"`` / ``"extension"`` /
    ``"marketplace"``). Raises :class:`LocalOverlayError` on either
    failure; returns ``None`` when both checks pass.
    """
    collisions = sorted(set(overlay_add) & set(overlay_remove))
    if collisions:
        joined = ", ".join(repr(k) for k in collisions)
        raise LocalOverlayError(
            f"{overlay_kind} overlay: entry(ies) appear in both add and remove "
            f"of local.yaml: {joined}. Drop one of the two list entries."
        )
    profile_set = set(profile_values)
    unknown_removes = sorted(set(overlay_remove) - profile_set)
    if unknown_removes:
        joined = ", ".join(repr(k) for k in unknown_removes)
        raise LocalOverlayError(
            f"{overlay_kind} overlay: remove entry(ies) not in profile-resolved "
            f"set {profile_name!r}: {joined}. Remove the entry from local.yaml "
            f"or add the value to the profile."
        )


def resolve_plugin_overlay(
    profile_plugins: list[str],
    profile_name: str,
    overlay: PluginOverlay,
) -> list[ResolvedPlugin]:
    """Merge a profile's resolved plugin list with the local.yaml overlay.

    Algorithm (mirrors :func:`setforge.preserved_keys.resolve_overlay`):

    1. For every plugin in ``profile_plugins``, in order:
       - If listed in ``overlay.remove`` -> append a
         :attr:`OverlayOrigin.LOCAL_REMOVE` entry.
       - Otherwise -> append a :attr:`OverlayOrigin.PROFILE` entry.
    2. For every plugin in ``overlay.add``, in order, that does NOT
       already appear in ``profile_plugins``: append a
       :attr:`OverlayOrigin.LOCAL_ADD` entry. (A redundant ``add`` of a
       plugin already in the profile chain is silently absorbed — mirrors
       :mod:`setforge.preserved_keys`.)

    Validation errors (both raise :class:`LocalOverlayError`):

    - ``add ∩ remove`` non-empty.
    - ``remove`` references a plugin not in ``profile_plugins``.
    """
    _check_overlay_lists(
        overlay_kind="plugin",
        profile_name=profile_name,
        profile_values=profile_plugins,
        overlay_add=list(overlay.add),
        overlay_remove=list(overlay.remove),
    )
    removed = set(overlay.remove)
    profile_set = set(profile_plugins)
    resolved: list[ResolvedPlugin] = []
    for value in profile_plugins:
        origin = (
            OverlayOrigin.LOCAL_REMOVE if value in removed else OverlayOrigin.PROFILE
        )
        resolved.append(ResolvedPlugin(value=value, origin=origin))
    for value in overlay.add:
        if value in profile_set:
            continue
        resolved.append(ResolvedPlugin(value=value, origin=OverlayOrigin.LOCAL_ADD))
    return resolved


def resolve_extension_overlay(
    profile_extensions: list[str],
    profile_name: str,
    overlay: ExtensionOverlay,
) -> list[ResolvedExtension]:
    """Merge a profile's resolved extension include list with the local.yaml overlay.

    Same algorithm as :func:`resolve_plugin_overlay`, applied to the
    extension include list. Excludes are profile-only and untouched by
    this resolver per SPEC 2.
    """
    _check_overlay_lists(
        overlay_kind="extension",
        profile_name=profile_name,
        profile_values=profile_extensions,
        overlay_add=list(overlay.add),
        overlay_remove=list(overlay.remove),
    )
    removed = set(overlay.remove)
    profile_set = set(profile_extensions)
    resolved: list[ResolvedExtension] = []
    for value in profile_extensions:
        origin = (
            OverlayOrigin.LOCAL_REMOVE if value in removed else OverlayOrigin.PROFILE
        )
        resolved.append(ResolvedExtension(value=value, origin=origin))
    for value in overlay.add:
        if value in profile_set:
            continue
        resolved.append(ResolvedExtension(value=value, origin=OverlayOrigin.LOCAL_ADD))
    return resolved


def resolve_marketplace_overlay(
    profile_marketplaces: list[str],
    profile_name: str,
    overlay: MarketplaceOverlay,
) -> list[ResolvedMarketplace]:
    """Merge a profile's resolved marketplace name list with the local.yaml overlay.

    Same algorithm as :func:`resolve_plugin_overlay`, applied to
    marketplace names. The overlay's ``add`` is a dict keyed by
    marketplace name; we work against the key set here. Source details
    (``_MarketplaceLocalDecl``) are surfaced separately by the loader at
    :func:`setforge.config.apply_local_overlay` when it merges
    ``cfg.marketplaces`` with the local overlay's add map.
    """
    overlay_add_names = list(overlay.add.keys())
    _check_overlay_lists(
        overlay_kind="marketplace",
        profile_name=profile_name,
        profile_values=profile_marketplaces,
        overlay_add=overlay_add_names,
        overlay_remove=list(overlay.remove),
    )
    removed = set(overlay.remove)
    profile_set = set(profile_marketplaces)
    resolved: list[ResolvedMarketplace] = []
    for value in profile_marketplaces:
        origin = (
            OverlayOrigin.LOCAL_REMOVE if value in removed else OverlayOrigin.PROFILE
        )
        resolved.append(ResolvedMarketplace(value=value, origin=origin))
    for value in overlay_add_names:
        if value in profile_set:
            continue
        resolved.append(
            ResolvedMarketplace(value=value, origin=OverlayOrigin.LOCAL_ADD)
        )
    return resolved


def display_tag(origin: OverlayOrigin) -> str:
    """Render an overlay-provenance tag for compare/install output.

    Single source of truth for SPEC 2 tag wording — callers MUST use
    this helper, never f-string the literal tag strings inline.

    - :attr:`OverlayOrigin.LOCAL_ADD` -> ``"[from local.yaml]"``.
    - :attr:`OverlayOrigin.LOCAL_REMOVE` -> the U+2212-prefixed
      ``"removed via local.yaml"`` wording per SPEC 2 mockup (see
      the return statement body for the exact literal — kept in one
      place to satisfy the SoT discipline + ruff RUF001 quarantine).
    - :attr:`OverlayOrigin.PROFILE` -> ``""`` (no tag for profile-origin
      entries; the caller suppresses the trailing whitespace).

    Mirrors :func:`setforge.preserved_keys.display_tag` (preserved_keys.py:140)
    in shape: exhaustive ``match`` over the StrEnum, no fallthrough.
    """
    match origin:
        case OverlayOrigin.LOCAL_ADD:
            return "[from local.yaml]"
        case OverlayOrigin.LOCAL_REMOVE:
            # U+2212 MINUS SIGN per SPEC 2 mockup — keeps column-width
            # parity with the unicode minus used elsewhere in setforge
            # output (cf. setforge.compare's overlay block renderer).
            return "[− removed via local.yaml]"  # noqa: RUF001
        case OverlayOrigin.PROFILE:
            return ""
        case _ as unreachable:
            assert_never(unreachable)


def has_local_overlay(
    resolved: list[ResolvedPlugin]
    | list[ResolvedExtension]
    | list[ResolvedMarketplace],
) -> bool:
    """Return True iff any entry in ``resolved`` originated in local.yaml.

    Drives SPEC 2's footer-summary gate — the
    ``[Host overlay summary: ...]`` line is suppressed when no overlay
    introduced any change. (Anti-smell: do NOT suppress on empty
    profile list — the gate is data-driven, not configuration-shape-driven.)
    """
    return any(
        entry.origin in (OverlayOrigin.LOCAL_ADD, OverlayOrigin.LOCAL_REMOVE)
        for entry in resolved
    )


__all__ = [
    "LocalOverlayError",
    "LocalOverlayLoadError",
    "OverlayOrigin",
    "ResolvedExtension",
    "ResolvedMarketplace",
    "ResolvedPlugin",
    "display_tag",
    "has_local_overlay",
    "resolve_extension_overlay",
    "resolve_marketplace_overlay",
    "resolve_plugin_overlay",
]
