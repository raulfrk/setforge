"""Read-only projection of native Codex state into lifecycle reports."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from setforge import codex_plugins, codex_resources, reconcile_adapter
from setforge.compare import CompareReport, CompareStatus, DriftClass, FileCompare
from setforge.config import Config, ResolvedProfile
from setforge.errors import PluginToolMissing
from setforge.reconcile import store as reconcile_store
from setforge.reconcile.types import file_id


def append_projection(
    report: CompareReport,
    cfg: Config,
    resolved: ResolvedProfile,
    repo_root: Path,
    *,
    profile: str,
) -> CompareReport:
    """Append deterministic native Codex config, plugin, and marketplace state."""
    entries: list[FileCompare] = []
    plans = codex_resources.plan_config_resources(
        cfg,
        resolved,
        repo_root,
        read_base=lambda resource_id: reconcile_store.read_base(
            profile, file_id(resource_id)
        ),
        stored_ids=tuple(map(str, reconcile_store.stored_file_ids(profile))),
        reconcile=False,
    )
    for plan in plans:
        matches = codex_resources.config_plan_matches_live(plan)
        entries.append(
            _entry(plan.resource_id, matches, "managed Codex TOML keys differ")
        )

    plugin_ids = reconcile_adapter.codex_plugin_ids(cfg, resolved)
    plugin_policy = reconcile_adapter.codex_plugin_policy(resolved)
    if (
        resolved.codex is not None
        and cfg.codex is not None
        and (plugin_ids or plugin_policy.value == "prune")
    ):
        try:
            plugin_plan = codex_plugins.plan_reconcile(
                declared_plugin_ids=plugin_ids,
                marketplaces=cfg.codex.marketplaces,
                policy=plugin_policy,
            )
        except PluginToolMissing as exc:
            entries.append(
                _entry(
                    "codex/plugin-state",
                    False,
                    f"Codex plugin state unavailable: {exc}",
                )
            )
            plugin_plan = None
        if plugin_plan is None:
            entries.sort(key=lambda entry: entry.name)
            report.entries.extend(entries)
            return replace(report, has_unexpected_drift=True)
        missing = set(plugin_plan.to_install)
        removed = set(plugin_plan.to_remove)
        for plugin_id in sorted(plugin_ids | removed):
            entries.append(
                _entry(
                    f"codex/plugin/{plugin_id}",
                    plugin_id not in missing and plugin_id not in removed,
                    "managed Codex plugin selection differs",
                )
            )
        add = {name for name, _source in plugin_plan.marketplaces_to_add}
        replacements = {
            name for name, _prior, _desired in plugin_plan.marketplaces_to_replace
        }
        required = {plugin_id.rpartition("@")[2] for plugin_id in plugin_ids}
        for name in sorted(required):
            entries.append(
                _entry(
                    f"codex/marketplace/{name}",
                    name not in add and name not in replacements,
                    "managed Codex marketplace source differs",
                )
            )

    entries.sort(key=lambda entry: entry.name)
    report.entries.extend(entries)
    return replace(
        report,
        has_unexpected_drift=report.has_unexpected_drift
        or any(entry.status is CompareStatus.DRIFTED for entry in entries),
    )


def config_destinations(
    cfg: Config, resolved: ResolvedProfile, repo_root: Path, *, profile: str
) -> tuple[Path, ...]:
    """Return selected native Codex TOML destinations without writing them."""
    plans = codex_resources.plan_config_resources(
        cfg,
        resolved,
        repo_root,
        read_base=lambda resource_id: reconcile_store.read_base(
            profile, file_id(resource_id)
        ),
        stored_ids=tuple(map(str, reconcile_store.stored_file_ids(profile))),
        reconcile=False,
    )
    return tuple(sorted({plan.destination for plan in plans}, key=str))


def _entry(name: str, matches: bool, reason: str) -> FileCompare:
    return FileCompare(
        name=name,
        status=CompareStatus.UNCHANGED if matches else CompareStatus.DRIFTED,
        diff="",
        drift_class=None if matches else DriftClass.UNEXPECTED,
        reason=None if matches else reason,
    )
