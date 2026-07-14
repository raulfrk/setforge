import pytest
from pydantic import ValidationError

from setforge.config import (
    ExtensionReconcile,
    PluginReconcile,
    Profile,
    ReconcilePolicy,
    ReconcileSpec,
)


def test_profile_reconcile_block_validates_and_round_trips() -> None:
    p = Profile.model_validate(
        {
            "reconcile": {
                "plugins": {"policy": "prune"},
                "extensions": {"exclude": ["x"], "policy": "additive"},
            }
        }
    )
    assert p.reconcile.plugins.policy is ReconcilePolicy.PRUNE
    assert p.reconcile.extensions.policy is ReconcilePolicy.ADDITIVE
    assert p.reconcile.extensions.exclude == ["x"]


def test_profile_reconcile_defaults_when_absent() -> None:
    p = Profile.model_validate({})
    assert p.reconcile == ReconcileSpec()
    assert p.reconcile.plugins.policy is ReconcilePolicy.ADDITIVE
    assert p.reconcile.extensions.policy is ReconcilePolicy.ADDITIVE
    assert p.reconcile.extensions.exclude == []


def test_reconcile_spec_rejects_unknown_key() -> None:
    with pytest.raises(ValidationError):
        Profile.model_validate({"reconcile": {"bogus": True}})


def test_plugin_reconcile_rejects_unknown_key() -> None:
    with pytest.raises(ValidationError):
        PluginReconcile(policy=ReconcilePolicy.ADDITIVE, bogus="y")  # type: ignore[call-arg]


def test_extension_reconcile_rejects_unknown_key() -> None:
    with pytest.raises(ValidationError):
        ExtensionReconcile(bogus="y")  # type: ignore[call-arg]
