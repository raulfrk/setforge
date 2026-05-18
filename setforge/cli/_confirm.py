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
from typing import TYPE_CHECKING

from prompt_toolkit.shortcuts import radiolist_dialog
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from setforge.cli._helpers import _iter_all_tracked_files
from setforge.compare import CompareStatus
from setforge.errors import ConfirmRequiresInteractive

if TYPE_CHECKING:
    from setforge.compare import CompareReport, FileCompare
    from setforge.config import Config, ResolvedProfile

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
    """One file that the --auto* operation will mutate.

    ``changed`` counts the unit the builder works in — sections for
    shared-section drift, unexpected keys for unexpected-drift entries,
    or a generic ``1`` when only a unified diff is available. We do
    not report line-level +/- because neither builder computes them.
    """

    source: Path
    dest: Path
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


def _resolve_drift_paths(
    drift_report: CompareReport,
    cfg: Config,
    resolved: ResolvedProfile,
    repo_root: Path,
) -> list[tuple[FileCompare, Path, Path]]:
    """Join ``drift_report.entries`` to tracked-file ``(sub_src, sub_dst)`` paths.

    Both ``install._build_unexpected_drift_plan`` and
    ``sync._build_capture_plan`` need the same ``name → (sub_src, sub_dst)``
    map keyed by both the expanded name and the bare ``tracked_file.src``
    string. Returns one ``(entry, sub_src, sub_dst)`` tuple per DRIFTED
    entry with drift content (either ``unexpected_drift_keys`` or
    ``diff`` non-empty). Entries with no path match fall back to the
    entry name in both positions, preserving the pre-extraction
    behavior.
    """
    paths_by_name: dict[str, tuple[Path, Path]] = {}
    for tracked_file, sub_src, sub_dst in _iter_all_tracked_files(
        cfg, resolved, repo_root
    ):
        # expand_tracked_file's naming convention: plain files use the
        # tracked_file name directly; directory entries use "name/relpath".
        # Register both the bare name and the prefixed form so lookup
        # works regardless of expansion shape.
        paths_by_name[sub_src.name] = (sub_src, sub_dst)
        paths_by_name[str(tracked_file.src)] = (sub_src, sub_dst)
    resolved_entries: list[tuple[FileCompare, Path, Path]] = []
    for entry in drift_report.entries:
        if entry.status is not CompareStatus.DRIFTED:
            continue
        if not (entry.unexpected_drift_keys or entry.diff):
            continue
        paths = paths_by_name.get(entry.name)
        if paths is None:
            sub_src = Path(entry.name)
            sub_dst = Path(entry.name)
        else:
            sub_src, sub_dst = paths
        resolved_entries.append((entry, sub_src, sub_dst))
    return resolved_entries


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
        table = Table(
            title="file changes",
            caption=(
                "counts are sections for shared-section drift, "
                "keys for unexpected-drift entries"
            ),
            show_lines=False,
        )
        table.add_column("source")
        table.add_column("dest")
        table.add_column("changes", justify="right")
        for fc in plan.file_changes:
            table.add_row(
                str(fc.source),
                str(fc.dest),
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
    # prompt_toolkit 3.0.x yes_no_dialog has no default= kwarg; radiolist
    # with default=False gives default-No behavior.
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
