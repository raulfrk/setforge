"""Apply a parsed ``setforge.lock`` onto resolved provision items.

OFFLINE-SAFE: imports NO resolver/network. github_release rebuilds
``item.config`` with the concrete tag (``tag: latest`` is an invalid URL)."""

from __future__ import annotations

import dataclasses

from setforge.config import GitHubReleasePackage
from setforge.lockfile import LockFile
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
    items: list[ProvisionItem], lock: LockFile
) -> list[ProvisionItem]:
    by_key: dict[tuple[str, str], ResolvedPin] = {
        (pin.type.value, pin.key): pin for pin in lock.packages
    }
    out: list[ProvisionItem] = []
    for item in items:
        pin = by_key.get((item.type, item.identity.key))
        out.append(item if pin is None else _override(item, pin))
    return out


def _override(item: ProvisionItem, pin: ResolvedPin) -> ProvisionItem:
    if isinstance(item.config, GitHubReleasePackage):
        return dataclasses.replace(
            item,
            version=pin.version,
            checksum=pin.integrity,
            config=item.config.model_copy(
                update={"tag": pin.version, "checksum": pin.integrity}
            ),
        )
    return dataclasses.replace(item, version=pin.version, checksum=pin.integrity)
