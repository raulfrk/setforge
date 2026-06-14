"""Cargo-binary installation, driven by the ``cargo`` CLI.

setforge tracks cargo-installed binaries (e.g. ``ast-grep``) so a fresh
host can reproduce them: a profile's ``cargo_binaries:`` list names crates
that :func:`install_cargo_binaries` installs during ``setforge install``.

Two deliberate softnesses distinguish this from the plugin / MCP paths:

- **Missing toolchain is soft.** ``cargo`` is resolved via
  :func:`setforge.binaries.resolve_binary`; when no layer finds it, the
  function emits ONE yellow warning and returns — install continues and
  exits 0. There is no rust toolchain on every host and a binary the user
  cannot build is not a hard failure (mirrors the gitleaks warn-and-skip
  in :mod:`setforge.secrets`).
- **Skip-if-present.** ``cargo install <crate>`` recompiles even when the
  crate is already current, so each crate is checked against the
  ``cargo install --list`` set BEFORE invoking install. Already-installed
  crates are skipped.

There is no revert tracking for cargo binaries — they are not cleanly
reversible (uninstalling a tool the host may now depend on elsewhere is
worse than leaving it), so the install path records nothing for revert.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Iterable

import typer

from setforge.binaries import resolve_binary, stderr_of

__all__ = ["install_cargo_binaries"]

LOGGER: logging.Logger = logging.getLogger(__name__)

_CARGO_BIN_NAME = "cargo"
_LIST_TIMEOUT_S = 30
"""Timeout for the cheap ``cargo install --list`` probe (seconds)."""
_INSTALL_TIMEOUT_S = 1800
"""Timeout for a single ``cargo install <crate>`` (30 min — compiles are slow)."""


def _warn(message: str) -> None:
    """Emit a yellow warning to stderr (soft-requirement convention)."""
    typer.secho(message, err=True, fg=typer.colors.YELLOW)


def _installed_crates(cargo: str) -> set[str]:
    """Return the set of crate names ``cargo install --list`` reports.

    The output lists each installed crate as ``<name> v<version>:`` on its
    own (unindented) line, with its binaries indented below. Returns an
    empty set when the probe fails or times out — a failed probe degrades
    to "assume nothing installed", so a crate gets a fresh
    (idempotent-anyway) install attempt rather than being silently
    skipped.
    """
    try:
        result = subprocess.run(
            [cargo, "install", "--list"],
            check=True,
            text=True,
            capture_output=True,
            timeout=_LIST_TIMEOUT_S,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        # OSError covers a which()-resolved cargo that fails to exec
        # (removed/replaced in the TOCTOU window, broken wrapper): degrade
        # to "assume nothing installed" rather than crashing install.
        LOGGER.warning("`cargo install --list` failed: %s", stderr_of(exc))
        return set()
    crates: set[str] = set()
    for line in result.stdout.splitlines():
        # Crate header lines are unindented and end with ``:`` after a
        # ``vX.Y.Z`` version token; binary lines are indented with spaces.
        if not line or line[0].isspace():
            continue
        name = line.split(" ", 1)[0]
        if name:
            crates.add(name)
    return crates


def install_cargo_binaries(crates: Iterable[str]) -> list[tuple[str, str]]:
    """Install each crate in ``crates`` via ``cargo install`` (skip-if-present).

    Returns a list of ``(crate, error)`` failures. A missing ``cargo``
    toolchain is NOT a failure: one yellow warning is emitted and an empty
    list returned (install continues, exits 0). Crates already reported by
    ``cargo install --list`` are skipped without invoking ``cargo install``
    (which would needlessly recompile). Per-crate subprocess errors are
    caught and recorded so one bad crate does not abort the loop; the
    caller decides whether to surface them.
    """
    wanted = [c for c in crates if c.strip()]
    if not wanted:
        return []

    resolved = resolve_binary(_CARGO_BIN_NAME)
    if resolved is None:
        _warn(
            "warning: skipping cargo binaries — cargo not found on PATH; "
            "install the Rust toolchain via https://rustup.rs to enable "
            f"({', '.join(wanted)})"
        )
        return []
    cargo = str(resolved)

    already = _installed_crates(cargo)
    failed: list[tuple[str, str]] = []
    for crate in wanted:
        if crate in already:
            typer.echo(f"cargo: {crate} already installed (skip)")
            continue
        typer.echo(f"cargo install {crate}")
        try:
            subprocess.run(
                # ``--`` terminates option parsing so a crate name from
                # user YAML that begins with ``-`` is treated as a positional
                # crate, never a cargo flag (cargo documents
                # ``cargo install [OPTIONS] [--] [crate]...``). Mirrors the
                # ``claude mcp add`` separator guard in setforge.mcp_servers.
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
            # (TOCTOU removal/replacement, broken wrapper): record the
            # per-crate failure rather than aborting the whole install.
            msg = stderr_of(exc)
            LOGGER.warning("cargo install failed for %s: %s", crate, msg)
            _warn(f"warning: cargo install {crate} failed — {msg}")
            failed.append((crate, msg))
    return failed
