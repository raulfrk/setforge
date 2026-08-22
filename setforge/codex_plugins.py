"""Codex plugin and marketplace discovery and reconciliation.

Only documented ``codex plugin`` commands are used.  In particular, Codex
currently exposes no plugin enable/disable operation, so this adapter never
edits Codex's private configuration to emulate one.
"""

from __future__ import annotations

import functools
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from setforge.binaries import resolve_binary, stderr_of
from setforge.config import MarketplaceSource, MarketplaceSourceKind, ReconcilePolicy
from setforge.errors import PluginToolMissing, SetforgeError

_TIMEOUT_S = 30
_CODEX_BIN_NAME = "codex"
_GITHUB_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_URL_USERINFO_RE = re.compile(r"(?P<authority>(?:[A-Za-z][A-Za-z0-9+.-]*:)?//)[^/\s]+@")


@dataclass(frozen=True, slots=True)
class InstalledPlugin:
    plugin_id: str
    name: str
    marketplace: str


@dataclass(frozen=True, slots=True)
class InstalledMarketplace:
    name: str
    root: Path
    source: MarketplaceSource | None = None


@dataclass(frozen=True, slots=True)
class CodexPluginPlan:
    policy: ReconcilePolicy
    to_install: tuple[str, ...]
    to_remove: tuple[str, ...]
    marketplaces_to_add: tuple[tuple[str, MarketplaceSource], ...]
    marketplaces_to_replace: tuple[
        tuple[str, MarketplaceSource, MarketplaceSource], ...
    ]
    pre_plugin_ids: tuple[str, ...]
    pre_marketplaces: tuple[tuple[str, str], ...]


@dataclass(slots=True)
class ReconcileReport:
    installed: list[str]
    removed: list[str]
    marketplaces_added: list[str]
    marketplaces_removed: list[tuple[str, dict[str, str]]]
    dry_run: bool
    failed: list[tuple[str, str]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(
            self.installed
            or self.removed
            or self.marketplaces_added
            or self.marketplaces_removed
        )


@functools.lru_cache(maxsize=1)
def _get_codex_bin() -> Path:
    path = resolve_binary(_CODEX_BIN_NAME)
    if path is None:
        raise PluginToolMissing(
            "codex binary not found; install Codex CLI or configure "
            "SETFORGE_CODEX_BIN / binaries.codex in SetForge's local config"
        )
    return path


def _run_json(args: list[str]) -> object:
    command = [str(_get_codex_bin()), *args, "--json"]
    try:
        result = subprocess.run(
            command,
            check=True,
            text=True,
            capture_output=True,
            timeout=_TIMEOUT_S,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = _URL_USERINFO_RE.sub(r"\g<authority>***@", stderr_of(exc))
        raise PluginToolMissing(
            f"Codex CLI does not support the required non-interactive command "
            f"{' '.join(args)!r}: {detail}"
        ) from exc
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise PluginToolMissing(
            f"Codex CLI returned invalid JSON for {' '.join(args)!r}"
        ) from exc


def _run_mutation(args: list[str]) -> None:
    raw = _run_json(args)
    if not isinstance(raw, dict) or raw.get("success") is not True:
        raise PluginToolMissing(
            f"Codex CLI reported an unsuccessful mutation for {' '.join(args)!r}"
        )


def _github_repo_from_remote(remote: str) -> str | None:
    value = remote.strip().removesuffix(".git")
    prefixes = (
        "https://github.com/",
        "http://github.com/",
        "git@github.com:",
        "ssh://git@github.com/",
    )
    for prefix in prefixes:
        if value.startswith(prefix):
            repo = value.removeprefix(prefix)
            return repo if _GITHUB_REPO_RE.fullmatch(repo) is not None else None
    return value if _GITHUB_REPO_RE.fullmatch(value) is not None else None


def _source_from_root(root: Path) -> MarketplaceSource:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            check=True,
            text=True,
            capture_output=True,
            timeout=_TIMEOUT_S,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return MarketplaceSource(source=MarketplaceSourceKind.PATH, path=root)
    repo = _github_repo_from_remote(result.stdout)
    if repo is None:
        return MarketplaceSource(source=MarketplaceSourceKind.PATH, path=root)
    return MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo=repo)


def _github_source_from_root(root: Path) -> MarketplaceSource:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            check=True,
            text=True,
            capture_output=True,
            timeout=_TIMEOUT_S,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise PluginToolMissing(
            f"cannot inspect Codex marketplace Git origin at {root}; refusing "
            "destructive source replacement"
        ) from exc
    repo = _github_repo_from_remote(result.stdout)
    if repo is None:
        raise PluginToolMissing(
            f"Codex marketplace at {root} has no canonical GitHub owner/repo origin"
        )
    return MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo=repo)


