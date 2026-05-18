"""Arrow-key confirmation wizard for mutating ``setforge`` --auto* operations.

Renders a rich-formatted RISKS panel describing the planned mutation and
the revert command, then prompts arrow-key yes/no via prompt_toolkit.
Short-circuits to True if ``yes=True``; raises
:class:`ConfirmRequiresInteractive` when stdin is not a TTY and the user
did not pass ``--yes`` (mirrors the
:class:`setforge.errors.CaptureRequiresInteractive` pattern).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from prompt_toolkit.shortcuts import radiolist_dialog
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from setforge.errors import ConfirmRequiresInteractive

# Re-export for callers that import via the historical
# ``setforge.cli._confirm`` path (tests, sibling CLI modules). The
# canonical home is ``setforge.errors`` alongside
# ``CaptureRequiresInteractive``.
__all__ = [
    "AutoDirection",
    "AutoPlan",
    "ConfirmRequiresInteractive",
    "FileChange",
    "confirm_auto_operation",
]


class AutoDirection(StrEnum):
    """Direction of a mutating --auto* operation."""

    TRACKED_TO_LIVE = "tracked-to-live"
    LIVE_TO_TRACKED = "live-to-tracked"


@dataclass(slots=True, frozen=True)
class FileChange:
    """One file that the --auto* operation will mutate."""

    source: Path
    dest: Path
    added: int = 0
    removed: int = 0
    changed: int = 0


@dataclass(slots=True, frozen=True)
class AutoPlan:
    """Inventory of what a mutating --auto* operation will do.

    The confirm wizard renders this as a rich panel + risks bullets,
    then asks the user to confirm.
    """

    direction: AutoDirection
    file_changes: tuple[FileChange, ...]
    risks: tuple[str, ...]
    revert_command: str


def _render_panel(
    *, command: str, profile: str, plan: AutoPlan, console: Console
) -> None:
    """Print the risks panel + file-change table + revert hint to ``console``."""
    header = (
        f"[bold]setforge {command}[/bold] "
        f"([cyan]{plan.direction.value}[/cyan]) "
        f"profile=[yellow]{profile}[/yellow]"
    )
    console.print(Panel.fit(header, title="confirmation required"))

    if plan.file_changes:
        table = Table(title="file changes", show_lines=False)
        table.add_column("source")
        table.add_column("dest")
        table.add_column("+", justify="right")
        table.add_column("-", justify="right")
        table.add_column("Δ", justify="right")
        for fc in plan.file_changes:
            table.add_row(
                str(fc.source),
                str(fc.dest),
                str(fc.added),
                str(fc.removed),
                str(fc.changed),
            )
        console.print(table)

    if plan.risks:
        console.print("[bold red]RISKS:[/bold red]")
        for risk in plan.risks:
            console.print(f"  • {risk}")

    console.print(
        "[bold]REVERT:[/bold] if you change your mind after applying:\n"
        f"  [cyan]{plan.revert_command}[/cyan]"
    )


def confirm_auto_operation(
    *,
    command: str,
    profile: str,
    plan: AutoPlan,
    yes: bool,
    console: Console | None = None,
) -> bool:
    """Render risks panel, prompt arrow-key yes/no, return user's choice.

    Short-circuits to True if ``yes`` is set (no panel rendered). Returns
    True if ``plan`` has no changes and no risks (no-op). Otherwise raises
    :class:`ConfirmRequiresInteractive` when stdin is not a TTY, or runs
    the arrow-key prompt and returns the user's choice. ``None`` from the
    dialog (Esc) is treated as abort.
    """
    if yes:
        return True
    if not plan.file_changes and not plan.risks:
        return True
    # TTY check FIRST — non-TTY callers see only the global handler's
    # ``error: ... requires --yes`` line, not a long panel printed
    # before the raise.
    if not sys.stdin.isatty():
        raise ConfirmRequiresInteractive(
            f"setforge {command} with --auto* requires --yes when stdin is not a TTY"
        )
    if console is None:
        console = Console()
    _render_panel(command=command, profile=profile, plan=plan, console=console)
    choice = radiolist_dialog(
        title=f"setforge {command}",
        text="Proceed with the mutation above?",
        values=[
            (False, "No  — abort, no mutations"),
            (True, "Yes — apply the changes"),
        ],
        default=False,
    ).run()
    if choice is None or choice is False:
        console.print("[red]✗ aborted[/red] — no mutations applied")
        return False
    console.print("[green]✓ proceeding[/green]")
    return True
