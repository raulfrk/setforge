"""Package-provisioning reconcile echo + gate helpers for install."""

from __future__ import annotations

import typer

from setforge.config import Config, ResolvedProfile
from setforge.lockfile import LockFile
from setforge.provision.dispatch import (
    ProvisioningPlan,
    apply_provisioning,
    report_provisioning,
    run_provisioning,
)
from setforge.provision.protocol import Outcome, ProvisionOutcome, ReconcileResult


def reconcile_packages(
    cfg: Config,
    resolved: ResolvedProfile,
    *,
    lock: LockFile | None = None,
    plan: ProvisioningPlan | None = None,
) -> list[ReconcileResult]:
    results = (
        apply_provisioning(plan)
        if plan is not None
        else run_provisioning(cfg, resolved, lock=lock)
    )
    for result in results:
        for outcome in result.outcomes:
            _echo_outcome(outcome)
    return results


def _echo_outcome(outcome: ProvisionOutcome) -> None:
    name = outcome.item.identity.display
    detail = f" — {outcome.detail}" if outcome.detail else ""
    match outcome.outcome:
        case Outcome.OK:
            typer.echo(f"provisioned {name}{detail}")
        case Outcome.SKIP:
            typer.echo(f"provision: {name} already present (skip)")
        case Outcome.SOFT:
            typer.secho(
                f"warning: skipped {name}{detail}", err=True, fg=typer.colors.YELLOW
            )
        case Outcome.HARD:
            typer.secho(
                f"FAILED provision {name}{detail}", err=True, fg=typer.colors.RED
            )


def dry_run_packages(
    cfg: Config,
    resolved: ResolvedProfile,
    *,
    plan: ProvisioningPlan | None = None,
) -> None:
    typer.echo("=== would-be package provision ===")
    results = (
        report_provisioning(plan)
        if plan is not None
        else run_provisioning(cfg, resolved, report_only=True)
    )
    planned = [
        identity
        for result in results
        for identity in (*result.delta.installed, *result.delta.activated)
    ]
    if not planned:
        typer.echo("  nothing to provision")
        return
    for identity in planned:
        typer.echo(f"  WOULD provision {identity.display}")
