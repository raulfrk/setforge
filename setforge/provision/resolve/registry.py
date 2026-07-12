"""Type-dispatch registry for resolvers — separate from the provisioner
registry (D6)."""

from collections.abc import Callable

from setforge.errors import DuplicateResolverType, UnknownResolverType
from setforge.provision.resolve.protocol import PackageType, Resolver

_REGISTRY: dict[PackageType, type[Resolver]] = {}


def register(type_: PackageType) -> Callable[[type[Resolver]], type[Resolver]]:
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
    try:
        cls = _REGISTRY[type_]
    except KeyError:
        known = ", ".join(sorted(t.value for t in _REGISTRY)) or "(none)"
        raise UnknownResolverType(
            f"no resolver registered for type {type_.value!r}; known types: {known}"
        ) from None
    return cls()