def _source_key(source: MarketplaceSource) -> tuple[str, str]:
    if source.source is MarketplaceSourceKind.GITHUB:
        return source.source.value, (source.repo or "").removesuffix(".git")
    return source.source.value, str((source.path or Path()).expanduser().resolve())


def list_installed() -> dict[str, InstalledPlugin]:
    raw = _run_json(["plugin", "list", "--available"])
    if not isinstance(raw, dict) or not isinstance(rows := raw.get("installed"), list):
        raise PluginToolMissing("Codex plugin list JSON has no installed list")
    installed: dict[str, InstalledPlugin] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise PluginToolMissing("Codex plugin list JSON contains a non-object row")
        plugin_id = row.get("pluginId")
        name = row.get("name")
        marketplace = row.get("marketplaceName")
        if (
            not isinstance(plugin_id, str)
            or not plugin_id
            or not isinstance(name, str)
            or not name
            or not isinstance(marketplace, str)
            or not marketplace
        ):
            raise PluginToolMissing(
                "Codex installed plugin JSON has an invalid identity"
            )
        if plugin_id in installed:
            raise PluginToolMissing(
                f"Codex plugin list JSON repeats pluginId {plugin_id!r}"
            )
        installed[plugin_id] = InstalledPlugin(plugin_id, name, marketplace)
    return installed


def list_marketplaces() -> dict[str, InstalledMarketplace]:
    raw = _run_json(["plugin", "marketplace", "list"])
    if not isinstance(raw, dict) or not isinstance(
        rows := raw.get("marketplaces"), list
    ):
        raise PluginToolMissing("Codex marketplace list JSON has no marketplaces list")
    installed: dict[str, InstalledMarketplace] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise PluginToolMissing("Codex marketplace JSON contains a non-object row")
        name, root = row.get("name"), row.get("root")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(root, str)
            or not root
        ):
            raise PluginToolMissing("Codex marketplace JSON has an invalid identity")
        if name in installed:
            raise PluginToolMissing(f"Codex marketplace JSON repeats name {name!r}")
        root_path = Path(root)
        installed[name] = InstalledMarketplace(
            name, root_path, _source_from_root(root_path)
        )
    return installed


def marketplace_add(source: MarketplaceSource) -> None:
    validate_marketplace_source(source)
    if source.source is MarketplaceSourceKind.GITHUB:
        assert source.repo is not None
        value = source.repo
    else:
        assert source.path is not None
        value = str(source.path.expanduser())
    _run_mutation(["plugin", "marketplace", "add", value])


def validate_marketplace_source(source: MarketplaceSource) -> None:
    """Reject source forms the native Codex CLI cannot safely replay."""
    if source.source is MarketplaceSourceKind.GITHUB:
        if source.repo is None or _GITHUB_REPO_RE.fullmatch(source.repo) is None:
            raise SetforgeError(
                "Codex GitHub marketplace repo must be credential-free owner/repo"
            )
    elif source.path is None:
        raise SetforgeError("Codex path marketplace has no path")


def marketplace_remove(name: str) -> None:
    _run_mutation(["plugin", "marketplace", "remove", name])


def marketplace_update(name: str) -> None:
    _run_mutation(["plugin", "marketplace", "upgrade", name])


def plugin_install(plugin_id: str) -> None:
    _run_mutation(["plugin", "add", plugin_id])


def plugin_remove(plugin_id: str) -> None:
    _run_mutation(["plugin", "remove", plugin_id])


def _marketplace_present(
    name: str, source: MarketplaceSource, installed: dict[str, InstalledMarketplace]
) -> bool:
    current = installed.get(name)
    if current is None:
        return False
    if source.source is MarketplaceSourceKind.PATH and source.path is not None:
        return current.root.resolve() == source.path.expanduser().resolve()
    actual = _github_source_from_root(current.root)
    return _source_key(actual) == _source_key(source)


def _inventory_snapshot(
    marketplaces: dict[str, InstalledMarketplace],
    declared: dict[str, MarketplaceSource] | None = None,
) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            (
                name,
                (
                    declared[name]
                    if declared is not None
                    and name in declared
                    and declared[name].source is MarketplaceSourceKind.PATH
                    and _marketplace_present(name, declared[name], marketplaces)
                    else item.source or _source_from_root(item.root)
                ).model_dump_json(exclude_none=True),
            )
            for name, item in marketplaces.items()
        )
    )


