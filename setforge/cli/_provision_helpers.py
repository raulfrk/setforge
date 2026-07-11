"""Package-provisioning reconcile echo + gate helpers for install.

Sibling to :mod:`setforge.cli._mcp_helpers`. No ``app`` import and no
``@app.command()`` registrations. The install side runs
:func:`reconcile_packages` (a thin wrapper over
:func:`setforge.provision.dispatch.run_provisioning` that echoes each
per-item outcome and returns the results for the exit gate); the dry-run
path runs :func:`dry_run_packages`, which reports the planned deltas
without applying.

There is no revert delta for provisioning: installed binaries are never
auto-pruned (uninstalling a tool the host may now depend on is worse than
leaving it), so nothing here feeds the install transition.
"""

from __future__ import annotations

import typer

from setforge.config import Config, ResolvedProfile
from setforge.provision.dispatch import run_provisioning
from setforge.provision.protocol import Outcome, ProvisionOutcome, ReconcileResult


def reconcile_packages(
    cfg: Config,
    resolved: ResolvedProfile,
) -> list[ReconcileResult]:
    """Provision every declared package / cargo binary; echo each outcome.

    Runs :func:`setforge.provision.dispatch.run_provisioning` under the real
    (apply) policy, echoes one line per recorded outcome — OK / SKIP as plain
    stdout, SOFT as a yellow stderr warning (never gates) — and returns the
    :class:`ReconcileResult`\\ s so the caller can gate the exit on any HARD
    outcome (:func:`setforge.cli.install._gate_on_provisioning_failures`).

    An unknown package ``type`` raises
    :class:`~setforge.errors.UnknownProvisionerType` from the dispatch, caught
    at the install command boundary by the global :class:`SetforgeError`
    handler.
    """
    results = run_provisioning(cfg, resolved)
    for result in results:
        for outcome in result.outcomes:
            _echo_outcome(outcome)
    return results


def _echo_outcome(outcome: ProvisionOutcome) -> None:
    """Echo one provisioning outcome (OK/SKIP plain, SOFT yellow, HARD red).

    SOFT is a warn-and-continue (a crate that will not build is host-specific);
    HARD is surfaced red here for context, but the actual non-zero exit is the
    aggregate gate's job, not this per-line echo.
    """
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
) -> None:
    """Emit the ``=== would-be package provision ===`` block (report-only).

    Runs :func:`setforge.provision.dispatch.run_provisioning` with
    ``report_only=True`` so every reconcile computes its delta and applies
    NOTHING (zero writes), then prints one ``WOULD provision`` line per planned
    identity. A declared package whose provisioner is unwired still raises
    :class:`~setforge.errors.UnknownProvisionerType` here — dry-run surfaces
    the same config error the real install would.
    """
    typer.echo("=== would-be package provision ===")
    results = run_provisioning(cfg, resolved, report_only=True)
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
