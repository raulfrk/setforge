"""The python (``uv tool``) :class:`Provisioner`.

Missing uv and install failures are SOFT, never HARD.
"""

from __future__ import annotations

import logging
import re
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

__all__ = ["PythonProvisioner"]

LOGGER: logging.Logger = logging.getLogger(__name__)

_UV_BIN_NAME = "uv"
_LIST_TIMEOUT_S = 30
_INSTALL_TIMEOUT_S = 600
_UNINSTALL_TIMEOUT_S = 60

# `uv tool list` colors the package name with ANSI (bold); strip it before
# matching so the escape bytes don't leak into the parsed name.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# A package line names the tool at line-start followed by whitespace (the
# version column): `ruff v0.5.0`. Anchoring on `^name\s+` keeps `foo` from
# prefix-matching `foobar`. Entry-point lines start with `- ` and are skipped.
_PACKAGE_LINE = re.compile(r"^(\S+)\s")


@register("python")
class PythonProvisioner(Provisioner):
    type = "python"

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
        return {
            Identity(key=name, display=name) for name in _parse_tools(result.stdout)
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
        # SKIP by NAME before any subprocess. A version pin is REPORT-only this
        # wave, so a mismatch on an already-present tool is NOT a reinstall.
        if item.identity in self.probe():
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


def _parse_tools(stdout: str) -> set[str]:
    tools: set[str] = set()
    for raw in stdout.splitlines():
        line = _ANSI.sub("", raw)
        # Entry-point lines start with "- "; blank/indented lines carry no name.
        if not line or line[0].isspace() or line.startswith("- "):
            continue
        match = _PACKAGE_LINE.match(line)
        if match is not None:
            tools.add(match.group(1))
    return tools
