"""Package-provisioning reconcile echo + gate helpers for install."""

from __future__ import annotations

import typer

from setforge.provision.dispatch import (
    ProvisioningPlan,
    apply_provisioning,
    report_provisioning,
)
from setforge.provision.ownership import PackageAction
from setforge.provision.protocol import Outcome, ProvisionOutcome, ReconcileResult


def reconcile_packages(plan: ProvisioningPlan) -> list[ReconcileResult]:
    results = apply_provisioning(plan)
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


def dry_run_packages(plan: ProvisioningPlan) -> None:
    typer.echo("=== would-be package provision ===")
    if plan.bundles:
        typer.echo(f"  bundles: {', '.join(plan.bundles)}")
    for decision in plan.ownership:
        if decision.action is PackageAction.ADOPT:
            typer.echo(
                f"  WOULD adopt {decision.item.identity.display} (metadata only)"
            )
        elif decision.action is PackageAction.HOLD:
            typer.echo(f"  HOLD {decision.item.identity.display}: {decision.detail}")
    results = report_provisioning(plan)
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
