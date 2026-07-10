"""Type-dispatch registry for provisioners (spec §5).

A new ecosystem provisioner registers itself with ``@register("type")`` in its
own module — adding one never requires editing this file. :func:`build` maps a
:class:`~setforge.provision.protocol.ProvisionItem` to a fresh provisioner
instance by its ``type``.
"""

from collections.abc import Callable

from setforge.errors import DuplicateProvisionerType, UnknownProvisionerType
from setforge.provision.protocol import Provisioner, ProvisionItem

_REGISTRY: dict[str, type[Provisioner]] = {}


def register(type_: str) -> Callable[[type[Provisioner]], type[Provisioner]]:
    """Return a class decorator that records a :class:`Provisioner` subclass.

    Raises :class:`DuplicateProvisionerType` if ``type_`` is already claimed —
    two provisioners sharing a registry key is a programming error caught at
    import time.
    """

    def _decorator(cls: type[Provisioner]) -> type[Provisioner]:
        if type_ in _REGISTRY:
            raise DuplicateProvisionerType(
                f"provisioner type {type_!r} is already registered to "
                f"{_REGISTRY[type_].__name__!r}"
            )
        _REGISTRY[type_] = cls
        return cls

    return _decorator


def build(item: ProvisionItem) -> Provisioner:
    """Instantiate the provisioner registered for ``item.type``.

    Raises :class:`UnknownProvisionerType` (naming the known types) when no
    provisioner claims ``item.type``.
    """
    try:
        cls = _REGISTRY[item.type]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise UnknownProvisionerType(
            f"no provisioner registered for type {item.type!r}; known types: {known}"
        ) from None
    return cls()
