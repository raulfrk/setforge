"""Unit tests for the local.yaml preserve_user_keys overlay resolver.

Spec: SPEC 8 (mockup B). Validates the semantics of
:func:`setforge.preserved_keys.resolve_overlay` — the contract that
``compare``/``install`` rely on to display provenance tags per
mockup B's ``[from local.yaml]`` / ``[from profile X]`` /
``[removed via local.yaml]`` shape.
"""

from __future__ import annotations

import pytest

from setforge.errors import ConfigError
from setforge.preserved_keys import (
    KeyOrigin,
    PreserveUserKeysOverlayError,
    ResolvedPreservedKey,
    resolve_overlay,
)


def test_empty_overlay_returns_profile_keys_tagged_from_profile() -> None:
    resolved = resolve_overlay(
        profile_keys=["a", "b"],
        profile_name="vm-headless",
        overlay_add=[],
        overlay_remove=[],
    )
    assert resolved == [
        ResolvedPreservedKey("a", KeyOrigin.FROM_PROFILE, "vm-headless"),
        ResolvedPreservedKey("b", KeyOrigin.FROM_PROFILE, "vm-headless"),
    ]


def test_add_only_appends_local_yaml_keys_after_profile_keys() -> None:
    resolved = resolve_overlay(
        profile_keys=["a"],
        profile_name="vm-headless",
        overlay_add=["x", "y"],
        overlay_remove=[],
    )
    assert resolved == [
        ResolvedPreservedKey("a", KeyOrigin.FROM_PROFILE, "vm-headless"),
        ResolvedPreservedKey("x", KeyOrigin.FROM_LOCAL_YAML, None),
        ResolvedPreservedKey("y", KeyOrigin.FROM_LOCAL_YAML, None),
    ]


def test_remove_only_tags_profile_key_as_removed_via_local() -> None:
    resolved = resolve_overlay(
        profile_keys=["a", "b"],
        profile_name="vm-headless",
        overlay_add=[],
        overlay_remove=["a"],
    )
    assert resolved == [
        ResolvedPreservedKey("a", KeyOrigin.REMOVED_VIA_LOCAL, "vm-headless"),
        ResolvedPreservedKey("b", KeyOrigin.FROM_PROFILE, "vm-headless"),
    ]


def test_add_and_remove_no_collision_mixed() -> None:
    resolved = resolve_overlay(
        profile_keys=["a", "b"],
        profile_name="p",
        overlay_add=["c"],
        overlay_remove=["a"],
    )
    assert resolved == [
        ResolvedPreservedKey("a", KeyOrigin.REMOVED_VIA_LOCAL, "p"),
        ResolvedPreservedKey("b", KeyOrigin.FROM_PROFILE, "p"),
        ResolvedPreservedKey("c", KeyOrigin.FROM_LOCAL_YAML, None),
    ]


def test_absent_overlay_identity() -> None:
    """profile_keys=[] + no overlay -> empty resolved list."""
    resolved = resolve_overlay(
        profile_keys=[],
        profile_name="p",
        overlay_add=[],
        overlay_remove=[],
    )
    assert resolved == []


def test_add_duplicating_profile_key_is_silently_dedup() -> None:
    """If local.yaml `add` lists a key already in the profile, it stays
    FROM_PROFILE (no duplicate FROM_LOCAL_YAML entry). Treating the
    redundant add as a hard error would require local.yaml to be aware
    of the entire profile chain — overly strict for a benign no-op."""
    resolved = resolve_overlay(
        profile_keys=["a"],
        profile_name="p",
        overlay_add=["a"],
        overlay_remove=[],
    )
    assert resolved == [
        ResolvedPreservedKey("a", KeyOrigin.FROM_PROFILE, "p"),
    ]


def test_collision_add_and_remove_same_key_raises() -> None:
    """add + remove of the same key is contradictory; surface immediately."""
    with pytest.raises(PreserveUserKeysOverlayError) as exc_info:
        resolve_overlay(
            profile_keys=["a"],
            profile_name="p",
            overlay_add=["x"],
            overlay_remove=["x"],
        )
    assert "in both add and remove" in str(exc_info.value)
    assert "'x'" in str(exc_info.value)


def test_collision_subclasses_config_error() -> None:
    """PreserveUserKeysOverlayError must be catchable as ConfigError so
    setforge validate's existing error handling stays uniform."""
    with pytest.raises(ConfigError):
        resolve_overlay(
            profile_keys=["a"],
            profile_name="p",
            overlay_add=["x"],
            overlay_remove=["x"],
        )


def test_remove_of_key_not_in_profile_chain_raises() -> None:
    """A `remove:` referencing a key the resolved profile chain never
    declared is a misconfiguration — surface with the canonical phrase
    so did-you-mean's setforge validate can format it."""
    with pytest.raises(PreserveUserKeysOverlayError) as exc_info:
        resolve_overlay(
            profile_keys=["a", "b"],
            profile_name="p",
            overlay_add=[],
            overlay_remove=["nonexistent"],
        )
    assert "not in profile chain" in str(exc_info.value)
    assert "'nonexistent'" in str(exc_info.value)


def test_resolved_key_is_frozen_dataclass() -> None:
    """Provenance is structural data, not mutable. Per anti-smell:
    must NOT mutate Pydantic models in-place; same posture for the
    resolver's value object."""
    rk = ResolvedPreservedKey("k", KeyOrigin.FROM_PROFILE, "p")
    with pytest.raises(AttributeError):
        rk.key = "other"  # type: ignore[misc]


def test_resolved_key_uses_slots() -> None:
    """slots=True is the canonical shape for setforge's value objects;
    asserts no accidental __dict__ creeps in."""
    rk = ResolvedPreservedKey("k", KeyOrigin.FROM_PROFILE, "p")
    assert not hasattr(rk, "__dict__")


def test_key_origin_is_str_enum() -> None:
    """Anti-smell: provenance must NOT be string-parsed. Enum values
    are the canonical wire shape; display tags are derived from them."""
    assert KeyOrigin.FROM_PROFILE.value == "from_profile"
    assert KeyOrigin.FROM_LOCAL_YAML.value == "from_local_yaml"
    assert KeyOrigin.REMOVED_VIA_LOCAL.value == "removed_via_local"
    # StrEnum members ARE str instances.
    assert isinstance(KeyOrigin.FROM_PROFILE, str)
