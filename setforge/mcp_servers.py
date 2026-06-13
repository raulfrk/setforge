"""MCP-server registration, driven by the ``claude mcp`` CLI.

setforge tracks MCP servers the same way it tracks plugins: a top-level
``mcp_servers:`` registry in ``setforge.yaml`` maps a bare name to a
:class:`~setforge.config.McpServerRef` (a command token list + a scope),
and each profile lists the bare names it wants registered. On install the
:func:`reconcile` pass here CONVERGES the declared set: a declared server
that is absent is added, one whose declared command differs from the live
registration is updated (remove + re-add), and undeclared servers are
never touched — setforge will not evict an MCP server the user registered
by hand.

Subprocess hygiene mirrors :mod:`setforge.claude_plugins`: the ``claude``
binary is resolved via :func:`setforge.binaries.resolve_binary` (a HARD
requirement — a missing ``claude`` raises :class:`PluginToolMissing`),
every ``subprocess.run`` uses ``shell=False`` with an explicit token list
and a ``timeout=``, and the ``add`` argv places flags BEFORE the name with
a literal ``"--"`` separator ahead of the user's command tokens::

    claude mcp add --scope <scope> <name> -- <command tokens...>

Idempotency for user-scope servers cannot lean on ``claude mcp list`` /
``get`` (unreliable for that scope), so an "already exists" stderr from
``mcp add`` is string-matched and swallowed as a benign no-op. The
per-server loop catches :class:`subprocess.CalledProcessError` /
:class:`subprocess.TimeoutExpired`, records ``(name, stderr)`` in the
report's ``failed`` list, and continues — the CLI gates its exit code on
the aggregated failures, never aborting the whole pass on one bad server.
"""

from __future__ import annotations

import functools
import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from setforge.binaries import resolve_binary, stderr_of
from setforge.config import Config, McpServerRef, ResolvedProfile
from setforge.errors import ConfigError, PluginToolMissing

__all__ = [
    "McpReconcileReport",
    "ensure_claude_available",
    "mcp_add",
    "mcp_get_command",
    "mcp_remove",
    "reconcile",
]

LOGGER: logging.Logger = logging.getLogger(__name__)

_CLAUDE_BIN_NAME = "claude"
_TIMEOUT_S = 30
"""Per-call timeout for ``claude mcp`` subprocesses (seconds)."""

# Substrings that mark an "already registered" outcome on ``mcp add``.
# Matched case-insensitively against the failed call's stderr so a server
# that is already present is treated as a benign no-op rather than a
# failure (user scope makes a pre-check via ``mcp list`` unreliable).
_ALREADY_EXISTS_MARKERS: tuple[str, ...] = (
    "already exists",
    "already registered",
    "already configured",
)


@functools.lru_cache(maxsize=1)
def _get_claude_bin() -> Path:
    """Resolve the ``claude`` binary via :func:`resolve_binary` or raise.

    Cached for the process lifetime; tests that change the resolved path
    between cases must call ``_get_claude_bin.cache_clear()``. Raises
    :class:`PluginToolMissing` when no layer resolves the binary.
    """
    path = resolve_binary(_CLAUDE_BIN_NAME)
    if path is None:
        raise PluginToolMissing(
            "claude binary not found; install Claude CLI or set "
            "--claude-bin / SETFORGE_CLAUDE_BIN / local.yaml"
        )
    return path


def ensure_claude_available() -> None:
    """Resolve the ``claude`` CLI or raise :class:`PluginToolMissing`."""
    _get_claude_bin()


