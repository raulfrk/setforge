"""Tests for the resolver registry.

A ``@register(PackageType.X)`` decorator records a Resolver in a module-level
dict SEPARATE from the provisioner registry; ``get_resolver(type)`` instantiates
by ``PackageType``. Registering a new resolver must touch ONLY the new resolver
file, never the registry module.
"""

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
    """Minimal Resolver-shaped class for exercising the registry."""

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
    """Snapshot + restore the module-level registry around each test.

    Clears the registry for the duration of the test so real resolver modules
    (cargo/go/python) that self-register at import time do not collide with the
    dummy registrations these tests make, then restores the real set on exit.
    """
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

    # Distinct dict objects: registering a resolver never mutates the
    # provisioner registry, and get_resolver never resolves a provisioner.
    assert registry._REGISTRY is not prov_registry._REGISTRY
    before = dict(prov_registry._REGISTRY)
    registry.register(PackageType.PYTHON)(_DummyResolver)
    assert before == prov_registry._REGISTRY
