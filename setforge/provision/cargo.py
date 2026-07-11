"""The cargo :class:`Provisioner`. Toolchain/build failures are SOFT, never HARD."""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Sequence

from setforge.binaries import resolve_binary, stderr_of
from setforge.provision.protocol import (
    Identity,
    Outcome,
    ProvisionDelta,
    Provisioner,
    ProvisionItem,
    ProvisionOutcome,
)
from setforge.provision.registry import register

__all__ = ["CargoProvisioner"]

LOGGER: logging.Logger = logging.getLogger(__name__)

_CARGO_BIN_NAME = "cargo"
_LIST_TIMEOUT_S = 30
_INSTALL_TIMEOUT_S = 1800
_UNINSTALL_TIMEOUT_S = 60


@register("cargo")
class CargoProvisioner(Provisioner):
    type = "cargo"

    def probe(self) -> set[Identity]:
        """Return the crates ``cargo install --list`` reports (fails OPEN)."""
        cargo = self._resolve()
        if cargo is None:
            return set()
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
            return set()
        return {
            Identity(key=name, display=name) for name in _parse_crates(result.stdout)
        }

    def plan(
        self, items: Sequence[ProvisionItem], installed: set[Identity]
    ) -> ProvisionDelta:
        return ProvisionDelta(
            installed=tuple(
                item.identity for item in items if item.identity not in installed
            )
        )

    def apply_one(self, item: ProvisionItem) -> ProvisionOutcome:
        cargo = self._resolve()
        crate = item.identity.display
        if cargo is None:
            return ProvisionOutcome(
                item=item,
                outcome=Outcome.SOFT,
                detail=(
                    "cargo not found on PATH; install the Rust toolchain via "
                    "https://rustup.rs to enable this crate"
                ),
            )
        if item.identity in self.probe():
            return ProvisionOutcome(item=item, outcome=Outcome.SKIP, detail="present")
        try:
            subprocess.run(
                [cargo, "install", "--", crate],
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


def _parse_crates(stdout: str) -> set[str]:
    crates: set[str] = set()
    for line in stdout.splitlines():
        if not line or line[0].isspace():
            continue
        name = line.split(" ", 1)[0]
        if name:
            crates.add(name)
    return crates
