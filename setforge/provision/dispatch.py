"""Wire declared packages / ``cargo_binaries`` into the provisioner protocol."""

from __future__ import annotations

from collections.abc import Sequence
from itertools import groupby

import setforge.provision.cargo as _cargo  # noqa: F401
import setforge.provision.github_release as _github_release  # noqa: F401
import setforge.provision.go as _go  # noqa: F401
import setforge.provision.local as _local  # noqa: F401
import setforge.provision.python as _python  # noqa: F401
from setforge.config import (
    CargoPackage,
    Config,
    ExtensionPackage,
    GitHubReleasePackage,
    GoPackage,
    LocalPackage,
    Package,
    PluginPackage,
    PythonPackage,
    ReconcilePolicy,
    ResolvedProfile,
)
from setforge.lockfile import LockFile
from setforge.provision.bundle import execute_bundle
from setforge.provision.driver import reconcile
from setforge.provision.lock_apply import apply_lock_to_items
from setforge.provision.protocol import (
    Identity,
    Outcome,
    ProvisionItem,
    ReconcileResult,
)
from setforge.provision.registry import build


def _package_identity(pkg: Package) -> Identity:
    match pkg:
        case CargoPackage():
            name = pkg.crate
        case PythonPackage():
            name = pkg.package
        case GoPackage():
            name = pkg.module
        case GitHubReleasePackage():
            name = pkg.repo
        case LocalPackage():
            name = pkg.binary
        case _:  # pragma: no cover - exhaustive over the Package union
            raise AssertionError(f"no identity mapping for package {pkg!r}")
    return Identity(key=name, display=name)


def resolve_provision_items(
    cfg: Config, resolved: ResolvedProfile
) -> list[ProvisionItem]:
    items: list[ProvisionItem] = []
    seen: set[Identity] = set()

    def _add(item: ProvisionItem) -> None:
        if item.identity in seen:
            return
        seen.add(item.identity)
        items.append(item)

    for ref in resolved.packages:
        pkg = cfg.packages[ref]
        # Plugin / extension packages route to the legacy claude_plugins /
        # vscode_extensions reconcile engines (via reconcile_adapter), NOT
        # the add-only generic provisioner driver. Skip them here so they
        # are not double-provisioned.
        if isinstance(pkg, PluginPackage | ExtensionPackage):
            continue
        _add(
            ProvisionItem(
                type=pkg.type.value,
                identity=_package_identity(pkg),
                config=pkg,
                version=getattr(pkg, "version", None),
                checksum=getattr(pkg, "checksum", None),
            )
        )
    for crate in resolved.cargo_binaries:
        crate = crate.strip()
        if not crate:
            continue
        _add(
            ProvisionItem(
                type="cargo",
                identity=Identity(key=crate, display=crate),
            )
        )
    return items


def run_provisioning(
    cfg: Config,
    resolved: ResolvedProfile,
    *,
    report_only: bool = False,
    lock: LockFile | None = None,
) -> list[ReconcileResult]:
    results: list[ReconcileResult] = []
    for name in resolved.bundles:
        results.append(execute_bundle(cfg.bundles[name], cfg, report_only=report_only))
    items = resolve_provision_items(cfg, resolved)
    # Offline pin override before reconcile (see lock_apply module docstring).
    if lock is not None:
        items = apply_lock_to_items(items, lock)
    items.sort(key=lambda it: it.type)
    for _type, group_iter in groupby(items, key=lambda it: it.type):
        group = list(group_iter)
        provisioner = build(group[0])
        results.append(
            reconcile(
                provisioner,
                group,
                policy=ReconcilePolicy.ADDITIVE,
                report_only=report_only,
            )
        )
    return results


def has_hard_failure(results: Sequence[ReconcileResult]) -> bool:
    return any(o.outcome is Outcome.HARD for result in results for o in result.outcomes)
