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

When structured inspection is unavailable, an "already exists" response
from ``mcp add`` proves presence but cannot prove the command or scope.
That case reports a failure and preserves the existing registration. The
per-server loop catches :class:`subprocess.CalledProcessError` /
:class:`subprocess.TimeoutExpired`, records ``(name, stderr)`` in the
report's ``failed`` list, and continues — the CLI gates its exit code on
the aggregated failures, never aborting the whole pass on one bad server.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from setforge.binaries import resolve_binary, stderr_of
from setforge.config import Config, McpScope, McpServerRef, ResolvedProfile
from setforge.errors import ConfigError, PluginToolMissing, SetforgeError

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

    ``added`` lists ``(name, command, scope)`` triples successfully registered
    this pass, including replacement endpoints. The command/scope ride along
    so the transition delta can re-add the exact registration on a redo.
    ``updated`` lists ``(name, prior_command, prior_scope)`` triples once the
    prior registration has been removed. The prior command/scope is captured
    immediately so revert can restore it even when replacement registration
    later fails. ``failed`` lists ``(name, stderr)``
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


@dataclass(frozen=True, slots=True)
class McpPlanEntry:
    """One MCP registration decision computed from the live registry."""

    name: str
    command: tuple[str, ...]
    scope: McpScope
    prior: tuple[tuple[str, ...], str] | None

    @property
    def ref(self) -> McpServerRef:
        """Build a detached validated registration from immutable primitives."""
        return McpServerRef(command=list(self.command), scope=self.scope)


@dataclass(frozen=True, slots=True)
class McpPlan:
    """The exact MCP operations selected by a read-only probe."""

    entries: tuple[McpPlanEntry, ...]
    preconditions: tuple[tuple[str, tuple[tuple[str, ...], str] | None], ...]
    context: tuple[str, str, str] | None = None


def plan_reconcile(cfg: Config, profile: ResolvedProfile) -> McpPlan:
    """Probe declared servers and retain only absent or drifted entries."""
    refs = _declared_refs(cfg, profile)
    if not refs:
        return McpPlan(entries=(), preconditions=())
    ensure_claude_available()
    context = inventory_context()
    entries: list[McpPlanEntry] = []
    preconditions: list[tuple[str, tuple[tuple[str, ...], str] | None]] = []
    for name, ref in refs:
        current = mcp_get_command(name)
        frozen_current = None if current is None else (tuple(current[0]), current[1])
        preconditions.append((name, frozen_current))
        if current is None:
            entries.append(
                McpPlanEntry(
                    name=name,
                    command=tuple(ref.command),
                    scope=ref.scope,
                    prior=None,
                )
            )
            continue
        prior_command, prior_scope = current
        if prior_command == ref.command and prior_scope == ref.scope:
            continue
        entries.append(
            McpPlanEntry(
                name=name,
                command=tuple(ref.command),
                scope=ref.scope,
                prior=(tuple(prior_command), prior_scope),
            )
        )
    return McpPlan(
        entries=tuple(entries), preconditions=tuple(preconditions), context=context
    )


def apply_plan(plan: McpPlan) -> McpReconcileReport:
    """Apply a plan after validating that each probed precondition still holds."""
    if plan.preconditions:
        require_inventory_context(plan.context)
    added: list[tuple[str, list[str], str]] = []
    updated: list[tuple[str, list[str], str]] = []
    failed: list[tuple[str, str]] = []
    actions = {entry.name: entry for entry in plan.entries}
    for name, frozen_expected in plan.preconditions:
        current = mcp_get_command(name)
        expected = (
            None
            if frozen_expected is None
            else (list(frozen_expected[0]), frozen_expected[1])
        )
        if current != expected:
            failed.append((name, "MCP inventory changed after planning"))
            continue
        entry = actions.get(name)
        if entry is None:
            continue
        if entry.prior is None:
            _converge_add(entry.name, entry.ref, added=added, failed=failed)
            continue
        prior_command, prior_scope = entry.prior
        _converge_update(
            entry.name,
            entry.ref,
            prior_command=list(prior_command),
            prior_scope=prior_scope,
            added=added,
            updated=updated,
            failed=failed,
        )
    return McpReconcileReport(added=added, updated=updated, failed=failed)


