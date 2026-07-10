"""Tests for the provisioner registry (spec §5).

A ``@register("type")`` decorator records a Provisioner subclass in a
module-level dict; ``build(item)`` instantiates by ``item.type``. Registering a
new type must touch ONLY the new provisioner file (the decorator), never the
registry module itself.
"""

from collections.abc import Sequence

import pytest

from setforge.errors import (
    DuplicateProvisionerType,
    SetforgeError,
    UnknownProvisionerType,
)
from setforge.provision import registry
from setforge.provision.protocol import (
    Identity,
    ProvisionDelta,
    Provisioner,
    ProvisionItem,
    ProvisionOutcome,
)


def _item(type_: str) -> ProvisionItem:
    return ProvisionItem(type=type_, identity=Identity(key="k", display="k"))


class _Dummy(Provisioner):
    """Minimal concrete provisioner for exercising the registry."""

    def probe(self) -> set[Identity]:
        return set()

    def plan(
        self, items: Sequence[ProvisionItem], installed: set[Identity]
    ) -> ProvisionDelta:
        return ProvisionDelta()

    def apply_one(self, item: ProvisionItem) -> ProvisionOutcome:
        raise NotImplementedError

    def uninstall_one(self, identity: Identity) -> None:
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _isolate_registry() -> object:
    """Snapshot + restore the module-level registry around each test."""
    saved = dict(registry._REGISTRY)
    try:
        yield
    finally:
        registry._REGISTRY.clear()
        registry._REGISTRY.update(saved)


def test_register_and_build_returns_instance() -> None:
    registry.register("dummy_ok")(_Dummy)
    built = registry.build(_item("dummy_ok"))
    assert isinstance(built, _Dummy)


def test_register_returns_the_class() -> None:
    # Decorator must return the class unchanged so it stays usable as a name.
    returned = registry.register("dummy_ret")(_Dummy)
    assert returned is _Dummy


def test_duplicate_registration_raises() -> None:
    registry.register("dummy_dup")(_Dummy)
    with pytest.raises(DuplicateProvisionerType):
        registry.register("dummy_dup")(_Dummy)


def test_unknown_type_raises() -> None:
    with pytest.raises(UnknownProvisionerType) as exc_info:
        registry.build(_item("no_such_type"))
    assert isinstance(exc_info.value, SetforgeError)
    assert "no_such_type" in str(exc_info.value)
