"""The python (``uv tool``) :class:`Provisioner`. Failures are SOFT, never HARD."""

from __future__ import annotations

import logging
import re
import subprocess

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
from setforge.provision.registry import register

__all__ = ["PythonProvisioner"]

LOGGER: logging.Logger = logging.getLogger(__name__)

_UV_BIN_NAME = "uv"
_LIST_TIMEOUT_S = 30
_INSTALL_TIMEOUT_S = 600
_UNINSTALL_TIMEOUT_S = 60

# `uv tool list` colors the package name with ANSI (bold); strip before matching.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# Anchored so `foo` never prefix-matches an installed `foobar`.
_PACKAGE_LINE = re.compile(r"^(\S+)\s")


@register("python")
class PythonProvisioner(Provisioner):
    type = "python"

    def __init__(self) -> None:
        self._versions: dict[str, str | None] = {}

    def probe(self) -> set[Identity]:
        """Return the tools ``uv tool list`` reports (fails OPEN)."""
        uv = self._resolve()
        if uv is None:
            return set()
        try:
            result = subprocess.run(
                [uv, "tool", "list"],
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
            LOGGER.warning("`uv tool list` failed: %s", stderr_of(exc))
            return set()
        self._versions = _parse_tools(result.stdout)
        return {Identity(key=name, display=name) for name in self._versions}

    def observations(self, installed: set[Identity]) -> tuple[PackageObservation, ...]:
        return tuple(
            PackageObservation(
                identity,
                ObservationOrigin.EXTERNAL,
                version=self._versions.get(identity.key),
                source="uv-tool",
            )
            for identity in sorted(installed, key=lambda value: value.key)
        )

    def apply_one(self, item: ProvisionItem) -> ProvisionOutcome:
        uv = self._resolve()
        if uv is None:
            return ProvisionOutcome(
                item=item,
                outcome=Outcome.SOFT,
                detail=(
                    "uv not found on PATH; install uv via "
                    "https://docs.astral.sh/uv/ to enable this package"
                ),
            )
        # Version pin is REPORT-only: present-by-name skips even on a mismatch.
        installed = self.probe()
        if item.identity in installed and (
            item.version is None
            or self._versions.get(item.identity.key) == item.version
        ):
            return ProvisionOutcome(item=item, outcome=Outcome.SKIP, detail="present")
        package = item.identity.display
        if item.version is not None:
            package = f"{package}=={item.version}"
        try:
            subprocess.run(
                [uv, "tool", "install", "--", package],
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
            LOGGER.warning("uv tool install failed for %s: %s", package, msg)
            return ProvisionOutcome(item=item, outcome=Outcome.SOFT, detail=msg)
        return ProvisionOutcome(item=item, outcome=Outcome.OK, detail="installed")

    def uninstall_one(self, identity: Identity) -> None:
        uv = self._resolve()
        if uv is None:
            return
        try:
            subprocess.run(
                [uv, "tool", "uninstall", "--", identity.display],
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
                "uv tool uninstall failed for %s: %s",
                identity.display,
                stderr_of(exc),
            )

    @staticmethod
    def _resolve() -> str | None:
        resolved = resolve_binary(_UV_BIN_NAME)
        return None if resolved is None else str(resolved)


def _parse_tools(stdout: str) -> dict[str, str | None]:
    if stdout.strip() == "No tools installed":
        return {}
    tools: dict[str, str | None] = {}
    for raw in stdout.splitlines():
        line = _ANSI.sub("", raw)
        if not line or line[0].isspace() or line.startswith("- "):
            continue
        match = _PACKAGE_LINE.match(line)
        if match is not None:
            fields = line.split()
            version = fields[1].removeprefix("v") if len(fields) > 1 else None
            tools[match.group(1)] = version
    return tools