def validate_plan(plan: McpPlan) -> None:
    """Fail if an MCP registration no longer matches its planned precondition."""
    if plan.preconditions:
        require_inventory_context(plan.context)
    for name, frozen_expected in plan.preconditions:
        current = mcp_get_command(name)
        expected = (
            None
            if frozen_expected is None
            else (list(frozen_expected[0]), frozen_expected[1])
        )
        if current != expected:
            raise SetforgeError(f"MCP inventory changed after planning: {name}")


def inventory_context() -> tuple[str, str, str]:
    """Resolve native cwd, global config, and Claude's local-project key."""
    cwd = Path.cwd().resolve()
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    config_dir = Path(override).expanduser() if override else Path.home()
    try:
        git = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetforgeError("cannot determine native MCP local-project key") from exc
    if git.returncode == 0 and git.stdout.strip():
        local_key = Path(git.stdout.strip()).resolve()
    elif git.returncode != 0 and "not a git repository" in git.stderr.lower():
        local_key = cwd
    else:
        raise SetforgeError("cannot determine native MCP local-project key")
    return str(cwd), str((config_dir / ".claude.json").resolve()), str(local_key)


def parse_inventory_context(raw: object) -> tuple[str, str, str]:
    """Validate persisted native coordinates without following old paths."""
    if not isinstance(raw, (list, tuple)) or len(raw) != 3:
        raise ValueError("invalid native MCP inventory context")
    if not all(
        isinstance(value, str)
        and Path(value).is_absolute()
        and ".." not in Path(value).parts
        and str(Path(value)) == value
        for value in raw
    ):
        raise ValueError("invalid native MCP inventory context")
    cwd, config, local_key = raw
    return cwd, config, local_key


def require_inventory_context(context: tuple[str, str, str] | None) -> None:
    """Refuse native effects when their original destination is unprovable."""
    if context is None:
        raise SetforgeError(
            "legacy MCP record lacks native inventory context; automatic reversal "
            "is unsafe — restore the original registration manually"
        )
    if inventory_context() != context:
        raise SetforgeError("native MCP inventory context changed; refusing mutation")


def _unique_native_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate native MCP inventory key")
        result[key] = value
    return result


def _native_config(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_native_keys
        )
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, ValueError) as exc:
        raise SetforgeError(f"cannot read native MCP inventory: {path}") from exc
    if not isinstance(payload, dict):
        raise SetforgeError(f"invalid native MCP inventory: {path}")
    return payload


