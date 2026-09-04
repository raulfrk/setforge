"""The go :class:`Provisioner`: hybrid receipt+GOBIN-binary probe, self-heals."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from setforge.binaries import resolve_binary, stderr_of
from setforge.provision.protocol import (
    Identity,
    ObservationOrigin,
    Outcome,
    PackageObservation,
    Provisioner,
    ProvisionItem,
    ProvisionOutcome,
)
from setforge.provision.receipt import ReceiptStore, default_receipt_root
from setforge.provision.registry import register

__all__ = ["GoProvisioner"]

LOGGER: logging.Logger = logging.getLogger(__name__)

_GO_BIN_NAME = "go"
_ENV_TIMEOUT_S = 30
_INSTALL_TIMEOUT_S = 1800
_LATEST = "latest"


def _binary_name(module: str) -> str:
    parts = [p for p in module.split("/") if p]
    if len(parts) >= 2 and parts[-2] == "cmd":
        return parts[-1]
    if parts and _is_major_version_suffix(parts[-1]):
        parts = parts[:-1]
    return parts[-1] if parts else module


def _is_major_version_suffix(segment: str) -> bool:
    return (
        len(segment) >= 2
        and segment[0] == "v"
        and segment[1:].isdigit()
        and int(segment[1:]) >= 2
    )


@register("go")
class GoProvisioner(Provisioner):
    type = "go"

    def __init__(self, *, receipts: ReceiptStore | None = None) -> None:
        self._receipts = (
            receipts if receipts is not None else ReceiptStore(default_receipt_root())
        )

    def probe(self) -> set[Identity]:
        # Fails OPEN (empty) when go is missing — never assume-installed.
        if self._resolve() is None:
            return set()
        gobin = self._gobin_dir()
        present: set[Identity] = set()
        for identity in self._receipts.installed_for(self.type):
            recorded = self._receipts.path_for(identity, provider=self.type)
            binary = (
                recorded if recorded is not None else gobin / _binary_name(identity.key)
            )
            try:
                exists = binary.is_file()
            except OSError:
                exists = False
            if exists:
                present.add(identity)
        return present

    def observations(self, installed: set[Identity]) -> tuple[PackageObservation, ...]:
        result: list[PackageObservation] = []
        for identity in sorted(installed, key=lambda value: value.key):
            entry = self._receipts.entry_for(identity, self.type)
            if entry is None:
                continue
            result.append(
                PackageObservation(
                    identity,
                    ObservationOrigin.CURRENT_RECEIPT
                    if entry.provider is not None
                    else ObservationOrigin.LEGACY_RECEIPT,
                    version=entry.version,
                    locator=str(entry.path) if entry.path is not None else None,
                    checksum=entry.checksum,
                )
            )
        return tuple(result)

    def apply_one(self, item: ProvisionItem) -> ProvisionOutcome:
        go = self._resolve()
        module = item.identity.display
        if go is None:
            return ProvisionOutcome(
                item=item,
                outcome=Outcome.SOFT,
                detail=(
                    "go not found on PATH; install the Go toolchain from "
                    "https://go.dev/dl/ to enable this module"
                ),
            )
        entry = self._receipts.entry_for(item.identity, self.type)
        if (
            item.identity in self.probe()
            and entry is not None
            and (item.version is None or entry.version == item.version)
            and (item.checksum is None or entry.checksum == item.checksum)
        ):
            return ProvisionOutcome(item=item, outcome=Outcome.SKIP, detail="present")
        spec = f"{module}@{item.version}" if item.version else f"{module}@{_LATEST}"
        try:
            subprocess.run(
                [go, "install", "--", spec],
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
            LOGGER.warning("go install failed for %s: %s", spec, msg)
            return ProvisionOutcome(item=item, outcome=Outcome.SOFT, detail=msg)
        # Receipt is the LAST step — written only after exit-0.
        binary = self._gobin_dir() / _binary_name(item.identity.key)
        self._receipts.record(
            item.identity,
            version=item.version,
            checksum=item.checksum,
            path=binary,
            provider=self.type,
        )
        return ProvisionOutcome(item=item, outcome=Outcome.OK, detail="installed")

    def uninstall_one(self, identity: Identity) -> None:
        recorded = self._receipts.path_for(identity, provider=self.type)
        binary = (
            recorded
            if recorded is not None
            else self._gobin_dir() / _binary_name(identity.key)
        )
        try:
            binary.unlink(missing_ok=True)
        except OSError as exc:
            LOGGER.warning("could not unlink go binary %s: %s", binary, exc)
        self._receipts.remove(identity, provider=self.type)

    def _gobin_dir(self) -> Path:
        # Precedence: GOBIN, else first GOPATH entry + /bin, else ~/go/bin.
        gobin = self._go_env("GOBIN")
        if gobin:
            return Path(gobin)
        gopath = self._go_env("GOPATH")
        if gopath:
            first = gopath.split(os.pathsep, 1)[0]
            if first:
                return Path(first) / "bin"
        return Path.home() / "go" / "bin"

    def _go_env(self, var: str) -> str:
        go = self._resolve()
        if go is None:
            return ""
        try:
            result = subprocess.run(
                [go, "env", var],
                check=True,
                text=True,
                capture_output=True,
                timeout=_ENV_TIMEOUT_S,
            )
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError,
        ) as exc:
            LOGGER.warning("`go env %s` failed: %s", var, stderr_of(exc))
            return ""
        return result.stdout.strip()

    @staticmethod
    def _resolve() -> str | None:
        resolved = resolve_binary(_GO_BIN_NAME)
        return None if resolved is None else str(resolved)
