"""MCP-server reconcile + reverse helpers shared by install / revert.

Sibling to :mod:`setforge.cli._plugin_helpers`. No ``app`` import and no
``@app.command()`` registrations; the helpers drive ``claude mcp``
subprocesses and write progress / warnings via ``typer.secho``. The
install side runs :func:`reconcile_mcp_servers` (a thin wrapper over
:func:`setforge.mcp_servers.reconcile` that maps its report into an
:class:`~setforge.transitions.MCPDelta`); the revert side runs
:func:`_reverse_mcp`, the inverse of an ``mcp.json`` delta.
"""

from __future__ import annotations

import contextlib
import subprocess

import typer

from setforge import mcp_servers as mcp_mod
from setforge import transitions
from setforge.config import Config, McpServerRef, ResolvedProfile
from setforge.errors import PluginToolMissing


def _warn_skip_mcp(exc: PluginToolMissing) -> None:
    """Yellow stderr warning when the ``claude`` binary is absent."""
    typer.secho(
        f"warning: skipping MCP server reconcile — {exc}",
        err=True,
        fg=typer.colors.YELLOW,
    )


def reconcile_mcp_servers(
    cfg: Config,
    resolved: ResolvedProfile,
) -> tuple[transitions.MCPDelta | None, list[tuple[str, str]]]:
    """Converge declared MCP servers; return ``(delta, failed)``.

    Returns ``(None, [])`` when the ``claude`` binary is missing
    (warn-and-skip; nothing landed). Otherwise returns the
    :class:`~transitions.MCPDelta` of what was registered / updated (built
    from successfully-applied ops only) plus the report's ``failed`` list
    so the caller can gate the exit code on it.
    """
    try:
        report = mcp_mod.reconcile(cfg, resolved)
    except PluginToolMissing as exc:
        _warn_skip_mcp(exc)
        return None, []

    for name, _command, _scope in report.added:
        typer.echo(f"mcp added     {name}")
    for name, _prior_command, _prior_scope in report.updated:
        typer.echo(f"mcp updated   {name}")
    for name, err in report.failed:
        typer.secho(f"FAILED mcp  {name} — {err}", err=True, fg=typer.colors.YELLOW)
    if not report and not report.failed:
        typer.echo("mcp servers: nothing to reconcile")

    delta = transitions.MCPDelta(
        added=tuple(
            (name, tuple(command), scope) for name, command, scope in report.added
        ),
        updated=tuple(
            (name, tuple(prior_command), prior_scope)
            for name, prior_command, prior_scope in report.updated
        ),
    )
    return delta, report.failed


def _reverse_mcp(
    delta: transitions.MCPDelta,
) -> tuple[transitions.MCPDelta | None, list[tuple[str, str]]]:
    """Apply the inverse of an ``mcp.json`` delta.

    Returns ``(reverse_delta, failed)``. The reverse delta reflects ONLY
    the inverse ops that succeeded (mirrors :func:`_reverse_plugins` /
    :func:`_reverse_extensions` — a revert-of-revert never re-applies a
    no-op) and is itself a closed MCPDelta so a redo (a second ``revert``,
    which reverts THIS reverse transition) round-trips.

    The two delta fields have ASYMMETRIC inverses, so the reverse delta
    cross-maps them to keep the redo correct:

    - a forward ``added`` ``(name, command, scope)`` is reversed by
      ``claude mcp remove name``. Its own inverse — what the redo must do
      — is to re-add that exact registration; the reverse delta therefore
      records it under ``updated`` (the field whose inverse is
      "re-add the stored command"), NOT ``added``.
    - a forward ``updated`` ``(name, prior_command, prior_scope)`` is
      reversed by re-adding the prior command. Its inverse on a redo is to
      remove it again; the reverse delta records it under ``added`` (the
      field whose inverse is "remove").

    Per-item failures warn-and-continue so the reverse transition is still
    written; :class:`PluginToolMissing` is a skip.
    """
    # reverse_to_readd: servers the redo must RE-ADD → stored under the
    # reverse delta's ``updated`` field (inverse = re-add stored command).
    reverse_to_readd: list[tuple[str, tuple[str, ...], str]] = []
    # reverse_to_remove: servers the redo must REMOVE → stored under the
    # reverse delta's ``added`` field (inverse = remove).
    reverse_to_remove: list[tuple[str, tuple[str, ...], str]] = []
    failed: list[tuple[str, str]] = []

    # ``added`` servers were registered this command → remove them. The
    # redo must re-add them, so record under reverse ``updated``.
    for name, command, scope in delta.added:
        if _try_remove(name, scope, failed):
            reverse_to_readd.append((name, tuple(command), scope))

    # ``updated`` servers had their prior command stashed → re-add it. The
    # redo must remove it again, so record under reverse ``added``.
    for name, prior_command, prior_scope in delta.updated:
        if _try_readd(name, prior_command, prior_scope, failed):
            reverse_to_remove.append((name, tuple(prior_command), prior_scope))

    reverse_delta = transitions.MCPDelta(
        added=tuple(reverse_to_remove),
        updated=tuple(reverse_to_readd),
    )
    if reverse_delta.is_empty():
        return None, failed
    return reverse_delta, failed


def _try_remove(name: str, scope: str, failed: list[tuple[str, str]]) -> bool:
    """Remove one server; return ``True`` on success, warn-and-continue otherwise."""
    try:
        mcp_mod.mcp_remove(name, scope=scope)
    except PluginToolMissing as exc:
        typer.secho(
            f"warning: skipping mcp remove of {name} — {exc}",
            err=True,
            fg=typer.colors.YELLOW,
        )
        return False
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        msg = mcp_mod.stderr_of(exc)
        failed.append((name, msg))
        typer.secho(
            f"FAILED mcp remove {name} — {msg}", err=True, fg=typer.colors.YELLOW
        )
        return False
    return True


def _try_readd(
    name: str,
    prior_command: tuple[str, ...],
    prior_scope: str,
    failed: list[tuple[str, str]],
) -> bool:
    """Re-add one server's prior command; ``True`` on success, warn otherwise.

    Removes any install-time registration first so the re-add of the prior
    command is unambiguous (mirrors the forward remove + re-add converge
    step). An already-absent server is benign — the subsequent add
    re-establishes the prior registration regardless.
    """
    prior_ref = McpServerRef(command=list(prior_command), scope=prior_scope)
    try:
        with contextlib.suppress(
            subprocess.CalledProcessError, subprocess.TimeoutExpired
        ):
            mcp_mod.mcp_remove(name, scope=prior_scope)
        mcp_mod.mcp_add(name, prior_ref)
    except PluginToolMissing as exc:
        typer.secho(
            f"warning: skipping mcp re-add of {name} — {exc}",
            err=True,
            fg=typer.colors.YELLOW,
        )
        return False
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        msg = mcp_mod.stderr_of(exc)
        failed.append((name, msg))
        typer.secho(
            f"FAILED mcp re-add {name} — {msg}", err=True, fg=typer.colors.YELLOW
        )
        return False
    return True
