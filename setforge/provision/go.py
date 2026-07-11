"""The go :class:`Provisioner` — marker-based, hybrid probe (spec §6).

``go`` has no native "list installed" command, so installs are tracked by
:class:`~setforge.provision.receipt.ReceiptStore` markers. The probe is
HYBRID: an identity counts as installed only if BOTH its receipt is present
AND its GOBIN binary file exists on disk. A manually-deleted binary (receipt
present, file gone) reads as absent and self-heals into a reinstall.

Missing toolchain and any install failure are SOFT, never HARD (go never
gates exit). The receipt is written only after ``go install`` exits 0, and
records the resolved GOBIN binary path so the revert path can unlink it.
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

from setforge.binaries import resolve_binary, stderr_of
from setforge.provision.protocol import (
    Identity,
    Outcome,
    ProvisionDelta,
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
    """Derive the installed binary name from a go module path.

    The last path segment names the binary, EXCEPT a ``cmd/<tool>`` leaf
    (``…/cmd/foo`` → ``foo``) or a trailing ``/vN`` major-version suffix
    (``…/mod/v2`` → ``mod``, stripped before taking the last segment).
    """
    parts = [p for p in module.split("/") if p]
    if len(parts) >= 2 and parts[-2] == "cmd":
        return parts[-1]
    if parts and _is_major_version_suffix(parts[-1]):
        parts = parts[:-1]
    return parts[-1] if parts else module


def _is_major_version_suffix(segment: str) -> bool:
    """Return True for a ``vN`` (N >= 2) semantic major-version path segment."""
    return (
        len(segment) >= 2
        and segment[0] == "v"
        and segment[1:].isdigit()
        and int(segment[1:]) >= 2
    )


@register("go")
class GoProvisioner(Provisioner):
    """Marker-based go provisioner with a hybrid receipt+binary probe."""

    type = "go"

    def __init__(self, *, receipts: ReceiptStore | None = None) -> None:
        """Bind to a :class:`ReceiptStore` (defaults to the real per-host root).

        ``build()`` instantiates provisioners with no args, so the default
        wires the production receipt directory; tests inject a ``tmp_path`` one.
        """
        self._receipts = (
            receipts if receipts is not None else ReceiptStore(default_receipt_root())
        )

    def probe(self) -> set[Identity]:
        """Return identities with BOTH a receipt AND a present GOBIN binary.

        Fails OPEN (empty) when ``go`` is missing. Receipts are read fresh
        from disk each call (no cache); a receipt whose binary was manually
        deleted is treated absent so the next reconcile reinstalls it.
        """
        if self._resolve() is None:
            return set()
        gobin = self._gobin_dir()
        present: set[Identity] = set()
        for identity in self._receipts.installed():
            recorded = self._receipts.path_for(identity)
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

    def plan(
        self, items: Sequence[ProvisionItem], installed: set[Identity]
    ) -> ProvisionDelta:
        return ProvisionDelta(
            installed=tuple(
                item.identity for item in items if item.identity not in installed
            )
        )

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
        if item.identity in self.probe():
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
            item.identity, version=item.version, checksum=item.checksum, path=binary
        )
        return ProvisionOutcome(item=item, outcome=Outcome.OK, detail="installed")

    def uninstall_one(self, identity: Identity) -> None:
        """Revert one install: drop the receipt and unlink the binary.

        Best-effort on the binary (a missing or unremovable file warns and is
        tolerated); the receipt removal is idempotent.
        """
        recorded = self._receipts.path_for(identity)
        binary = (
            recorded
            if recorded is not None
            else self._gobin_dir() / _binary_name(identity.key)
        )
        try:
            binary.unlink(missing_ok=True)
        except OSError as exc:
            LOGGER.warning("could not unlink go binary %s: %s", binary, exc)
        self._receipts.remove(identity)

    def _gobin_dir(self) -> Path:
        """Resolve the install directory go drops binaries into.

        ``go env GOBIN`` if non-empty, else the FIRST ``go env GOPATH`` entry
        (``os.pathsep``-split) + ``/bin``, else ``~/go/bin``.
        """
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
        """Return ``go env <var>`` stripped, or "" if go is missing / errors."""
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
