"""Apply a parsed ``setforge.lock`` onto resolved provision items.

OFFLINE-SAFE: imports NO resolver/network. github_release rebuilds
``item.config`` with the concrete tag (``tag: latest`` is an invalid URL)."""

from __future__ import annotations

import dataclasses

from setforge.config import GitHubReleasePackage
from setforge.errors import ConfigError
from setforge.lockfile import LockFile
from setforge.platform_assets import (
    HostPlatform,
    current_host_platform,
    normalize_platform_arch,
    normalize_platform_os,
    select_platform_asset,
)
from setforge.provision.protocol import ProvisionItem
from setforge.provision.resolve.protocol import PackageType, ResolvedPin


def extension_pins(lock: LockFile | None) -> dict[str, ResolvedPin]:
    # Casefolded (unlike plugin_pins): VS Code extension ids are case-insensitive.
    if lock is None:
        return {}
    return {
        pin.key.casefold(): pin
        for pin in lock.packages
        if pin.type is PackageType.EXTENSION
    }


def plugin_pins(lock: LockFile | None) -> dict[str, ResolvedPin]:
    if lock is None:
        return {}
    return {pin.key: pin for pin in lock.packages if pin.type is PackageType.PLUGIN}


def apply_lock_to_items(
    items: list[ProvisionItem],
    lock: LockFile,
    *,
    platform_os: str | None = None,
    platform_arch: str | None = None,
) -> list[ProvisionItem]:
    by_key: dict[tuple[str, str], ResolvedPin] = {
        (pin.type.value, pin.key): pin for pin in lock.packages
    }
    out: list[ProvisionItem] = []
    for item in items:
        pin = by_key.get((item.type, item.identity.key))
        out.append(
            item
            if pin is None
            else _override(
                item,
                pin,
                platform_os=platform_os,
                platform_arch=platform_arch,
            )
        )
    return out


def _override(
    item: ProvisionItem,
    pin: ResolvedPin,
    *,
    platform_os: str | None,
    platform_arch: str | None,
) -> ProvisionItem:
    if isinstance(item.config, GitHubReleasePackage):
        if item.config.assets is not None or pin.artifacts:
            return _override_portable_release(
                item,
                pin,
                platform_os=platform_os,
                platform_arch=platform_arch,
            )
        return dataclasses.replace(
            item,
            version=pin.version,
            checksum=pin.integrity,
            config=item.config.model_copy(
                update={"tag": pin.version, "checksum": pin.integrity}
            ),
        )
    return dataclasses.replace(item, version=pin.version, checksum=pin.integrity)


def _override_portable_release(
    item: ProvisionItem,
    pin: ResolvedPin,
    *,
    platform_os: str | None,
    platform_arch: str | None,
) -> ProvisionItem:
    config = item.config
    if not isinstance(config, GitHubReleasePackage):  # pragma: no cover
        raise ConfigError("portable release pin applied to non-release package")
    if config.assets is None or not pin.artifacts:
        raise ConfigError(
            f"github_release lock/config artifact mode mismatch for {config.repo!r}"
        )
    declared = {
        (variant.os or "*", variant.arch or "*", variant.asset): variant
        for variant in config.assets
    }
    locked = {(row.os, row.arch, row.asset): row for row in pin.artifacts}
    if declared.keys() != locked.keys():
        raise ConfigError(
            f"github_release lock artifacts do not match config for {config.repo!r}"
        )
    for key, variant in declared.items():
        configured_checksum = variant.checksum
        if (
            configured_checksum is not None
            and configured_checksum != locked[key].checksum
        ):
            raise ConfigError(
                f"github_release lock checksum does not match config for "
                f"{config.repo!r} asset {variant.asset!r}"
            )
    if platform_os is None or platform_arch is None:
        current = current_host_platform()
        host_os = (
            current.os if platform_os is None else normalize_platform_os(platform_os)
        )
        host_arch = (
            current.arch
            if platform_arch is None
            else normalize_platform_arch(platform_arch)
        )
    else:
        host_os = normalize_platform_os(platform_os)
        host_arch = normalize_platform_arch(platform_arch)
    selected = select_platform_asset(config.assets, os=host_os, arch=host_arch)
    selector = (selected.os or "*", selected.arch or "*", selected.asset)
    artifact = locked.get(selector)
    if artifact is None:  # pragma: no cover - correspondence checked above
        raise ConfigError("selected github_release artifact is absent from lock")
    selected_config = config.model_copy(
        update={
            "tag": pin.version,
            "asset": selected.asset,
            "assets": None,
            "checksum": artifact.checksum,
        }
    )
    return dataclasses.replace(
        item,
        version=pin.version,
        checksum=artifact.checksum,
        artifact=selected.asset,
        platform=HostPlatform(host_os, host_arch).key,
        config=selected_config,
    )
