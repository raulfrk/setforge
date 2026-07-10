"""Tests for the provisioner protocol surface (spec §2) + error model (§5)."""

from dataclasses import FrozenInstanceError

import pytest
from pydantic import BaseModel

from setforge.errors import ProvisionItemFailed, SetforgeError
from setforge.provision.protocol import (
    DesiredState,
    Identity,
    Outcome,
    ProvisionDelta,
    Provisioner,
    ProvisionItem,
    ProvisionOutcome,
    ReconcileResult,
)


def test_outcome_values() -> None:
    assert Outcome.OK == "ok"
    assert Outcome.SKIP == "skip"
    assert Outcome.SOFT == "soft"
    assert Outcome.HARD == "hard"


def test_desired_state_values() -> None:
    assert DesiredState.ABSENT == "absent"
    assert DesiredState.PRESENT == "present"
    assert DesiredState.ACTIVE == "active"


def test_identity_is_frozen() -> None:
    ident = Identity(key="k", display="Display")
    with pytest.raises(FrozenInstanceError):
        ident.key = "other"  # type: ignore[misc]


def test_identity_matches_on_key_only() -> None:
    # Two identities with the same normalized key but a different display
    # form MUST be equal AND hash-equal: matching is on .key, display only
    # carries the original form for subprocess invocation.
    a = Identity(key="k", display="A")
    b = Identity(key="k", display="B")
    assert a == b
    assert hash(a) == hash(b)
    assert len({a, b}) == 1


def test_identity_different_key_not_equal() -> None:
    assert Identity(key="k1", display="X") != Identity(key="k2", display="X")


def test_provision_item_defaults() -> None:
    item = ProvisionItem(type="reference", identity=Identity(key="k", display="k"))
    assert item.desired is DesiredState.ACTIVE
    assert item.version is None
    assert item.checksum is None
    # config defaults to a validated pydantic model, never a raw dict.
    assert isinstance(item.config, BaseModel)


def test_provision_outcome_default_detail() -> None:
    item = ProvisionItem(type="reference", identity=Identity(key="k", display="k"))
    outcome = ProvisionOutcome(item=item, outcome=Outcome.SKIP)
    assert outcome.detail == ""


def test_provision_delta_is_empty() -> None:
    assert ProvisionDelta().is_empty() is True
    populated = ProvisionDelta(installed=(Identity(key="k", display="k"),))
    assert populated.is_empty() is False
    activated = ProvisionDelta(activated=(Identity(key="k", display="k"),))
    assert activated.is_empty() is False


def test_reconcile_result_defaults() -> None:
    result = ReconcileResult(delta=ProvisionDelta())
    assert result.outcomes == ()
    assert result.reported is False


def test_provisioner_is_abstract() -> None:
    with pytest.raises(TypeError):
        Provisioner()  # type: ignore[abstract]


def test_provision_item_failed_carries_fields() -> None:
    exc = ProvisionItemFailed(
        item_id="ripgrep",
        error_summary="checksum mismatch",
        full_stderr="long trace...\nmore",
        kind=Outcome.HARD,
    )
    assert exc.item_id == "ripgrep"
    assert exc.error_summary == "checksum mismatch"
    assert exc.full_stderr == "long trace...\nmore"
    assert exc.kind is Outcome.HARD
    assert isinstance(exc, SetforgeError)
