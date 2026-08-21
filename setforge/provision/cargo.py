"""The Cargo provisioner with exact lock-pin and registry-source enforcement.

Toolchain and build failures remain SOFT; malformed lock pins are HARD.
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass

from setforge.binaries import resolve_binary, stderr_of
from setforge.errors import ResolveError
from setforge.provision.protocol import (
    Identity,
    ObservationOrigin,
    Outcome,
    PackageObservation,
    ProvisionDelta,
    Provisioner,
    ProvisionItem,
    ProvisionOutcome,
)
from setforge.provision.registry import register
from setforge.provision.resolve.cargo import registry_checksum

__all__ = ["CargoProvisioner"]

LOGGER: logging.Logger = logging.getLogger(__name__)

_CARGO_BIN_NAME = "cargo"
_LIST_TIMEOUT_S = 30
_INSTALL_TIMEOUT_S = 1800
_UNINSTALL_TIMEOUT_S = 60

_LIST_HEADER_RE = re.compile(
    r"^(?P<name>\S+) v(?P<version>\S+?)(?: \((?P<source>.+)\))?:$"
)
_EXACT_VERSION_RE = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_CHECKSUM_RE = re.compile(r"^sha256:[0-9a-fA-F]{64}$")


@dataclass(frozen=True, order=True, slots=True)
class _InstalledCrate:
    """One top-level record from ``cargo install --list``."""

    name: str
    version: str
    source: str | None = None


@register("cargo")
class CargoProvisioner(Provisioner):
    type = "cargo"

    def __init__(self) -> None:
        self._inventory: tuple[_InstalledCrate, ...] | None = None
        self._registry_checksums: dict[tuple[str, str], str | None] = {}

    def probe(self) -> set[Identity]:
        """Return the crates ``cargo install --list`` reports (fails OPEN)."""
        cargo = self._resolve()
        if cargo is None:
            self._inventory = ()
            return set()
        self._inventory = self._probe_inventory(cargo)
        if self._inventory is None:
            return set()
        return {
            Identity(key=_crate_key(record.name), display=record.name)
            for record in self._inventory
        }

    def _probe_inventory(self, cargo: str) -> tuple[_InstalledCrate, ...] | None:
        """Read complete Cargo records; malformed output invalidates the probe."""
        try:
            result = subprocess.run(
                [cargo, "install", "--list"],
                check=True,
                text=True,
                capture_output=True,
                timeout=_LIST_TIMEOUT_S,
            )
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError,
        ) as exc:
            LOGGER.warning("`cargo install --list` failed: %s", stderr_of(exc))
            return None
        inventory = _parse_crates(result.stdout)
        if inventory is None:
            LOGGER.warning("`cargo install --list` returned malformed inventory")
        return inventory

    def inventory_fingerprint(
        self, installed: set[Identity]
    ) -> tuple[bool, tuple[_InstalledCrate, ...]]:
        """Include versions and provenance in frozen-plan revalidation."""
        del installed
        inventory = self._inventory
        return inventory is not None, inventory or ()

    def observations(self, installed: set[Identity]) -> tuple[PackageObservation, ...]:
        del installed
        return tuple(
            PackageObservation(
                Identity(key=_crate_key(record.name), display=record.name),
                ObservationOrigin.EXTERNAL,
                version=record.version,
                source=record.source or "crates.io",
            )
            for record in self._inventory or ()
        )

    def plan_fingerprint(
        self, items: Sequence[ProvisionItem], installed: set[Identity]
    ) -> tuple[
        tuple[bool, tuple[_InstalledCrate, ...]],
        tuple[tuple[str, str, str | None], ...],
    ]:
        """Freeze installed metadata and exact registry checksum rows."""
        checksums: list[tuple[str, str, str | None]] = []
        self._registry_checksums = {}
        for item in items:
            if not _valid_pin(item):
                continue
            assert item.version is not None
            coordinate = (_crate_key(item.identity.key), item.version)
            actual = self._lookup_registry_checksum(*coordinate)
            self._registry_checksums[coordinate] = actual
            checksums.append((*coordinate, actual))
        return self.inventory_fingerprint(installed), tuple(sorted(checksums))

    @staticmethod
    def _lookup_registry_checksum(crate: str, version: str) -> str | None:
        try:
            return registry_checksum(crate, version)
        except ResolveError as exc:
            LOGGER.warning(
                "could not verify Cargo registry checksum for %s %s: %s",
                crate,
                version,
                exc,
            )
            return None

    def plan(
        self, items: Sequence[ProvisionItem], installed: set[Identity]
    ) -> ProvisionDelta:
        return ProvisionDelta(
            installed=tuple(
                item.identity
                for item in items
                if not self._item_is_satisfied(item, installed)
            )
        )

    def _item_is_satisfied(self, item: ProvisionItem, installed: set[Identity]) -> bool:
        if item.version is None and item.checksum is None:
            expected = _crate_key(item.identity.key)
            return any(_crate_key(identity.key) == expected for identity in installed)
        if not _valid_pin(item):
            return False
        assert item.version is not None
        coordinate = (_crate_key(item.identity.key), item.version)
        if coordinate not in self._registry_checksums:
            self._registry_checksums[coordinate] = self._lookup_registry_checksum(
                *coordinate
            )
        if self._registry_checksums[coordinate] != (item.checksum or "").lower():
            return False
        return any(
            _crate_key(record.name) == _crate_key(item.identity.key)
            and record.version == item.version
            and record.source is None
            for record in self._inventory or ()
        )

    def apply_one(self, item: ProvisionItem) -> ProvisionOutcome:
        crate = item.identity.display
        pinned = item.version is not None or item.checksum is not None
        if pinned and not _valid_pin(item):
            return ProvisionOutcome(
                item=item,
                outcome=Outcome.HARD,
                detail="cargo lock pin requires an exact version and sha256 checksum",
            )
        if pinned:
            assert item.version is not None
            try:
                actual_checksum = registry_checksum(item.identity.key, item.version)
            except ResolveError as exc:
                return ProvisionOutcome(
                    item=item,
                    outcome=Outcome.HARD,
                    detail=f"could not verify Cargo registry checksum: {exc}",
                )
            if actual_checksum != (item.checksum or "").lower():
                return ProvisionOutcome(
                    item=item,
                    outcome=Outcome.HARD,
                    detail=(
                        f"Cargo registry checksum mismatch for {crate} "
                        f"{item.version}: expected {item.checksum}, got "
                        f"{actual_checksum}"
                    ),
                )
            self._registry_checksums[(_crate_key(item.identity.key), item.version)] = (
                actual_checksum
            )
        cargo = self._resolve()
        if cargo is None:
            return ProvisionOutcome(
                item=item,
                outcome=Outcome.SOFT,
                detail=(
                    "cargo not found on PATH; install the Rust toolchain via "
                    "https://rustup.rs to enable this crate"
                ),
            )
        self._inventory = self._probe_inventory(cargo)
        installed = {
            Identity(key=_crate_key(record.name), display=record.name)
            for record in self._inventory or ()
        }
        if self._item_is_satisfied(item, installed):
            return ProvisionOutcome(item=item, outcome=Outcome.SKIP, detail="present")
        argv = [cargo, "install"]
        if pinned:
            assert item.version is not None
            argv.extend(["--version", item.version, "--locked"])
            if any(
                _crate_key(record.name) == _crate_key(item.identity.key)
                for record in self._inventory or ()
            ):
                argv.append("--force")
        argv.extend(["--", crate])
        try:
            subprocess.run(
                argv,
                check=True,
                text=True,
                capture_output=True,
                timeout=_INSTALL_TIMEOUT_S,
            )
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError,
        ) as exc:
            msg = stderr_of(exc)
            LOGGER.warning("cargo install failed for %s: %s", crate, msg)
            return ProvisionOutcome(item=item, outcome=Outcome.SOFT, detail=msg)
        return ProvisionOutcome(item=item, outcome=Outcome.OK, detail="installed")

    def uninstall_one(self, identity: Identity) -> None:
        cargo = self._resolve()
        if cargo is None:
            return
        try:
            subprocess.run(
                [cargo, "uninstall", "--", identity.display],
                check=True,
                text=True,
                capture_output=True,
                timeout=_UNINSTALL_TIMEOUT_S,
            )
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError,
        ) as exc:
            LOGGER.warning(
                "cargo uninstall failed for %s: %s", identity.display, stderr_of(exc)
            )

    @staticmethod
    def _resolve() -> str | None:
        resolved = resolve_binary(_CARGO_BIN_NAME)
        return None if resolved is None else str(resolved)


def _parse_crates(stdout: str) -> tuple[_InstalledCrate, ...] | None:
    crates: dict[str, _InstalledCrate] = {}
    for line in stdout.splitlines():
        if not line or line[0].isspace():
            continue
        match = _LIST_HEADER_RE.fullmatch(line)
        if match is None:
            return None
        record = _InstalledCrate(
            name=match["name"],
            version=match["version"],
            source=match["source"],
        )
        key = _crate_key(record.name)
        previous = crates.get(key)
        if previous is not None and previous != record:
            return None
        crates[key] = record
    return tuple(sorted(crates.values()))


def _crate_key(name: str) -> str:
    """Return Cargo's comparison/cache identity while preserving display spelling."""
    return name.casefold()


def _valid_pin(item: ProvisionItem) -> bool:
    return bool(
        item.version is not None
        and _EXACT_VERSION_RE.fullmatch(item.version)
        and item.checksum is not None
        and _CHECKSUM_RE.fullmatch(item.checksum)
    )
