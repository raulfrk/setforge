"""Wire declared packages / ``cargo_binaries`` into the provisioner protocol.

The install-time bridge between the config surface (``packages:`` /
``cargo_binaries:`` on a resolved profile) and the uniform provisioner
protocol (:mod:`setforge.provision.driver`). It:

1. resolves every declared item to a :class:`ProvisionItem` — a referenced
   ``packages:`` entry mapped by its per-type identity, PLUS the legacy
   ``cargo_binaries:`` list mapped to ``type=cargo`` items (so those failures
   are now RECORDED + gated instead of discarded), deduped by identity;
2. groups by ``type``, :func:`~setforge.provision.registry.build`\\ s the
   provisioner once per type, and :func:`~setforge.provision.driver.reconcile`\\ s
   each with the ADDITIVE policy (declared installs; nothing pruned);
3. returns every :class:`ReconcileResult` so the caller can echo SOFT warnings
   and gate the exit on any HARD outcome.

A declared package naming a ``type`` with no registered provisioner surfaces
as :class:`~setforge.errors.UnknownProvisionerType` from ``build()`` — a real
config error, not silently skipped. ``bundles:`` are NOT executed here (the
bundle executor lands separately); a non-empty ``bundles:`` list only emits a
"not yet supported" notice.

REPORT-no-write is honored via ``report_only`` threaded straight to
:func:`~setforge.provision.driver.reconcile` — the ``--dry-run`` path passes
``report_only=True`` and every reconcile computes its delta without applying.
"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import groupby

import typer

import setforge.provision.cargo  # noqa: F401  (registers the cargo provisioner)
from setforge.config import (
    CargoPackage,
    Config,
    GitHubReleasePackage,
    GoPackage,
    LocalPackage,
    Package,
    PythonPackage,
    ReconcilePolicy,
    ResolvedProfile,
)
from setforge.provision.driver import reconcile
from setforge.provision.protocol import (
    Identity,
    Outcome,
    ProvisionItem,
    ReconcileResult,
)
from setforge.provision.registry import build


def _package_identity(pkg: Package) -> Identity:
    """Map a per-type package model to its provisioner match identity.

    The keying attribute differs per ecosystem (cargo → crate, python →
    package, go → module, github_release → repo, local → binary); the match
    key and the display form are both that attribute, so a cargo crate maps
    to ``Identity(key=crate, display=crate)`` — the exact shape
    :class:`~setforge.provision.cargo.CargoProvisioner` invokes ``cargo
    install`` with. A new package type added to :data:`~setforge.config.Package`
    without a branch here raises, so the mapping can never silently drop one.
    """
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
    """Resolve a profile's declared items to :class:`ProvisionItem`\\ s.

    Two sources, additive:

    - each ``resolved.packages`` reference looked up in
      :attr:`Config.packages` → a :class:`ProvisionItem` of that package's
      ``type``, carrying the pydantic model in ``config`` and its version /
      checksum pin when present;
    - each ``resolved.cargo_binaries`` crate → a ``type=cargo``
      :class:`ProvisionItem` (backward-compat; these now flow through the
      provisioner too — the discarded-failure bug fix).

    Deduped by identity: a crate declared BOTH as a ``cargo_binaries`` entry
    and a ``cargo`` package appears once (first occurrence wins). Packages are
    resolved first so an explicit ``packages:`` entry (which may carry a
    version pin) wins over the bare ``cargo_binaries`` string.
    """
    items: list[ProvisionItem] = []
    seen: set[Identity] = set()

    def _add(item: ProvisionItem) -> None:
        if item.identity in seen:
            return
        seen.add(item.identity)
        items.append(item)

    for ref in resolved.packages:
        pkg = cfg.packages[ref]
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
) -> list[ReconcileResult]:
    """Reconcile every declared package / cargo binary through its provisioner.

    Resolves the items (:func:`resolve_provision_items`), notices any
    ``bundles:`` (skipped with a message — the bundle executor lands
    separately), then groups by ``type`` and reconciles each group under the
    ADDITIVE policy (packages install declared, prune nothing). ``report_only``
    threads to :func:`~setforge.provision.driver.reconcile` so a ``--dry-run``
    computes each delta without applying.

    An unknown ``type`` raises :class:`~setforge.errors.UnknownProvisionerType`
    from :func:`~setforge.provision.registry.build` — a declared package whose
    provisioner has not been wired yet is a real config error, surfaced (not
    silently skipped). Returns every :class:`ReconcileResult` for the caller to
    warn on SOFT and gate on HARD.
    """
    if resolved.bundles:
        typer.secho(
            "note: bundles not yet supported (coming with the bundle model); "
            f"skipping {len(resolved.bundles)} declared bundle(s): "
            f"{', '.join(resolved.bundles)}",
            err=True,
            fg=typer.colors.YELLOW,
        )
    items = resolve_provision_items(cfg, resolved)
    results: list[ReconcileResult] = []
    # Sort so groupby sees each type contiguously; build() one provisioner per
    # type and reconcile its slice under the always-ADDITIVE package policy.
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
    """True iff any outcome across ``results`` is :attr:`Outcome.HARD`."""
    return any(o.outcome is Outcome.HARD for result in results for o in result.outcomes)
