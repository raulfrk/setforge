"""Arrow-key confirm-explain-redo wizard for ``setforge revert`` (mockup A).

Renders the full revert plan — transition metadata, per-file diff
summaries, plugin / extension reconciles, RISKS panel, and REDO
instructions — then prompts arrow-key abort / apply / apply+editor
via prompt_toolkit. Short-circuits to APPLY when ``yes=True``; raises
:class:`ConfirmRequiresInteractive` when stdin is not a TTY and the
user did not pass ``--yes`` (mirrors :func:`confirm_auto_operation`).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel

from setforge.errors import ConfirmRequiresInteractive

# ``prompt_toolkit.shortcuts.radiolist_dialog`` is imported lazily via
# the module-level ``__getattr__`` below so non-interactive callers (and
# the cold-start path of ``setforge --help`` / ``validate`` / ``compare``)
# never pay the ~140ms cost. The TUI fires only when ``yes=False`` and
# stdin is a TTY. Module-level ``__getattr__`` keeps the attribute-on-
# module access path that the test suite's ``monkeypatch.setattr(
# "setforge.cli._revert_confirm.radiolist_dialog", ...)`` relies on.
# Mirrors the trampoline in ``setforge/cli/_confirm.py``.


def __getattr__(name: str) -> Any:  # noqa: ANN401 — PEP 562 module hook returns Any
    if name == "radiolist_dialog":
        from prompt_toolkit.shortcuts import radiolist_dialog

        return radiolist_dialog
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ExtensionOperation",
    "ExtensionReconcile",
    "FileMutation",
    "PluginOperation",
    "PluginReconcile",
    "RevertChoice",
    "RevertPlan",
    "confirm_revert_operation",
]


class RevertChoice(StrEnum):
    """User's choice at the revert confirm wizard."""

    ABORT = "abort"
    APPLY = "apply"
    APPLY_WITH_EDITOR = "apply-with-editor"


class PluginOperation(StrEnum):
    """Forward plugin operation recorded by an install/sync transition.

    On revert, ``ENABLED`` is reversed to disable and ``DISABLED`` to
    enable — see ``_render_plugins_section`` for the panel marker
    dispatch.
    """

    ENABLED = "enabled"
    DISABLED = "disabled"


class ExtensionOperation(StrEnum):
    """Forward extension operation recorded by an install/sync transition.

    On revert, ``INSTALLED`` is reversed to uninstall and ``UNINSTALLED``
    to install — see ``_render_extensions_section`` for the panel
    marker dispatch.
    """

    INSTALLED = "installed"
    UNINSTALLED = "uninstalled"


@dataclass(slots=True, frozen=True)
class FileMutation:
    """One file the revert will mutate.

    ``diff_summary`` is the human-readable line-delta string
    (e.g. ``"+14 -3"``) shown in the per-file listing. ``user_edit_collision``
    is the sorted tuple of ``(start_line, end_line)`` inclusive ranges that
    were edited live since the transition was recorded AND overlap with the
    reverse-patch hunks. Empty tuple means no collision risk has been
    pre-detected for this file.

    **v1 contract.** ``user_edit_collision`` is always empty in v1 —
    :func:`setforge.cli.revert._build_revert_plan` does not pre-compute
    overlap ranges. Real collision detection happens at apply time
    via ``patch --dry-run -R`` inside
    :func:`setforge.transitions.apply_patch_reverse`, which refuses
    cleanly on conflict. The empty default exists so the panel can
    surface conflict ranges in a future version that pre-walks the
    reverse-hunks against live state (e.g. diff vs. recorded baseline).
    """

    path: Path
    diff_summary: str
    user_edit_collision: tuple[tuple[int, int], ...] = ()


@dataclass(slots=True, frozen=True)
class PluginReconcile:
    """One plugin operation the install/sync transition performed.

    On revert we will invert ``operation`` — a :attr:`PluginOperation.ENABLED`
    plugin becomes disabled, a :attr:`PluginOperation.DISABLED` plugin
    becomes re-enabled. ``source`` is the human-readable provenance hint
    (e.g. ``"[from local.yaml]"``) surfaced in the panel listing.
    """

    plugin_id: str
    operation: PluginOperation
    source: str


