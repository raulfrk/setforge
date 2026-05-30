"""Resolved-preserved-key provenance shape and overlay resolver.

Resolves a TrackedFile's effective preserve_user_keys list from
``profile.preserve_user_keys`` and the optional ``local.yaml`` overlay
(``profile UNION local.add MINUS local.remove``), tagging each key
with its origin so compare/deploy output can display provenance per
mockup B.

The resolver is loader-driven — see
:func:`setforge.config.load_config_with_overlay`. Per anti-smell
guidance: this module is intentionally free of import-time references
to :mod:`setforge.source`, so :mod:`setforge.source` can lazy-import
the overlay model at load-time without circularity.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import assert_never

from setforge.errors import ConfigError


class KeyOrigin(StrEnum):
    """Provenance of one entry in a TrackedFile's resolved-preserved-key list.

    Wire shape for compare/install output formatters — display tags
    (e.g. ``[from local.yaml]``) are derived from these members, never
    parsed from strings. The three members exhaust mockup B's display
    surface.
    """

    FROM_PROFILE = "from_profile"
    FROM_LOCAL_YAML = "from_local_yaml"
    REMOVED_VIA_LOCAL = "removed_via_local"


@dataclass(frozen=True, slots=True)
class ResolvedPreservedKey:
    """One entry in a TrackedFile's resolved preserve_user_keys chain.

    Captures the key itself, its provenance (:class:`KeyOrigin`), and
    (for FROM_PROFILE / REMOVED_VIA_LOCAL) the name of the profile that
    declared the key in its ``preserve_user_keys`` block. The
    ``source_profile`` is the profile chain's leaf name — the same name
    surface ``compare`` / ``install`` already display elsewhere.

    Mockup B display mapping:

    - ``FROM_PROFILE`` -> ``[from profile <source_profile>]``
    - ``FROM_LOCAL_YAML`` -> ``[from local.yaml]``
    - ``REMOVED_VIA_LOCAL`` -> ``[removed via local.yaml]``
    """

    key: str
    origin: KeyOrigin
    source_profile: str | None


class PreserveUserKeysOverlayError(ConfigError):
    """Raised when the local.yaml ``preserve_user_keys`` overlay is
    self-contradictory or references a key absent from the profile chain.

    Subclasses :class:`ConfigError` so :func:`setforge.cli.validate`'s
    existing config-error handling path catches it uniformly. The
    instance's message carries the canonical phrase the validate-side
    formatter keys on:

    - ``"in both add and remove"`` for the add∩remove collision case.
    - ``"not in profile chain"`` for the unknown-remove case.
    """


def resolve_overlay(
    profile_keys: list[str],
    profile_name: str,
    overlay_add: list[str],
    overlay_remove: list[str],
) -> list[ResolvedPreservedKey]:
    """Merge profile keys with the local.yaml add/remove overlay.

    Algorithm (see SPEC 8):

    1. For every key in ``profile_keys``, in order:
       - If the key is in ``overlay_remove`` -> append a
         ``REMOVED_VIA_LOCAL`` entry tagged with ``profile_name``.
       - Otherwise -> append a ``FROM_PROFILE`` entry tagged with
         ``profile_name``.
    2. For every key in ``overlay_add``, in order, that does NOT
       already appear in ``profile_keys``: append a ``FROM_LOCAL_YAML``
       entry with ``source_profile=None``. (A redundant ``add`` of a
       key already in the profile chain is silently absorbed — the key
       stays tagged FROM_PROFILE; treating it as an error would
       require local.yaml to be aware of the chain.)

    Validation errors (both raise :class:`PreserveUserKeysOverlayError`):

    - ``add ∩ remove`` non-empty (a key appears in both lists in this
      overlay — contradictory, surface immediately).
    - ``remove`` references a key not present in ``profile_keys`` (a
      typo or stale overlay — surface so the user is forced to fix it
      rather than silently no-op).

    Anti-smell: provenance is structural data (StrEnum + dataclass);
    callers must NEVER string-parse the display tags back into origins.
    """
    collisions = sorted(set(overlay_add) & set(overlay_remove))
    if collisions:
        joined = ", ".join(repr(k) for k in collisions)
        raise PreserveUserKeysOverlayError(
            f"preserve_user_keys overlay: key(s) appear in both add and remove "
            f"of local.yaml: {joined}. Drop one of the two list entries."
        )

    profile_set = set(profile_keys)
    unknown_removes = sorted(set(overlay_remove) - profile_set)
    if unknown_removes:
        joined = ", ".join(repr(k) for k in unknown_removes)
        raise PreserveUserKeysOverlayError(
            f"preserve_user_keys overlay: remove key(s) not in profile chain "
            f"{profile_name!r}: {joined}. Remove the entry from local.yaml or "
            f"add the key to the profile's preserve_user_keys."
        )

    removed = set(overlay_remove)
    resolved: list[ResolvedPreservedKey] = []
    for key in profile_keys:
        origin = (
            KeyOrigin.REMOVED_VIA_LOCAL if key in removed else KeyOrigin.FROM_PROFILE
        )
        resolved.append(ResolvedPreservedKey(key, origin, profile_name))
    for key in overlay_add:
        if key in profile_set:
            # Redundant add — already covered by FROM_PROFILE above.
            continue
        resolved.append(ResolvedPreservedKey(key, KeyOrigin.FROM_LOCAL_YAML, None))
    return resolved


def display_tag(resolved: ResolvedPreservedKey) -> str:
    """Render one :class:`ResolvedPreservedKey`'s mockup-B provenance tag.

    Exhaustive over :class:`KeyOrigin` — adding a new variant requires
    extending this match. Anti-smell: callers must NOT string-parse
    this output back into origins; ``resolved.origin`` carries the
    canonical wire shape.
    """
    match resolved.origin:
        case KeyOrigin.FROM_PROFILE:
            return f"[from profile {resolved.source_profile}]"
        case KeyOrigin.FROM_LOCAL_YAML:
            return "[from local.yaml]"
        case KeyOrigin.REMOVED_VIA_LOCAL:
            return "[removed via local.yaml]"
        case _ as unreachable:
            assert_never(unreachable)


def has_local_yaml_overlay(resolved_list: list[ResolvedPreservedKey]) -> bool:
    """Return True iff at least one resolved key originated in local.yaml.

    Drives mockup B's "applying host overlay" output gate — the
    overlay block is suppressed when no tracked_file's resolved list
    carries any FROM_LOCAL_YAML or REMOVED_VIA_LOCAL entry. (Anti-smell:
    do NOT suppress on empty profile chain — the gate is data-driven,
    not configuration-shape-driven.)
    """
    return any(
        k.origin in (KeyOrigin.FROM_LOCAL_YAML, KeyOrigin.REMOVED_VIA_LOCAL)
        for k in resolved_list
    )


__all__ = [
    "KeyOrigin",
    "PreserveUserKeysOverlayError",
    "ResolvedPreservedKey",
    "display_tag",
    "has_local_yaml_overlay",
    "resolve_overlay",
]
