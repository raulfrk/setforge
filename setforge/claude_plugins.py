"""Claude plugin & marketplace reconcile, driven by the ``claude`` CLI.

All subprocess invocations honor the locked hygiene rules: the ``claude``
binary is resolved via :func:`setforge.binaries.resolve_binary` (which
walks CLI flag → env var → host-local config → PATH), raising
:class:`PluginToolMissing` if every layer comes up empty.
``subprocess.run`` always uses ``check=True, text=True,
capture_output=True, timeout=30``, and args are always a list with no
``shell=True``.

Implements a three-way reconcile per spec Δ2: plugins can be ``enabled``,
``disabled``, or ``absent``.  The reconcile computes:

- ``to_install`` — declared but absent (genuinely missing).
- ``to_enable``  — declared but disabled (cheap re-activation).
- ``to_disable`` — enabled but not declared (PRUNE only).

Marketplaces are always-on: declared marketplaces that are not installed
trigger ``marketplace_add``; stale marketplaces are never auto-evicted.
"""

from __future__ import annotations

import functools
import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from setforge import claude_marketplace_cache as _mp_cache
from setforge.binaries import load_host_local_config, resolve_binary, stderr_of
from setforge.claude_marketplace_cache import _TIMEOUT_S, resolve_marketplace_source
from setforge.config import (
    ClaudeInstallMode,
    Config,
    MarketplaceSource,
    MarketplaceSourceKind,
    ReconcilePolicy,
    ResolvedProfile,
)
from setforge.errors import ConfigError, MarketplaceCacheMiss, PluginToolMissing

__all__ = [
    "ReconcileReport",
    "ensure_claude_available",
    "list_installed",
    "list_marketplaces",
    "marketplace_add",
    "marketplace_remove",
    "marketplace_update",
    "plugin_disable",
    "plugin_enable",
    "plugin_install",
    "plugin_uninstall",
    "reconcile",
]

LOGGER: logging.Logger = logging.getLogger(__name__)

_CLAUDE_BIN_NAME = "claude"


@functools.lru_cache(maxsize=1)
def _get_claude_bin() -> Path:
    """Resolve the ``claude`` binary via :func:`resolve_binary` or raise.

    The result is cached for the process lifetime via
    :func:`functools.lru_cache`. Tests that change the resolved path
    between cases must call ``_get_claude_bin.cache_clear()``.
    Raises :class:`PluginToolMissing` when the resolved path is ``None``
    (binary not found at any layer). Non-executable paths surface as
    :class:`BinaryOverrideInvalid` propagated from :func:`resolve_binary`.
    """
    path = resolve_binary(_CLAUDE_BIN_NAME)
    if path is None:
        raise PluginToolMissing(
            "claude binary not found; install Claude CLI or set "
            "--claude-bin / SETFORGE_CLAUDE_BIN / local.yaml"
        )
    return path


def ensure_claude_available() -> None:
    """Resolve the claude CLI or raise.

    Raises :class:`PluginToolMissing` if the binary cannot be found at any
    layer, or :class:`BinaryOverrideInvalid` (propagated from
    :func:`resolve_binary`) if a configured override points at a
    non-executable path. Both are :class:`SetforgeError` subclasses, so an
    uncaught one still exits non-zero via the top-level CLI handler.
    """
    _get_claude_bin()


