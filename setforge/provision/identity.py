"""The canonical package → :class:`Identity` mapping.

One home for the dedup key so ref-declared packages (``dispatch``) and
inline/ref bundle sources (``bundle``) agree on the same key. The match/case
over the ``Package`` union ends in a ``case _`` wildcard that raises, so an
added package kind is caught at runtime by that wildcard rather than by mypy
at type-check time.
"""

from __future__ import annotations

from setforge.config import (
    CargoPackage,
    GitHubReleasePackage,
    GoPackage,
    LocalPackage,
    Package,
    PythonPackage,
)
from setforge.provision.protocol import Identity


def package_identity(pkg: Package) -> Identity:
    match pkg:
        case CargoPackage():
            name = pkg.crate
        case PythonPackage():
            name = pkg.package
        case GoPackage():
            name = pkg.module
        case GitHubReleasePackage():
            name = pkg.repo
        case LocalPackage():
            name = pkg.binary
        case _:  # pragma: no cover - Plugin/Extension route elsewhere
            raise AssertionError(f"no identity mapping for package {pkg!r}")
    return Identity(key=name, display=name)


def package_version(pkg: Package) -> str | None:
    """Return the provider's desired version in the shared item protocol."""
    if isinstance(pkg, GitHubReleasePackage):
        return pkg.tag
    value = getattr(pkg, "version", None)
    return value if isinstance(value, str) else None