@dataclass(frozen=True, slots=True)
class McpReconcileReport:
    """Summary of what an MCP reconcile pass did.

    ``added`` lists ``(name, command, scope)`` triples registered for the
    first time this pass — the command/scope ride along so the transition
    delta can re-add the exact registration on a redo.
    ``updated`` lists ``(name, prior_command, prior_scope)`` triples for
    servers whose declared command differed from the live one and were
    therefore removed + re-added — the prior command/scope is captured so
    revert can re-add the original. ``failed`` lists ``(name, stderr)``
    for per-server subprocess errors; it is the authoritative failure
    signal the CLI gates the exit code on.
    """

    added: list[tuple[str, list[str], str]]
    updated: list[tuple[str, list[str], str]]
    failed: list[tuple[str, str]] = field(default_factory=list)

    def __bool__(self) -> bool:
        # Planned/executed work only; ``failed`` is excluded so the
        # "nothing to reconcile" branch in the CLI stays meaningful.
        return bool(self.added or self.updated)


def mcp_get_command(name: str) -> tuple[list[str], str] | None:
    """Best-effort read of a registered server's command + scope.

    Calls ``claude mcp get <name> --json`` and parses the command token
    list and scope out of the JSON. Returns ``None`` when the server is
    absent, when the CLI does not support ``--json`` / ``get``, or when
    the output cannot be parsed — every one of those is a "cannot
    determine current command" signal that the converge path treats as
    "fall back to a plain add (idempotent)". NEVER raises on a missing
    server; only the binary-missing case propagates as
    :class:`PluginToolMissing` (resolved upstream by the caller).
    """
    claude = str(_get_claude_bin())
    try:
        result = subprocess.run(
            [claude, "mcp", "get", name, "--json"],
            check=True,
            text=True,
            capture_output=True,
            timeout=_TIMEOUT_S,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    command = payload.get("command")
    args = payload.get("args", [])
    scope = payload.get("scope", "user")
    if not isinstance(command, str) or not isinstance(args, list):
        return None
    tokens = [command, *(str(a) for a in args)]
    return tokens, str(scope)


def mcp_add(name: str, ref: McpServerRef) -> None:
    """Register a server via ``claude mcp add --scope <scope> <name> -- <tokens>``.

    Flags precede ``name``; a literal ``"--"`` element separates the
    setforge-controlled portion from the user's command tokens so a token
    that starts with ``-`` is never parsed as a ``claude`` flag.
    ``shell=False`` with an explicit list — user tokens are never joined
    into a shell string. Raises :class:`subprocess.CalledProcessError` /
    :class:`subprocess.TimeoutExpired` on failure (the caller's per-item
    handler classifies "already exists" vs a real error).
    """
    claude = str(_get_claude_bin())
    subprocess.run(
        [claude, "mcp", "add", "--scope", ref.scope, name, "--", *ref.command],
        check=True,
        text=True,
        capture_output=True,
        timeout=_TIMEOUT_S,
    )


def mcp_remove(name: str, *, scope: str = "user") -> None:
    """Remove a server via ``claude mcp remove --scope <scope> <name>``."""
    claude = str(_get_claude_bin())
    subprocess.run(
        [claude, "mcp", "remove", "--scope", scope, name],
        check=True,
        text=True,
        capture_output=True,
        timeout=_TIMEOUT_S,
    )


def _is_already_exists(stderr: str) -> bool:
    """Return ``True`` when ``stderr`` reads as an already-registered no-op."""
    lowered = stderr.lower()
    return any(marker in lowered for marker in _ALREADY_EXISTS_MARKERS)


def _declared_refs(
    cfg: Config, profile: ResolvedProfile
) -> list[tuple[str, McpServerRef]]:
    """Resolve the profile's bare MCP names to ``(name, McpServerRef)`` pairs.

    A name absent from the top-level :attr:`Config.mcp_servers` registry
    raises :class:`ConfigError` — mirrors
    :func:`setforge.claude_plugins._declared_plugin_ids`. (``load_config``
    already cross-validates, so this is a defensive second line.)
    """
    refs: list[tuple[str, McpServerRef]] = []
    for bare_name in profile.mcp_servers:
        ref = cfg.mcp_servers.get(bare_name)
        if ref is None:
            raise ConfigError(
                f"profile references undeclared MCP server: {bare_name!r} "
                f"(add it to top-level mcp_servers:)"
            )
        refs.append((bare_name, ref))
    return refs


def reconcile(
    cfg: Config,
    profile: ResolvedProfile,
    *,
    dry_run: bool = False,
) -> McpReconcileReport:
    """Converge the declared MCP-server set (add-absent / update-on-change).

    For each declared server:

    - read the live command best-effort via :func:`mcp_get_command`;
    - if it matches the declared command + scope, do nothing;
    - if it differs, remove + re-add and record an ``updated`` entry
      carrying the PRIOR command + scope (so revert can re-add it);
    - if the server cannot be read (absent, or ``get`` unsupported),
      attempt :func:`mcp_add` and treat an "already exists" stderr as a
      benign no-op rather than a failure.

    Undeclared live servers are NEVER removed. Per-server subprocess
    failures are caught and appended to the report's ``failed`` list so
    one bad server does not abort the pass. ``dry_run=True`` plans the
    work (populating ``added`` / ``updated``) without running any write
    subprocess.
    """
    declared = _declared_refs(cfg, profile)
    added: list[tuple[str, list[str], str]] = []
    updated: list[tuple[str, list[str], str]] = []
    failed: list[tuple[str, str]] = []

    for name, ref in declared:
        # ``mcp_get_command`` swallows a missing server / unsupported
        # ``get`` into ``None``, so a ``None`` here unambiguously means
        # "treat as absent → add" (the add path itself swallows an
        # "already exists" stderr, so a stale-but-present server is still
        # a benign no-op).
        current = mcp_get_command(name)
        if current is not None:
            prior_command, prior_scope = current
            if prior_command == ref.command and prior_scope == ref.scope:
                LOGGER.info("mcp server up-to-date: %s", name)
                continue
            _converge_update(
                name,
                ref,
                prior_command=prior_command,
                prior_scope=prior_scope,
                dry_run=dry_run,
                updated=updated,
                failed=failed,
            )
            continue
        _converge_add(name, ref, dry_run=dry_run, added=added, failed=failed)

    return McpReconcileReport(added=added, updated=updated, failed=failed)


def _converge_add(
    name: str,
    ref: McpServerRef,
    *,
    dry_run: bool,
    added: list[tuple[str, list[str], str]],
    failed: list[tuple[str, str]],
) -> None:
    """Add an absent server; swallow an "already exists" stderr as a no-op."""
    LOGGER.info("adding mcp server: %s", name)
    if dry_run:
        added.append((name, list(ref.command), ref.scope))
        return
    try:
        mcp_add(name, ref)
        added.append((name, list(ref.command), ref.scope))
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        msg = stderr_of(exc)
        if _is_already_exists(msg):
            LOGGER.info("mcp server already registered (no-op): %s", name)
            return
        LOGGER.warning("mcp add failed for %s: %s", name, msg)
        failed.append((name, msg))


def _converge_update(
    name: str,
    ref: McpServerRef,
    *,
    prior_command: list[str],
    prior_scope: str,
    dry_run: bool,
    updated: list[tuple[str, list[str], str]],
    failed: list[tuple[str, str]],
) -> None:
    """Update a drifted server (remove + re-add), recording the prior command.

    The ``updated`` entry stores the PRIOR command + scope so revert can
    re-add the original registration — a flat name alone is not
    invertible. Only recorded on a fully-successful remove + re-add.
    """
    LOGGER.info("updating mcp server: %s", name)
    if dry_run:
        updated.append((name, list(prior_command), prior_scope))
        return
    try:
        mcp_remove(name, scope=prior_scope)
        mcp_add(name, ref)
        updated.append((name, list(prior_command), prior_scope))
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        msg = stderr_of(exc)
        LOGGER.warning("mcp update failed for %s: %s", name, msg)
        failed.append((name, msg))
