"""Table tests for merging the W1 ``reconcile:`` block across an
``extends:`` chain — both policy axes independently plus exclude concat.

Profiles are built via ``Profile.model_validate(dict)`` (never passed as
model instances) so ``model_fields_set`` reflects only what the source
dict actually provided — the "explicitly set" checks in
``_merge_reconcile`` depend on that realism.
"""

from pathlib import Path

import pytest

from setforge.config import (
    Config,
    Profile,
    ReconcilePolicy,
    TrackedFile,
    resolve_profile,
)


def _cfg(parent: dict[str, object], child: dict[str, object] | None) -> Config:
    profiles: dict[str, Profile] = {"parent": Profile.model_validate(parent)}
    child_dict: dict[str, object] = {"extends": "parent", **(child or {})}
    profiles["child"] = Profile.model_validate(child_dict)
    return Config(
        tracked_files={"d": TrackedFile(src=Path("a"), dst="b")},
        profiles=profiles,
    )


# --- plugin policy axis ------------------------------------------------


def test_plugin_policy_child_explicit_overrides_parent() -> None:
    cfg = _cfg(
        {"reconcile": {"plugins": {"policy": "prune"}}},
        {"reconcile": {"plugins": {"policy": "additive"}}},
    )
    resolved = resolve_profile(cfg, "child")
    assert resolved.reconcile.plugins.policy is ReconcilePolicy.ADDITIVE


def test_plugin_policy_child_unset_inherits_parent() -> None:
    cfg = _cfg({"reconcile": {"plugins": {"policy": "prune"}}}, None)
    resolved = resolve_profile(cfg, "child")
    assert resolved.reconcile.plugins.policy is ReconcilePolicy.PRUNE


# --- extension policy axis ----------------------------------------------


def test_extension_policy_child_explicit_overrides_parent() -> None:
    cfg = _cfg(
        {"reconcile": {"extensions": {"policy": "prune"}}},
        {"reconcile": {"extensions": {"policy": "additive"}}},
    )
    resolved = resolve_profile(cfg, "child")
    assert resolved.reconcile.extensions.policy is ReconcilePolicy.ADDITIVE


def test_extension_policy_child_unset_inherits_parent() -> None:
    cfg = _cfg({"reconcile": {"extensions": {"policy": "prune"}}}, None)
    resolved = resolve_profile(cfg, "child")
    assert resolved.reconcile.extensions.policy is ReconcilePolicy.PRUNE


# --- axes are independent (setting one does not clobber the other) ------


def test_plugin_and_extension_policy_axes_are_independent() -> None:
    cfg = _cfg(
        {
            "reconcile": {
                "plugins": {"policy": "prune"},
                "extensions": {"policy": "report"},
            }
        },
        {"reconcile": {"plugins": {"policy": "additive"}}},
    )
    resolved = resolve_profile(cfg, "child")
    assert resolved.reconcile.plugins.policy is ReconcilePolicy.ADDITIVE
    assert resolved.reconcile.extensions.policy is ReconcilePolicy.REPORT


# --- exclude concat -------------------------------------------------------


def test_extension_exclude_concatenates_parent_and_child() -> None:
    cfg = _cfg(
        {"reconcile": {"extensions": {"exclude": ["a"]}}},
        {"reconcile": {"extensions": {"exclude": ["b"]}}},
    )
    resolved = resolve_profile(cfg, "child")
    assert resolved.reconcile.extensions.exclude == ["a", "b"]


def test_extension_exclude_dedups_repeat_first_occurrence() -> None:
    cfg = _cfg(
        {"reconcile": {"extensions": {"exclude": ["a", "b"]}}},
        {"reconcile": {"extensions": {"exclude": ["b", "c"]}}},
    )
    resolved = resolve_profile(cfg, "child")
    assert resolved.reconcile.extensions.exclude == ["a", "b", "c"]


# --- 3-level chain: grandparent policy survives two unset descendants ---


def test_reconcile_policy_from_grandparent_survives_three_level_chain() -> None:
    """Both policy axes set only at the grandparent must reach the leaf
    unchanged through two profiles that omit ``reconcile`` entirely. The
    merge rebuilds ``resolved.reconcile`` every loop iteration, so this
    guards against an accumulation-reset regression."""
    cfg = Config(
        tracked_files={"d": TrackedFile(src=Path("a"), dst="b")},
        profiles={
            "grand": Profile.model_validate(
                {
                    "reconcile": {
                        "plugins": {"policy": "prune"},
                        "extensions": {"policy": "report"},
                    }
                }
            ),
            "parent": Profile.model_validate({"extends": "grand"}),
            "child": Profile.model_validate({"extends": "parent"}),
        },
    )
    resolved = resolve_profile(cfg, "child")
    assert resolved.reconcile.plugins.policy is ReconcilePolicy.PRUNE
    assert resolved.reconcile.extensions.policy is ReconcilePolicy.REPORT


# --- sibling-set boundary: exclude set but policy left unset -------------


def test_extension_policy_inherits_when_child_sets_only_exclude() -> None:
    """A child that provides ``extensions.exclude`` but NOT
    ``extensions.policy`` must still inherit the parent's policy — the
    sibling-field-set-but-this-field-unset boundary the docstring flags."""
    cfg = _cfg(
        {"reconcile": {"extensions": {"policy": "prune"}}},
        {"reconcile": {"extensions": {"exclude": ["x"]}}},
    )
    resolved = resolve_profile(cfg, "child")
    assert resolved.reconcile.extensions.policy is ReconcilePolicy.PRUNE
    assert resolved.reconcile.extensions.exclude == ["x"]


# --- sanity: model_fields_set realism assumption underlying the above ---


@pytest.mark.parametrize(
    ("src", "expect_set"),
    [
        ({}, False),
        ({"reconcile": {"plugins": {"policy": "prune"}}}, True),
    ],
)
def test_model_fields_set_reflects_dict_construction(
    src: dict[str, object], expect_set: bool
) -> None:
    p = Profile.model_validate(src)
    assert ("reconcile" in p.model_fields_set) is expect_set
