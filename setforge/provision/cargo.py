"""The cargo :class:`Provisioner` — install crates via the ``cargo`` CLI.

This is the PATTERN-DEFINING ecosystem provisioner: the go / python /
github_release provisioners copy its shape. It refactors the imperative
:mod:`setforge.cargo` logic onto the uniform protocol without changing any
behavior.

cargo is a LIST-based provisioner, not marker-based: ``cargo install --list``
is the authoritative record of what is installed, so :meth:`probe` reads it
live and no receipt is written (a receipt would only duplicate — and risk
disagreeing with — cargo's own list).

Two deliberate softnesses carry over from the imperative path:

- **Missing toolchain is SOFT, never HARD.** ``cargo`` is resolved via
  :func:`setforge.binaries.resolve_binary`; when no host has a Rust
  toolchain, :meth:`apply_one` returns :attr:`Outcome.SOFT` (warn-and-skip)
  rather than gating the install exit. A binary the user cannot build is not
  a hard failure.
- **A build failure is SOFT and RECORDED.** ``cargo install`` compiling and
  failing returns :attr:`Outcome.SOFT` with the captured stderr tail as
  ``detail`` — surfaced to the user, never discarded, never escalated to
  HARD. One bad crate does not gate the whole reconcile.

:meth:`probe` fails OPEN: a missing toolchain or a failed ``--list`` degrades
to "assume nothing installed", so a crate gets a fresh (idempotent-anyway)
install attempt rather than being silently skipped.
"""

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
"""Timeout for the cheap ``cargo install --list`` probe (seconds)."""
_INSTALL_TIMEOUT_S = 1800
"""Timeout for a single ``cargo install <crate>`` (30 min — compiles are slow)."""
_UNINSTALL_TIMEOUT_S = 60
"""Timeout for ``cargo uninstall <crate>`` (a metadata + file removal, fast)."""


@register("cargo")
class CargoProvisioner(Provisioner):
    """Install cargo crates on the uniform provisioner protocol.

    The crate name is carried as ``item.identity`` (``key == display ==``
    crate); the install/uninstall subprocess is invoked with that name after
    a literal ``--`` end-of-options separator so a leading-dash crate name
    can never be parsed as a cargo flag.
    """

    type = "cargo"

    def probe(self) -> set[Identity]:
        """Return the crates ``cargo install --list`` reports (fail-OPEN).

        Resolves ``cargo`` and parses its ``--list`` output. FAIL-OPEN: a
        missing toolchain OR a failed/timed-out probe returns an empty set —
        "assume nothing installed" — so a crate gets a fresh, idempotent
        install attempt rather than being silently skipped. Never writes; no
        receipt (cargo's native list is the source of truth).
        """
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
            # OSError covers a which()-resolved cargo that fails to exec
            # (removed/replaced in the TOCTOU window, broken wrapper): degrade
            # to "assume nothing installed" rather than crashing the probe.
            LOGGER.warning("`cargo install --list` failed: %s", stderr_of(exc))
            return set()
        return {
            Identity(key=name, display=name) for name in _parse_crates(result.stdout)
        }

    def plan(
        self, items: Sequence[ProvisionItem], installed: set[Identity]
    ) -> ProvisionDelta:
        """Return the declared identities not in ``installed``. PURE — no writes."""
        return ProvisionDelta(
            installed=tuple(
                item.identity for item in items if item.identity not in installed
            )
        )

    def apply_one(self, item: ProvisionItem) -> ProvisionOutcome:
        """Install one crate via ``cargo install`` (skip-if-present).

        Missing ``cargo`` → :attr:`Outcome.SOFT` (warn-and-skip, NEVER HARD).
        Already present → :attr:`Outcome.SKIP` (checked BEFORE any subprocess,
        so a current crate is never needlessly recompiled). Otherwise
        ``cargo install -- <crate>``: success → :attr:`Outcome.OK`; a build
        failure / timeout / exec error → :attr:`Outcome.SOFT` with the
        captured stderr tail as ``detail`` (RECORDED, never HARD, never
        discarded, never ``shell=True``).
        """
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
                # ``--`` terminates option parsing so a crate name from user
                # YAML that begins with ``-`` is treated as a positional crate,
                # never a cargo flag (``cargo install [OPTIONS] [--] [crate]...``).
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
            # OSError covers a which()-resolved cargo that fails to exec
            # (TOCTOU removal/replacement, broken wrapper). A build failure is
            # SOFT and surfaced — one bad crate never gates the reconcile exit.
            msg = stderr_of(exc)
            LOGGER.warning("cargo install failed for %s: %s", crate, msg)
            return ProvisionOutcome(item=item, outcome=Outcome.SOFT, detail=msg)
        return ProvisionOutcome(item=item, outcome=Outcome.OK, detail="installed")

    def uninstall_one(self, identity: Identity) -> None:
        """Undo one install via ``cargo uninstall`` (the future-cleanup path).

        Missing ``cargo`` is a no-op (nothing to remove). ``cargo uninstall``
        of an absent crate exits non-zero; that — and any timeout / exec
        error — is tolerated so a partial or already-removed state does not
        abort the cleanup loop.
        """
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
        """Resolve the ``cargo`` binary, or ``None`` when no layer finds it."""
        resolved = resolve_binary(_CARGO_BIN_NAME)
        return None if resolved is None else str(resolved)


def _parse_crates(stdout: str) -> set[str]:
    """Extract crate names from ``cargo install --list`` output.

    Each installed crate is a ``<name> v<version>:`` header on its own
    (unindented) line, with its binaries indented below. Header lines are
    unindented; binary lines start with whitespace and are skipped. The crate
    name is the first whitespace-delimited token of a header line.
    """
    crates: set[str] = set()
    for line in stdout.splitlines():
        if not line or line[0].isspace():
            continue
        name = line.split(" ", 1)[0]
        if name:
            crates.add(name)
    return crates