def plan_reconcile(
    *,
    declared_plugin_ids: set[str],
    marketplaces: dict[str, MarketplaceSource],
    policy: ReconcilePolicy,
) -> CodexPluginPlan:
    plugins = list_installed()
    current_marketplaces = list_marketplaces()
    to_install = tuple(sorted(declared_plugin_ids - set(plugins)))
    to_remove = (
        tuple(sorted(set(plugins) - declared_plugin_ids))
        if policy is ReconcilePolicy.PRUNE
        else ()
    )
    required_marketplaces = {
        plugin_id.rpartition("@")[2] for plugin_id in declared_plugin_ids
    }
    to_add = tuple(
        (name, marketplaces[name])
        for name in sorted(required_marketplaces)
        if name not in current_marketplaces
    )
    to_replace = tuple(
        (
            name,
            current_marketplaces[name].source
            or _source_from_root(current_marketplaces[name].root),
            marketplaces[name],
        )
        for name in sorted(required_marketplaces & set(current_marketplaces))
        if not _marketplace_present(name, marketplaces[name], current_marketplaces)
    )
    return CodexPluginPlan(
        policy=policy,
        to_install=to_install,
        to_remove=to_remove,
        marketplaces_to_add=to_add,
        marketplaces_to_replace=to_replace,
        pre_plugin_ids=tuple(sorted(plugins)),
        pre_marketplaces=_inventory_snapshot(current_marketplaces, marketplaces),
    )


def validate_plan(plan: CodexPluginPlan) -> None:
    current_marketplaces = list_marketplaces()
    expected_marketplaces = {
        name: MarketplaceSource.model_validate_json(source_json)
        for name, source_json in plan.pre_marketplaces
    }
    marketplace_changed = set(current_marketplaces) != set(
        expected_marketplaces
    ) or any(
        not _marketplace_present(name, source, current_marketplaces)
        for name, source in expected_marketplaces.items()
    )
    if tuple(sorted(list_installed())) != plan.pre_plugin_ids or marketplace_changed:
        raise SetforgeError("Codex plugin inventory changed after planning")


def apply_plan(  # noqa: C901 - mirrors the ordered native reconciliation phases
    plan: CodexPluginPlan, *, dry_run: bool = False
) -> ReconcileReport:
    read_only = dry_run or plan.policy is ReconcilePolicy.REPORT
    report = ReconcileReport(
        installed=list(plan.to_install),
        removed=list(plan.to_remove),
        marketplaces_added=[name for name, _ in plan.marketplaces_to_add],
        marketplaces_removed=[],
        dry_run=read_only,
    )
    if read_only:
        report.marketplaces_added.extend(
            name for name, _prior, _desired in plan.marketplaces_to_replace
        )
        report.marketplaces_removed.extend(
            (name, prior.model_dump(mode="json", exclude_none=True))
            for name, prior, _desired in plan.marketplaces_to_replace
        )
        return report
    validate_plan(plan)
    successful: set[str] = set()
    for name, prior_source, desired_source in plan.marketplaces_to_replace:
        error: tuple[str, str] | None = None
        try:
            marketplace_remove(name)
            marketplace_add(desired_source)
        except (PluginToolMissing, SetforgeError) as exc:
            error = (f"marketplace:{name}", str(exc))
        current = list_marketplaces()
        prior_present = _marketplace_present(name, prior_source, current)
        desired_present = _marketplace_present(name, desired_source, current)
        if not prior_present:
            report.marketplaces_removed.append(
                (name, prior_source.model_dump(mode="json", exclude_none=True))
            )
        if desired_present:
            report.marketplaces_added.append(name)
            successful.add(f"marketplace:{name}")
        if error is not None:
            report.failed.append(error)
    actions: list[tuple[str, Any, tuple[Any, ...]]] = [
        *(
            (f"marketplace:{name}", marketplace_add, (source,))
            for name, source in plan.marketplaces_to_add
        ),
        *((plugin_id, plugin_install, (plugin_id,)) for plugin_id in plan.to_install),
        *((plugin_id, plugin_remove, (plugin_id,)) for plugin_id in plan.to_remove),
    ]
    for identity, operation, args in actions:
        try:
            operation(*args)
            successful.add(identity)
        except (PluginToolMissing, SetforgeError) as exc:
            report.failed.append((identity, str(exc)))
            if identity.startswith("marketplace:"):
                name = identity.removeprefix("marketplace:")
                source = args[0]
                if _marketplace_present(name, source, list_marketplaces()):
                    successful.add(identity)
            elif (operation is plugin_install and identity in list_installed()) or (
                operation is plugin_remove and identity not in list_installed()
            ):
                successful.add(identity)
    report.marketplaces_added[:] = [
        name
        for name in report.marketplaces_added
        if f"marketplace:{name}" in successful
    ]
    report.installed[:] = [item for item in report.installed if item in successful]
    report.removed[:] = [item for item in report.removed if item in successful]
    return report
