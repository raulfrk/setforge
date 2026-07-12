"""Apply a parsed ``setforge.lock`` onto resolved provision items (spec §B4).

The lock-CONSUMPTION half of B10: given the items ``resolve_provision_items``
produced from the spec, override each one's version/integrity from the matching
lock pin BEFORE reconcile, so ``install`` provisions the PINNED version, not the
spec's (possibly floating) one. Matched by ``(type, key)`` — the pin ``key`` and
the item ``identity.key`` are the same ecosystem-natural id (cargo→crate,
python→package, go→module, github_release→repo).

OFFLINE-SAFE (spec §C, the load-bearing invariant): applying a lock touches NO
resolver and NO network. This module imports neither the resolver registry nor
any ``resolve/*`` module — a locked install just copies the locked scalars onto
the items. The per-ecosystem field mapping:

* python / go — the provisioner reads ``item.version``; the locked version lands
  there. The checksum is recorded on ``item.checksum`` for drift visibility; the
  native sumdb / tool integrity enforces the bytes.
* cargo — the provisioner resolves + verifies via the crates index natively, so
  the locked version/checksum are recorded on the item (drift + ``--locked``
  visibility) but do not change the install subprocess.
* github_release — the provisioner builds its download URL from
  ``item.config.tag`` and verifies against ``item.config.checksum``. A
  ``tag: latest`` spec would otherwise hit ``.../download/latest/...`` (not a
  valid GitHub download path), so the locked CONCRETE tag MUST replace it: a new
  frozen :class:`~setforge.config.GitHubReleasePackage` is built with the locked
  tag + checksum and swapped into ``item.config``.
"""

from __future__ import annotations

import dataclasses

from setforge.config import GitHubReleasePackage
from setforge.lockfile import LockFile
from setforge.provision.protocol import ProvisionItem
from setforge.provision.resolve.protocol import PackageType, ResolvedPin


def extension_pins(lock: LockFile | None) -> dict[str, ResolvedPin]:
    """Return the lock's extension pins keyed by casefolded ``publisher.name``.

    Extensions do NOT flow through the dispatch ``ProvisionItem`` path (they go
    through :func:`setforge.vscode_extensions.reconcile`), so
    :func:`apply_lock_to_items` never reaches them. This is the parallel
    consumption hook for the extension reconcile: the key is casefolded to match
    the reconcile's identity keys (VS Code treats extension ids
    case-insensitively). ``None`` / no extension pins yields an empty map, and
    the reconcile then keeps today's marketplace-id install. Offline-safe: no
    resolver import, no network.
    """
    if lock is None:
        return {}
    return {
        pin.key.casefold(): pin
        for pin in lock.packages
        if pin.type is PackageType.EXTENSION
    }


def apply_lock_to_items(
    items: list[ProvisionItem], lock: LockFile
) -> list[ProvisionItem]:
    """Return ``items`` with each lock-matched item overridden from its pin.

    Purely functional: builds a fresh list, never mutates the frozen inputs. An
    item with no matching pin passes through unchanged (partial coverage is fine
    here — ``--locked`` is the separate fail-closed coverage gate). No resolver
    is imported or called, so this is offline-safe.
    """
    by_key: dict[tuple[str, str], ResolvedPin] = {
        (pin.type.value, pin.key): pin for pin in lock.packages
    }
    out: list[ProvisionItem] = []
    for item in items:
        pin = by_key.get((item.type, item.identity.key))
        out.append(item if pin is None else _override(item, pin))
    return out


def _override(item: ProvisionItem, pin: ResolvedPin) -> ProvisionItem:
    """Apply one pin's version/integrity to one item (per-ecosystem mapping).

    github_release rebuilds ``config`` so the locked concrete tag + checksum
    reach the provisioner's URL/verify path; every other ecosystem records the
    locked version + checksum on the item scalars.
    """
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