@dataclass(slots=True, frozen=True)
class ExtensionReconcile:
    """One VSCode extension operation the install/sync transition performed.

    On revert we will invert ``operation`` — :attr:`ExtensionOperation.INSTALLED`
    becomes uninstalled, :attr:`ExtensionOperation.UNINSTALLED` becomes
    re-installed. ``source`` is the provenance hint surfaced in the
    panel listing.
    """

    extension_id: str
    operation: ExtensionOperation
    source: str


@dataclass(slots=True, frozen=True)
class RevertPlan:
    """Snapshot of what ``setforge revert`` will do for one transition.

    Built by :func:`setforge.cli.revert._build_revert_plan` from the
    on-disk transition dir; rendered by :func:`confirm_revert_operation`.
    """

    transition_id: str
    transition_type: str
    profile: str
    age_human: str
    file_mutations: tuple[FileMutation, ...] = ()
    plugin_reconciles: tuple[PluginReconcile, ...] = ()
    extension_reconciles: tuple[ExtensionReconcile, ...] = ()
    redo_command: str = ""


def _format_collision_ranges(ranges: tuple[tuple[int, int], ...]) -> str:
    """Format ``((14, 22), (47, 49))`` as ``"lines 14-22, 47-49"``."""
    if not ranges:
        return ""
    return "lines " + ", ".join(
        f"{start}-{end}" if start != end else f"{start}" for start, end in ranges
    )


def _render_files_section(plan: RevertPlan, console: Console) -> None:
    """Render the ``files affected (N)`` listing."""
    console.print(f"  files affected ({len(plan.file_mutations)}):")
    for fm in plan.file_mutations:
        console.print(f"    M  {fm.path}  (line-delta: {fm.diff_summary})")


def _render_plugins_section(plan: RevertPlan, console: Console) -> None:
    """Render the ``plugins reconciled (N)`` listing."""
    if not plan.plugin_reconciles:
        return
    console.print(f"  plugins reconciled ({len(plan.plugin_reconciles)}):")
    for pr in plan.plugin_reconciles:
        marker = "+" if pr.operation is PluginOperation.ENABLED else "-"
        console.print(f"    {marker} {pr.plugin_id}  {pr.source}")


def _render_extensions_section(plan: RevertPlan, console: Console) -> None:
    """Render the ``extensions reconciled (N)`` listing."""
    if not plan.extension_reconciles:
        return
    console.print(f"  extensions reconciled ({len(plan.extension_reconciles)}):")
    for er in plan.extension_reconciles:
        marker = "+" if er.operation is ExtensionOperation.INSTALLED else "-"
        console.print(f"    {marker} {er.extension_id}  {er.source}")


def _render_risks_section(plan: RevertPlan, console: Console) -> None:
    """Render the RISKS panel with patch-reverse-collision callouts."""
    console.print("[bold red]=== RISKS ===[/bold red]")
    collisions = [fm for fm in plan.file_mutations if fm.user_edit_collision]
    if collisions:
        console.print(
            "  - Live edits since the transition collide with the reverse-patch on:"
        )
        for fm in collisions:
            console.print(
                f"      {fm.path} ({_format_collision_ranges(fm.user_edit_collision)})"
            )
        console.print(
            "    setforge will refuse cleanly on collision; "
            "resolve manually then re-run."
        )
    else:
        console.print(
            "  - Collision check happens at apply time "
            "(``patch --dry-run -R`` inside apply_patch_reverse); revert "
            "uses patch-reverse, not whole-file overwrite, and refuses "
            "cleanly if any reverse-hunk collides with a live edit."
        )
    if plan.plugin_reconciles or plan.extension_reconciles:
        console.print(
            "  - Plugin/extension re-disable triggers actual claude/code CLI calls "
            "— slow on flaky network; up to ~30s."
        )


