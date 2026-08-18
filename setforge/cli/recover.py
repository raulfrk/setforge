"""Inspect and explicitly recover an interrupted SetForge operation."""

from __future__ import annotations

import sys

import typer

from setforge import operations
from setforge.cli import _PROFILE_OPTION, app
from setforge.errors import ConfirmRequiresInteractive, SetforgeError
from setforge.locking import mutation_locks


@app.command("recover")
def recover(
    profile: str = _PROFILE_OPTION,
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Restore every executable pre-operation snapshot.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Confirm recovery non-interactively.",
    ),
    acknowledge_manual: bool = typer.Option(
        False,
        "--acknowledge-manual",
        help="Clear a manual-remediation record after completing its instructions.",
    ),
) -> None:
    """Inspect or recover the active write-ahead operation for a profile."""
    journal = operations.load(profile)
    _render(journal)
    if acknowledge_manual:
        _acknowledge_manual(journal, yes=yes)
        return
    if not apply:
        typer.echo(f"recover with: setforge recover --profile={profile} --apply")
        return
    if journal.phase is operations.OperationPhase.MANUAL:
        raise SetforgeError(
            "automatic compensation already completed; follow the listed manual "
            "remediation, then use --acknowledge-manual --yes"
        )
    if not yes:
        if not sys.stdin.isatty():
            raise ConfirmRequiresInteractive(
                "setforge recover --apply requires --yes when stdin is not a TTY"
            )
        if not typer.confirm(
            "restore the captured pre-operation state?", default=False
        ):
            typer.echo("aborted: recovery journal retained")
            return

    _apply_recovery(journal)


def _acknowledge_manual(journal: operations.OperationJournal, *, yes: bool) -> None:
    if journal.phase is not operations.OperationPhase.MANUAL:
        raise SetforgeError("only a manual-remediation journal can be acknowledged")
    if not yes:
        raise ConfirmRequiresInteractive(
            "setforge recover --acknowledge-manual requires --yes"
        )
    with mutation_locks(
        resources=journal.resources_lock,
        config_dir=journal.config_dir,
        profiles=operations.locked_profiles(journal),
        allow_operation_id=journal.operation_id,
    ):
        operations.complete(journal)
    typer.echo(f"acknowledged manual recovery for {journal.operation_id}")


def _apply_recovery(journal: operations.OperationJournal) -> None:
    """Recover one confirmed journal under its recorded lock envelope."""
    with mutation_locks(
        resources=journal.resources_lock,
        config_dir=journal.config_dir,
        profiles=operations.locked_profiles(journal),
        allow_operation_id=journal.operation_id,
    ):
        current = operations.load(journal.profile)
        if current.operation_id != journal.operation_id:
            raise SetforgeError("operation journal changed before recovery; retry")
        operations.validate_recovery(current)
        operations.recover_adapters(current)
        recovered = operations.finish_recovery(operations.recover_files(current))
        manual = tuple(
            checkpoint
            for checkpoint in recovered.checkpoints
            if checkpoint.kind is operations.CheckpointKind.IRREVERSIBLE
        )
        if manual:
            operations.mark_manual(recovered)
            typer.secho(
                "automatic recovery complete; manual remediation remains:",
                err=True,
                fg=typer.colors.YELLOW,
            )
            for checkpoint in manual:
                typer.secho(
                    f"  {checkpoint.name}: {checkpoint.recovery}",
                    err=True,
                    fg=typer.colors.YELLOW,
                )
            raise typer.Exit(code=1)
        operations.complete(recovered)
    typer.echo(f"recovered operation {journal.operation_id}")


def _render(journal: operations.OperationJournal) -> None:
    typer.echo(f"operation: {journal.operation_id}")
    typer.echo(f"command:   {journal.command}")
    typer.echo(f"profile:   {journal.profile}")
    typer.echo(f"phase:     {journal.phase.value}")
    if not journal.checkpoints:
        typer.echo("checkpoints: none started")
        return
    typer.echo("checkpoints:")
    for checkpoint in journal.checkpoints:
        status = "complete" if checkpoint.completed else "uncertain"
        typer.echo(f"  {status:9} {checkpoint.kind.value:13} {checkpoint.name}")