# ---------------------------------------------------------------------------
# Report dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """Summary of what reconcile did (or would do for REPORT / dry_run).

    ``to_install`` is a list of ``(plugin_name, marketplace_name)`` tuples
    for plugins that are genuinely absent.  ``to_enable`` and ``to_disable``
    are lists of plugin IDs in ``name@marketplace`` form.
    ``marketplaces_added`` lists marketplace names that were (or would be)
    added.  ``dry_run`` is ``True`` whenever no write commands were run.
    ``failed`` lists ``(id, err)`` tuples for actions that errored; it is
    the authoritative failure signal the CLI gates the exit code on.
    """

    to_install: list[tuple[str, str]]
    to_enable: list[str]
    to_disable: list[str]
    marketplaces_added: list[str]
    dry_run: bool
    failed: list[tuple[str, str]] = field(default_factory=list)

    def __bool__(self) -> bool:
        # Truthiness reports planned/executed work only; ``failed`` is
        # deliberately excluded. The CLI gates the failure exit code on
        # ``report.failed`` directly (see plugin_reconcile / ext reconcile),
        # so a failures-only report must still be falsy here for the
        # "nothing to reconcile" branch to mean "no work planned".
        return bool(
            self.to_install
            or self.to_enable
            or self.to_disable
            or self.marketplaces_added
        )


# ---------------------------------------------------------------------------
# Low-level subprocess wrappers
# ---------------------------------------------------------------------------


def list_marketplaces() -> dict[str, dict]:
    """Return installed marketplaces as ``{name: entry_dict}``.

    Calls ``claude plugin marketplace list --json`` and parses the JSON
    array.  Each element must have a ``name`` key; the whole element is
    kept as the value so callers can inspect ``source``, ``repo``, etc.

    Raises :class:`PluginToolMissing` when the binary is missing, the CLI
    call fails, or its output is not a JSON array.
    """
    claude = str(_get_claude_bin())
    try:
        result = subprocess.run(
            [claude, "plugin", "marketplace", "list", "--json"],
            check=True,
            text=True,
            capture_output=True,
            timeout=_TIMEOUT_S,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise PluginToolMissing(
            f"`claude plugin marketplace list` failed: {stderr_of(exc)}"
        ) from exc
    try:
        entries = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PluginToolMissing(
            "`claude plugin marketplace list` returned non-JSON output: "
            f"{result.stdout[:200]!r}"
        ) from exc
    if not isinstance(entries, list):
        raise PluginToolMissing(
            "`claude plugin marketplace list` returned non-list JSON: "
            f"{result.stdout[:200]!r}"
        )
    return {e["name"]: e for e in entries if "name" in e}


def list_installed() -> dict[str, dict]:
    """Return installed plugins as ``{id: entry_dict}`` where ``id`` is
    ``"<name>@<marketplace>"``.

    Calls ``claude plugin list --json`` and parses the JSON array.
    The ``enabled`` field (``bool``) is preserved on each entry.

    Raises :class:`PluginToolMissing` when the binary is missing, the CLI
    call fails, or its output is not a JSON array.
    """
    claude = str(_get_claude_bin())
    try:
        result = subprocess.run(
            [claude, "plugin", "list", "--json"],
            check=True,
            text=True,
            capture_output=True,
            timeout=_TIMEOUT_S,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise PluginToolMissing(
            f"`claude plugin list` failed: {stderr_of(exc)}"
        ) from exc
    try:
        entries = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PluginToolMissing(
            f"`claude plugin list` returned non-JSON output: {result.stdout[:200]!r}"
        ) from exc
    if not isinstance(entries, list):
        raise PluginToolMissing(
            f"`claude plugin list` returned non-list JSON: {result.stdout[:200]!r}"
        )
    return {e["id"]: e for e in entries if "id" in e}


def marketplace_add(name: str, source: MarketplaceSource) -> None:
    """Register a marketplace via ``claude plugin marketplace add <source>``.

    The source argument is the repo path (``owner/repo``) for GitHub
    sources, or the absolute file-system path for local sources.
    """
    claude = str(_get_claude_bin())
    if source.source is MarketplaceSourceKind.GITHUB:
        # narrows MarketplaceSource.repo (str | None) for mypy; upstream-guarded
        # by resolve_marketplace_source for GITHUB sources
        source_arg = source.repo or ""
    else:
        source_arg = str(source.path or "")
    subprocess.run(
        [claude, "plugin", "marketplace", "add", source_arg],
        check=True,
        text=True,
        capture_output=True,
        timeout=_TIMEOUT_S,
    )


def marketplace_remove(name: str) -> None:
    """Remove a marketplace via ``claude plugin marketplace remove <name>``."""
    claude = str(_get_claude_bin())
    subprocess.run(
        [claude, "plugin", "marketplace", "remove", name],
        check=True,
        text=True,
        capture_output=True,
        timeout=_TIMEOUT_S,
    )


def marketplace_update(name: str) -> None:
    """Update a marketplace via ``claude plugin marketplace update <name>``."""
    claude = str(_get_claude_bin())
    subprocess.run(
        [claude, "plugin", "marketplace", "update", name],
        check=True,
        text=True,
        capture_output=True,
        timeout=_TIMEOUT_S,
    )


def plugin_install(name: str, marketplace: str) -> None:
    """Install a plugin via ``claude plugin install <name>@<marketplace> --scope=user``.

    Always passes ``--scope=user`` per spec § Locked decisions row 8.
    """
    claude = str(_get_claude_bin())
    subprocess.run(
        [claude, "plugin", "install", f"{name}@{marketplace}", "--scope=user"],
        check=True,
        text=True,
        capture_output=True,
        timeout=_TIMEOUT_S,
    )


def plugin_uninstall(plugin_id: str) -> None:
    """Uninstall a plugin via ``claude plugin uninstall <id>``.

    ``plugin_id`` should be in ``"<name>@<marketplace>"`` form. Used by
    :func:`setforge.cli.revert` as the inverse of :func:`plugin_install`
    when reversing a transition's ``PluginDelta.installed`` list.
    """
    claude = str(_get_claude_bin())
    subprocess.run(
        [claude, "plugin", "uninstall", plugin_id],
        check=True,
        text=True,
        capture_output=True,
        timeout=_TIMEOUT_S,
    )


def plugin_enable(plugin_id: str) -> None:
    """Re-activate a disabled plugin via ``claude plugin enable <id>``.

    This is a cheap re-activation — no re-download happens.  ``plugin_id``
    should be in ``"<name>@<marketplace>"`` form.
    """
    claude = str(_get_claude_bin())
    subprocess.run(
        [claude, "plugin", "enable", plugin_id],
        check=True,
        text=True,
        capture_output=True,
        timeout=_TIMEOUT_S,
    )


def plugin_disable(plugin_id: str) -> None:
    """Disable a plugin via ``claude plugin disable <id>``.

    ``plugin_id`` should be in ``"<name>@<marketplace>"`` form.
    """
    claude = str(_get_claude_bin())
    subprocess.run(
        [claude, "plugin", "disable", plugin_id],
        check=True,
        text=True,
        capture_output=True,
        timeout=_TIMEOUT_S,
    )


# ---------------------------------------------------------------------------
# Reconcile algorithm — three-way per spec Δ2
# ---------------------------------------------------------------------------


def _split_id(pid: str) -> tuple[str, str]:
    """Split ``"name@marketplace"`` into ``(name, marketplace)``."""
    name, mp = pid.split("@", 1)
    return name, mp


def _add_declared_marketplaces(
    cfg: Config,
    mps_to_add: list[str],
    install_mode: ClaudeInstallMode,
    cache_root: Path,
    failed: list[tuple[str, str]],
    *,
    auto: bool = False,
) -> None:
    """Run install-mode dispatch + ``marketplace_add`` for each name in ``mps_to_add``.

    Side-effects ``failed`` in place on every per-marketplace failure
    (cache miss or ``claude``-side subprocess failure). Extracted from
    :func:`reconcile` so the host-local install-mode swap site is
    isolated from the larger plugin-state reconcile loop. Pure
    w.r.t. anything outside ``failed`` — the caller still owns the
    surrounding state machine.

    ``auto`` propagates to :func:`resolve_marketplace_source` and
    governs the cache-collision wizard. Default is interactive (the
    wizard fires on URL drift); pass ``auto=True`` from a non-
    interactive CLI path to refuse silent auto-resolution.
    """
    for mp_name in mps_to_add:
        LOGGER.info("adding marketplace: %s", mp_name)
        try:
            effective_source = resolve_marketplace_source(
                cfg.marketplaces[mp_name],
                install_mode,
                cache_root=cache_root,
                mp_name=mp_name,
                auto=auto,
            )
            marketplace_add(mp_name, effective_source)
        except MarketplaceCacheMiss as exc:
            LOGGER.warning("marketplace_add failed for %s: %s", mp_name, exc)
            failed.append((mp_name, str(exc)))
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            msg = stderr_of(exc)
            LOGGER.warning("marketplace_add failed for %s: %s", mp_name, msg)
            failed.append((mp_name, msg))


def _declared_plugin_ids(cfg: Config, profile: ResolvedProfile) -> set[str]:
    """Resolve the profile's bare plugin names to ``"name@marketplace"`` ids.

    Bare profile names (e.g. ``"superpowers"``) resolve via the
    top-level :attr:`Config.claude_plugins` registry. A name not
    present in the registry raises :class:`ConfigError`.
    """
    declared: set[str] = set()
    for bare_name in profile.claude_plugins:
        ref = cfg.claude_plugins.get(bare_name)
        if ref is None:
            raise ConfigError(
                f"profile references undeclared plugin: {bare_name!r} "
                f"(add it to top-level claude_plugins:)"
            )
        declared.add(f"{bare_name}@{ref.marketplace}")
    return declared


def _plugin_state_diff(
    declared: set[str], policy: ReconcilePolicy
) -> tuple[list[str], list[str], list[str]]:
    """Diff ``declared`` against the live plugin state per spec § Δ2.

    States:
    - ``to_install`` = declared - (enabled union disabled)   # genuinely absent
    - ``to_enable``  = declared intersect disabled                # present but off
    - ``to_disable`` = enabled - declared  (PRUNE only)
    """
    installed = list_installed()
    enabled = {pid for pid, p in installed.items() if p.get("enabled", True)}
    disabled = {pid for pid, p in installed.items() if not p.get("enabled", True)}

    to_install = sorted(declared - (enabled | disabled))
    to_enable = sorted(declared & disabled)
    # Compute to_disable for PRUNE and REPORT (both need the diff);
    # only ADDITIVE suppresses the diff entirely.
    if policy is not ReconcilePolicy.ADDITIVE:
        to_disable = sorted(enabled - declared)
    else:
        to_disable = []
    return to_install, to_enable, to_disable


def _build_report(
    to_install: list[str],
    to_enable: list[str],
    to_disable: list[str],
    mps_to_add: list[str],
    *,
    dry_run: bool,
    failed: list[tuple[str, str]] | None = None,
) -> ReconcileReport:
    """Assemble a :class:`ReconcileReport`, splitting install ids into pairs."""
    return ReconcileReport(
        to_install=[_split_id(pid) for pid in to_install],
        to_enable=to_enable,
        to_disable=to_disable,
        marketplaces_added=mps_to_add,
        dry_run=dry_run,
        failed=failed if failed is not None else [],
    )


def _read_only_report(
    to_install: list[str],
    to_enable: list[str],
    to_disable: list[str],
    mps_to_add: list[str],
) -> ReconcileReport:
    """Log the intended actions and build the read-only (``dry_run``) report."""
    LOGGER.info(
        "reconcile (read-only): to_install=%s to_enable=%s to_disable=%s "
        "marketplaces_to_add=%s",
        to_install,
        to_enable,
        to_disable,
        mps_to_add,
    )
    return _build_report(to_install, to_enable, to_disable, mps_to_add, dry_run=True)


def reconcile(
    cfg: Config,
    profile: ResolvedProfile,
    *,
    dry_run: bool = False,
) -> ReconcileReport:
    """Three-way reconcile per spec § Δ2.

    The plugin-state diff (``to_install`` / ``to_enable`` /
    ``to_disable``) is computed by :func:`_plugin_state_diff`; bare
    profile names resolve to ``"<name>@<marketplace>"`` form via
    :func:`_declared_plugin_ids` before any subprocess work. Raises
    :class:`ConfigError` when a profile name is absent from the registry.

    Marketplaces (always-on, regardless of policy): each declared
    marketplace not in ``list_marketplaces()`` gets ``marketplace_add``
    called (except under ``REPORT`` policy or ``dry_run=True``, where it
    is listed but not executed).  Stale marketplaces are never evicted.

    ``dry_run=True`` logs intended actions and returns without running any
    write subprocess. ``REPORT`` policy behaves identically to
    ``dry_run=True`` for write suppression.
    """
    declared = _declared_plugin_ids(cfg, profile)
    to_install, to_enable, to_disable = _plugin_state_diff(
        declared, profile.plugins_reconcile
    )

    # Marketplaces: always-on regardless of policy
    mps_to_add = sorted(set(cfg.marketplaces) - set(list_marketplaces()))

    if dry_run or profile.plugins_reconcile is ReconcilePolicy.REPORT:
        return _read_only_report(to_install, to_enable, to_disable, mps_to_add)

    failed: list[tuple[str, str]] = []

    # Host-local install-mode dispatch (LOCAL_CLONE swaps GitHub sources
    # for on-disk cache paths; see _add_declared_marketplaces).
    install_mode = load_host_local_config().claude.install_mode
    _add_declared_marketplaces(
        cfg, mps_to_add, install_mode, _mp_cache.MARKETPLACE_CACHE_ROOT, failed
    )

    _reconcile_install(to_install, to_enable, failed)
    if profile.plugins_reconcile is ReconcilePolicy.PRUNE:
        _reconcile_remove(to_disable, failed)

    return _build_report(
        to_install, to_enable, to_disable, mps_to_add, dry_run=False, failed=failed
    )


def _reconcile_install(
    to_install: list[str],
    to_enable: list[str],
    failed: list[tuple[str, str]],
) -> None:
    """Run the install + enable loop, appending subprocess failures to ``failed``.

    Per spec § Algorithm β2: freshly-installed plugins
    land disabled in ``installed_plugins.json`` — ``claude plugin install``
    never touches ``enabledPlugins``. To make a single reconcile run land
    each plugin active, successful installs are routed through the enable
    loop via a separate working list, leaving the report's ``to_enable``
    field semantically clean (the original ``declared intersect disabled``
    set, NOT freshly-installed plugins). Failed installs are NOT enabled.
    """
    runtime_to_enable: list[str] = list(to_enable)
    for pid in to_install:
        name, mp = _split_id(pid)
        LOGGER.info("installing plugin: %s @ %s", name, mp)
        try:
            plugin_install(name, mp)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            msg = stderr_of(exc)
            LOGGER.warning("plugin_install failed for %s: %s", pid, msg)
            failed.append((pid, msg))
        else:
            runtime_to_enable.append(pid)

    for pid in runtime_to_enable:
        LOGGER.info("enabling plugin: %s", pid)
        try:
            plugin_enable(pid)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            msg = stderr_of(exc)
            LOGGER.warning("plugin_enable failed for %s: %s", pid, msg)
            failed.append((pid, msg))


def _reconcile_remove(
    to_disable: list[str],
    failed: list[tuple[str, str]],
) -> None:
    """Run the disable loop for ``PRUNE`` policy, recording failures."""
    for pid in to_disable:
        LOGGER.info("disabling plugin: %s", pid)
        try:
            plugin_disable(pid)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            msg = stderr_of(exc)
            LOGGER.warning("plugin_disable failed for %s: %s", pid, msg)
            failed.append((pid, msg))
