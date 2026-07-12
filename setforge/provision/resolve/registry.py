"""Type-dispatch registry for resolvers (spec §B2).

A dedicated registry — SEPARATE from :mod:`setforge.provision.registry` — so
resolution and provisioning stay decoupled (D6). A per-ecosystem resolver
registers itself with ``@register(PackageType.X)`` in its own module (later
tasks); :func:`get_resolver` returns the registered instance by
:class:`~setforge.provision.resolve.protocol.PackageType`.

Mirrors the provisioner registry's shape (``@register`` decorator + a lookup
that names the known types on a miss), but keyed by ``PackageType`` and holding
its own ``_REGISTRY`` dict.
"""

from collections.abc import Callable

from setforge.errors import DuplicateResolverType, UnknownResolverType
from setforge.provision.resolve.protocol import PackageType, Resolver

_REGISTRY: dict[PackageType, type[Resolver]] = {}


def register(type_: PackageType) -> Callable[[type[Resolver]], type[Resolver]]:
    """Return a class decorator that records a :class:`Resolver` for ``type_``.

    Raises :class:`~setforge.errors.DuplicateResolverType` if ``type_`` is
    already claimed — two resolvers sharing a key is a programming error caught
    at import time.
    """

    def _decorator(cls: type[Resolver]) -> type[Resolver]:
        if type_ in _REGISTRY:
            raise DuplicateResolverType(
                f"resolver type {type_.value!r} is already registered to "
                f"{_REGISTRY[type_].__name__!r}"
            )
        _REGISTRY[type_] = cls
        return cls

    return _decorator


def get_resolver(type_: PackageType) -> Resolver:
    """Instantiate the resolver registered for ``type_``.

    Raises :class:`~setforge.errors.UnknownResolverType` (naming the known
    types) when no resolver claims ``type_``.
    """
    try:
        cls = _REGISTRY[type_]
    except KeyError:
        known = ", ".join(sorted(t.value for t in _REGISTRY)) or "(none)"
        raise UnknownResolverType(
            f"no resolver registered for type {type_.value!r}; known types: {known}"
        ) from None
    return cls()