def _registration_command(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        raise SetforgeError("invalid MCP registration")
    if set(payload) - {"command", "args", "scope", "type", "env"}:
        raise SetforgeError("cannot represent MCP registration fields")
    if payload.get("type", "stdio") != "stdio" or payload.get("env", {}) != {}:
        raise SetforgeError("cannot represent MCP registration type or environment")
    command, args = payload.get("command"), payload.get("args", [])
    if (
        not isinstance(command, str)
        or not command
        or not isinstance(args, list)
        or not all(isinstance(arg, str) for arg in args)
    ):
        raise SetforgeError("invalid MCP registration command")
    return [command, *args]


def _native_command(name: str) -> tuple[list[str], str] | None:
    cwd, config_path, local_key = inventory_context()
    config = _native_config(Path(config_path))
    projects = config.get("projects", {})
    if not isinstance(projects, dict):
        raise SetforgeError("invalid native MCP local inventory")
    local = projects.get(local_key, {})
    if not isinstance(local, dict):
        raise SetforgeError("invalid native MCP local inventory")
    sources = (
        ("user", config),
        ("local", local),
        ("project", _native_config(Path(cwd) / ".mcp.json")),
    )
    found: list[tuple[list[str], str]] = []
    for scope, source in sources:
        servers = source.get("mcpServers", {})
        if not isinstance(servers, dict):
            raise SetforgeError("invalid native MCP server inventory")
        if name not in servers:
            continue
        row = servers[name]
        if not isinstance(row, dict) or set(row) - {"command", "args", "type", "env"}:
            raise SetforgeError("cannot represent native MCP registration")
        found.append((_registration_command(row), scope))
    if len(found) > 1:
        raise SetforgeError(f"ambiguous native MCP registration across scopes: {name}")
    return found[0] if found else None


def mcp_get_command(name: str) -> tuple[list[str], str] | None:
    """Read exact JSON receipts, falling back to native scoped config when unsupported.

    Only a confirmed absence returns None. Invalid, ambiguous or unreadable
    inventory cannot authorize an add, update, inverse, or recovery.
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
    except subprocess.CalledProcessError as exc:
        message = stderr_of(exc).lower()
        if "unknown option" in message and "--json" in message:
            return _native_command(name)
        if "no mcp server found" in message:
            return None
        raise SetforgeError(f"cannot inspect MCP registration: {name}") from exc
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise SetforgeError(f"cannot inspect MCP registration: {name}") from exc
    try:
        payload = json.loads(result.stdout, object_pairs_hook=_unique_native_keys)
    except ValueError as exc:
        raise SetforgeError(f"invalid MCP JSON receipt: {name}") from exc
    command = _registration_command(payload)
    scope = payload.get("scope", "user")
    try:
        McpScope(scope)
    except (ValueError, TypeError) as exc:
        raise SetforgeError(f"invalid MCP registration scope: {name}") from exc
    return command, scope


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
    """Return ``True`` when ``stderr`` reports an existing registration."""
    lowered = stderr.lower()
    return any(marker in lowered for marker in _ALREADY_EXISTS_MARKERS)


def _declared_refs(
    cfg: Config, profile: ResolvedProfile
) -> list[tuple[str, McpServerRef]]:
    """Resolve the profile's bare MCP names to ``(name, McpServerRef)`` pairs.

    A name absent from the top-level :attr:`Config.mcp_servers` registry
    raises :class:`ConfigError` — mirrors
    :func:`setforge.reconcile_adapter.plugin_ids`. (``load_config``
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
) -> McpReconcileReport:
    """Converge the declared MCP-server set (add-absent / update-on-change).

    For each declared server:

    - read the live command best-effort via :func:`mcp_get_command`;
    - if it matches the declared command + scope, do nothing;
    - if it differs, record the removed PRIOR endpoint under ``updated``
      and a successfully registered replacement under ``added``;
    - if it is absent, attempt :func:`mcp_add`; an "already exists"
      response is a failure because it cannot prove convergence;
    - unreadable or ambiguous inventory refuses planning before effects.

    Undeclared live servers are NEVER removed. Per-server subprocess
    failures are caught and appended to the report's ``failed`` list so
    one bad server does not abort the pass.

    Raises:
        ConfigError: a profile MCP name absent from the top-level
            ``mcp_servers:`` registry (from :func:`_declared_refs`).
        PluginToolMissing: the ``claude`` binary cannot be resolved (from
            the first ``claude mcp`` subprocess via :func:`_get_claude_bin`).
    """
    return apply_plan(plan_reconcile(cfg, profile))


def _converge_add(
    name: str,
    ref: McpServerRef,
    *,
    added: list[tuple[str, list[str], str]],
    failed: list[tuple[str, str]],
) -> None:
    """Add an absent server without mistaking existence for verified convergence."""
    LOGGER.info("adding mcp server: %s", name)
    try:
        mcp_add(name, ref)
        added.append((name, list(ref.command), ref.scope))
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        msg = stderr_of(exc)
        if _is_already_exists(msg):
            detail = (
                "cannot verify existing MCP server command and scope: structured "
                "inspection was unavailable; existing registration preserved"
            )
            LOGGER.warning("mcp add could not verify %s: %s", name, detail)
            failed.append((name, detail))
            return
        LOGGER.warning("mcp add failed for %s: %s", name, msg)
        failed.append((name, msg))


def _converge_update(
    name: str,
    ref: McpServerRef,
    *,
    prior_command: list[str],
    prior_scope: str,
    added: list[tuple[str, list[str], str]],
    updated: list[tuple[str, list[str], str]],
    failed: list[tuple[str, str]],
) -> None:
    """Update a drifted server (remove + re-add), recording the prior command.

    The ``updated`` entry stores the PRIOR command + scope so revert can
    re-add the original registration — a flat name alone is not invertible.
    It is recorded immediately after a successful remove because that is the
    destructive point; replacement failure must not erase the inverse.
    """
    LOGGER.info("updating mcp server: %s", name)
    try:
        mcp_remove(name, scope=prior_scope)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        msg = stderr_of(exc)
        LOGGER.warning("mcp update failed for %s: %s", name, msg)
        failed.append((name, msg))
        return
    # Removal is already a successful destructive mutation. Record its inverse
    # immediately so the transition remains revertible even when replacement
    # registration fails below.
    updated.append((name, list(prior_command), prior_scope))
    try:
        mcp_add(name, ref)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        msg = stderr_of(exc)
        LOGGER.warning("mcp update failed for %s: %s", name, msg)
        failed.append((name, msg))
        return
    added.append((name, list(ref.command), ref.scope))
