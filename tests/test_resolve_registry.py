"""Tests for the resolver registry: separate from the provisioner registry."""

from typing import ClassVar

import pytest

from setforge.errors import (
    DuplicateResolverType,
    SetforgeError,
    UnknownResolverType,
)
from setforge.provision.resolve import registry
from setforge.provision.resolve.protocol import (
    IntegrityKind,
    PackageType,
    ResolvedPin,
)


class _DummyResolver:
    type: ClassVar[PackageType] = PackageType.CARGO

    def resolve(self, item: object) -> ResolvedPin:
        return ResolvedPin(
            type=PackageType.CARGO,
            key="k",
            version="1.0.0",
            integrity="sha256:00",
            integrity_kind=IntegrityKind.CHECKSUM,
        )


@pytest.fixture(autouse=True)
def _isolate_registry() -> object:
    saved = dict(registry._REGISTRY)
    registry._REGISTRY.clear()
    try:
        yield
    finally:
        registry._REGISTRY.clear()
        registry._REGISTRY.update(saved)


def test_register_and_get_returns_instance() -> None:
    registry.register(PackageType.CARGO)(_DummyResolver)
    got = registry.get_resolver(PackageType.CARGO)
    assert isinstance(got, _DummyResolver)


def test_register_returns_the_class() -> None:
    returned = registry.register(PackageType.GO)(_DummyResolver)
    assert returned is _DummyResolver


def test_duplicate_registration_raises() -> None:
    registry.register(PackageType.PLUGIN)(_DummyResolver)
    with pytest.raises(DuplicateResolverType):
        registry.register(PackageType.PLUGIN)(_DummyResolver)


def test_unknown_type_raises_and_names_it() -> None:
    with pytest.raises(UnknownResolverType) as exc_info:
        registry.get_resolver(PackageType.EXTENSION)
    assert isinstance(exc_info.value, SetforgeError)
    assert "extension" in str(exc_info.value)


def test_registry_is_separate_from_provisioner_registry() -> None:
    from setforge.provision import registry as prov_registry

    assert registry._REGISTRY is not prov_registry._REGISTRY
    before = dict(prov_registry._REGISTRY)
    registry.register(PackageType.PYTHON)(_DummyResolver)
    assert before == prov_registry._REGISTRY
