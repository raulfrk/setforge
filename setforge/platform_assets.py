"""Pure platform release-asset selection."""

from __future__ import annotations

import platform
from dataclasses import dataclass

from setforge.config import PlatformAssetVariant
from setforge.errors import ConfigError

_OS_ALIASES = {
    "linux": "linux",
    "linux2": "linux",
    "darwin": "macos",
    "mac": "macos",
    "macos": "macos",
    "osx": "macos",
}
_ARCH_ALIASES = {
    "x86_64": "x86_64",
    "x86-64": "x86_64",
    "amd64": "x86_64",
    "x64": "x86_64",
    "aarch64": "aarch64",
    "arm64": "aarch64",
    "arm64-v8a": "aarch64",
}


@dataclass(frozen=True, slots=True)
class HostPlatform:
    """Canonical host coordinates used for release-asset selection."""

    os: str
    arch: str

    @property
    def key(self) -> str:
        """Return the stable lock/receipt platform key."""
        return f"{self.os}-{self.arch}"


def normalize_platform_os(value: str) -> str:
    """Return a canonical supported operating-system token."""
    normalized = _OS_ALIASES.get(value.strip().casefold())
    if normalized is None:
        raise ValueError(f"unsupported platform operating system: {value!r}")
    return normalized


def normalize_platform_arch(value: str) -> str:
    """Return a canonical supported architecture token."""
    normalized = _ARCH_ALIASES.get(value.strip().casefold())
    if normalized is None:
        raise ValueError(f"unsupported platform architecture: {value!r}")
    return normalized


def current_host_platform() -> HostPlatform:
    """Read and canonicalize the current operating system and architecture."""
    try:
        return HostPlatform(
            os=normalize_platform_os(platform.system()),
            arch=normalize_platform_arch(platform.machine()),
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def select_platform_asset(
    variants: tuple[PlatformAssetVariant, ...], *, os: str, arch: str
) -> PlatformAssetVariant:
    """Select the unique highest-specificity variant for one platform."""
    try:
        canonical_os = normalize_platform_os(os)
        canonical_arch = normalize_platform_arch(arch)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    ranked: list[tuple[int, PlatformAssetVariant]] = []
    for variant in variants:
        if variant.os not in {None, canonical_os} or variant.arch not in {
            None,
            canonical_arch,
        }:
            continue
        rank = (2 if variant.os is not None else 0) + (
            1 if variant.arch is not None else 0
        )
        ranked.append((rank, variant))
    if not ranked:
        raise ConfigError(
            f"no release asset matches platform {canonical_os}/{canonical_arch}"
        )
    winning_rank = max(rank for rank, _variant in ranked)
    winners = [variant for rank, variant in ranked if rank == winning_rank]
    if len(winners) != 1:
        raise ConfigError(
            f"release asset selection is ambiguous for {canonical_os}/{canonical_arch}"
        )
    return winners[0]
