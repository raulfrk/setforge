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

from setforge.cli._output import make_console
from setforge.errors import ConfirmRequiresInteractive


def __getattr__(name: str) -> Any:  # noqa: ANN401 — PEP 562 module hook returns Any
    if name == "button_bar":
        from setforge.ui.widgets import button_bar

        return button_bar
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ExtensionOperation",
    "ExtensionReconcile",
    "FileMutation",
    "MultiStepRevertPlan",
    "PluginOperation",
    "PluginReconcile",
    "RevertChoice",
    "RevertPlan",
    "confirm_multi_step_revert_operation",
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
    (e.g. ``"+14 -3"``) shown in the per-file listing. Collision detection
    happens at apply time via ``patch --dry-run -R`` inside
    :func:`setforge.transitions.apply_patch_reverse`, which refuses
    cleanly on conflict.

    ``mode_restore`` is a human-readable note (e.g. ``"mode → 0o600"``)
    set when the revert will also chmod this path back to its pre-install
    permission bits (the content patch carries bytes only, so a mode-only
    install would otherwise be silently reverted on the mode axis). ``None``
    when the install changed no mode for this path.
    """

    path: Path
    diff_summary: str
    mode_restore: str | None = None


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


def _render_files_section(plan: RevertPlan, console: Console) -> None:
    """Render the ``files affected (N)`` listing."""
    console.print(f"  files affected ({len(plan.file_mutations)}):")
    for fm in plan.file_mutations:
        suffix = f", {fm.mode_restore}" if fm.mode_restore else ""
        console.print(f"    M  {fm.path}  (line-delta: {fm.diff_summary}{suffix})")


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
    """Render the RISKS panel with the whole-file drift callout."""
    console.print("[bold red]=== RISKS ===[/bold red]")
    console.print(
        "  - Revert restores whole files: each must still hold what the "
        "transition left (bytes, mode, link target; timestamps are ignored). "
        "A file edited since refuses the revert, naming it, before anything "
        "is written."
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
        f"  Restore the {len(plan.file_mutations)} file mutation(s) "
        "from the recorded pre-transition images."
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
    """Drive ``button_bar`` and translate its return into a RevertChoice.

    Esc / Ctrl-C returns :data:`CANCEL` from the widget; a monkeypatched
    stub could still return ``False`` — both map to :attr:`RevertChoice.ABORT`
    per the wizard-discipline invariant.
    """
    from setforge.cli import _revert_confirm as _self
    from setforge.ui.widgets import CANCEL, Button

    choice = _self.button_bar(
        [
            Button("no, abort (default — safe)", RevertChoice.ABORT),
            Button("yes, revert", RevertChoice.APPLY),
            Button(
                "yes + open editor before applying",
                RevertChoice.APPLY_WITH_EDITOR,
            ),
        ],
        title=f"setforge revert ({plan.transition_type})",
        body="What should setforge do?",
        initial=0,
    )
    if choice is CANCEL or choice is False:
        return RevertChoice.ABORT
    if not isinstance(choice, RevertChoice):
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
        console = make_console(stderr=True)
    _render_panel(plan, console)
    choice = _prompt_choice(plan)
    if choice is RevertChoice.ABORT:
        console.print("[red]aborted[/red] — no mutations applied")
    return choice


@dataclass(slots=True, frozen=True)
class MultiStepRevertPlan:
    """Multi-step rollback covering N transitions (``revert --to-before``).

    Newest-first ordering matches the apply order — the user sees the
    most-recent transition (which reverts first) at the top of the
    summary, mirroring ``transitions list``'s default. The wrapped
    :class:`RevertPlan` per step keeps the rich per-step data
    (file mutations, plugin/extension reconciles) available without
    re-walking the on-disk transition dir at render time.
    """

    profile: str
    steps: tuple[RevertPlan, ...]

    def __post_init__(self) -> None:
        if not self.steps:
            raise ValueError("MultiStepRevertPlan must have at least one step")


def _render_multi_step_panel(plan: MultiStepRevertPlan, console: Console) -> None:
    """Print the multi-step summary panel; one row per transition step."""
    header = (
        f"[bold]setforge revert[/bold] profile=[yellow]{plan.profile}[/yellow] "
        f"(reverts {len(plan.steps)} transitions)"
    )
    console.print(Panel.fit(header, title="multi-step revert (--to-before)"))
    for i, step in enumerate(plan.steps, start=1):
        files = len(step.file_mutations)
        plugins = len(step.plugin_reconciles)
        exts = len(step.extension_reconciles)
        console.print(
            f"  step {i}/{len(plan.steps)}: {step.transition_id}  "
            f"type={step.transition_type}  age={step.age_human}  "
            f"files={files} plugins={plugins} ext={exts}"
        )
    console.print("[bold]=== what 'revert --to-before' will do ===[/bold]")
    console.print(
        f"  Restore each step's files in newest-first order "
        f"({len(plan.steps)} steps total)."
    )
    console.print(
        "  Every step's files have been checked against live; a failure on "
        "a later step (rare) rolls the steps already applied back and exits "
        "1. Abort is still safe here — nothing has been written yet."
    )


def _prompt_multi_step_choice(plan: MultiStepRevertPlan) -> RevertChoice:
    """Drive ``button_bar`` for the multi-step plan; map to RevertChoice.

    Esc / Ctrl-C / unknown returns map to :attr:`RevertChoice.ABORT`
    per the wizard-discipline invariant — the safe default for a
    destructive multi-step op.
    """
    from setforge.cli import _revert_confirm as _self
    from setforge.ui.widgets import CANCEL, Button

    choice = _self.button_bar(
        [
            Button("no, abort (default — safe)", RevertChoice.ABORT),
            Button(
                f"yes, revert these {len(plan.steps)} transitions",
                RevertChoice.APPLY,
            ),
        ],
        title=f"setforge revert --to-before ({len(plan.steps)} steps)",
        body="What should setforge do?",
        initial=0,
    )
    if choice is CANCEL or choice is False:
        return RevertChoice.ABORT
    if not isinstance(choice, RevertChoice):
        return RevertChoice.ABORT
    return choice


def confirm_multi_step_revert_operation(
    *,
    plan: MultiStepRevertPlan,
    yes: bool,
    console: Console | None = None,
) -> RevertChoice:
    """Render the multi-step summary and prompt the user once for all N steps.

    Mirrors :func:`confirm_revert_operation`'s contract: short-circuits to
    :attr:`RevertChoice.APPLY` when ``yes`` is set (no panel rendered);
    raises :class:`ConfirmRequiresInteractive` on non-TTY without ``yes``;
    returns :attr:`RevertChoice.ABORT` on Esc / Ctrl-C. One wizard
    invocation covers all N transitions — never N separate prompts.
    """
    if yes:
        return RevertChoice.APPLY
    if not sys.stdin.isatty():
        raise ConfirmRequiresInteractive(
            "setforge revert --to-before requires --yes when stdin is not a TTY"
        )
    if console is None:
        console = make_console(stderr=True)
    _render_multi_step_panel(plan, console)
    choice = _prompt_multi_step_choice(plan)
    if choice is RevertChoice.ABORT:
        console.print("[red]aborted[/red] — no mutations applied")
    return choice