def _render_panel(plan: RevertPlan, console: Console) -> None:
    """Print the full mockup-A panel to ``console``."""
    header = f"[bold]setforge revert[/bold] profile=[yellow]{plan.profile}[/yellow]"
    console.print(Panel.fit(header, title="resolving most-recent transition"))
    console.print(f"transition: {plan.transition_id}")
    console.print(f"  type:    {plan.transition_type}")
    console.print(f"  profile: {plan.profile}")
    console.print(f"  age:     {plan.age_human}")
    _render_files_section(plan, console)
    _render_plugins_section(plan, console)
    _render_extensions_section(plan, console)
    console.print("[bold]=== what 'revert' will do ===[/bold]")
    console.print(
        f"  Reverse the {len(plan.file_mutations)} file mutation(s) "
        "using stored patch-reverse data."
    )
    if plan.plugin_reconciles:
        console.print(f"  Reverse {len(plan.plugin_reconciles)} plugin reconcile(s).")
    if plan.extension_reconciles:
        console.print(
            f"  Reverse {len(plan.extension_reconciles)} extension reconcile(s)."
        )
    _render_risks_section(plan, console)
    console.print("[bold]=== REDO (after revert lands) ===[/bold]")
    console.print(
        "  setforge revert acts as an inverse op. To REDO this "
        f"{plan.transition_type} — run:"
    )
    console.print(f"      [cyan]{plan.redo_command}[/cyan]")
    console.print("  again. Second invocation re-applies the original mutations.")


def _prompt_choice(plan: RevertPlan) -> RevertChoice:
    """Drive ``radiolist_dialog`` and translate its return into a RevertChoice.

    Esc / Ctrl-C returns ``None`` from prompt_toolkit; some versions
    return ``False`` on cancel — both map to :attr:`RevertChoice.ABORT`
    per the wizard-discipline invariant.
    """
    # Resolve through the module-level ``__getattr__`` (lazy prompt_toolkit
    # import); tests monkeypatch the same attribute path.
    from setforge.cli import _revert_confirm as _self

    choice = _self.radiolist_dialog(
        title=f"setforge revert ({plan.transition_type})",
        text="What should setforge do?",
        values=[
            (RevertChoice.ABORT, "no, abort (default — safe)"),
            (RevertChoice.APPLY, "yes, revert"),
            (
                RevertChoice.APPLY_WITH_EDITOR,
                "yes + open editor before applying",
            ),
        ],
        default=RevertChoice.ABORT,
    ).run()
    if choice is None or choice is False:
        return RevertChoice.ABORT
    if not isinstance(choice, RevertChoice):
        # Defensive: a monkeypatched dialog could return a stray value.
        return RevertChoice.ABORT
    return choice


def confirm_revert_operation(
    *,
    plan: RevertPlan,
    yes: bool,
    console: Console | None = None,
) -> RevertChoice:
    """Render the explain+REDO panel and prompt arrow-key choice.

    Short-circuits to :attr:`RevertChoice.APPLY` if ``yes`` is set (no
    panel rendered). Raises :class:`ConfirmRequiresInteractive` when
    stdin is not a TTY and ``yes`` was not passed (mirrors
    :func:`confirm_auto_operation`). Returns
    :attr:`RevertChoice.ABORT` on Esc / Ctrl-C (None choice).
    """
    if yes:
        return RevertChoice.APPLY
    # TTY check FIRST — non-TTY callers see only the global handler's
    # ``error: ... requires --yes`` line, not a long panel printed
    # before the raise.
    if not sys.stdin.isatty():
        raise ConfirmRequiresInteractive(
            "setforge revert requires --yes when stdin is not a TTY"
        )
    if console is None:
        console = Console()
    _render_panel(plan, console)
    choice = _prompt_choice(plan)
    if choice is RevertChoice.ABORT:
        console.print("[red]aborted[/red] — no mutations applied")
    return choice
