"""Fresh-host onboarding welcome panel for ``setforge install``.

Renders an inventory of what an install on a brand-new host will touch
(tracked files / dst directories / plugins / extensions / bootstrap
stubs) and prompts the user for arrow-key consent: proceed, run dry-run
first, abort, or abort + show config docs.

The welcome fires only on fresh hosts — detected via the absence of any
transition record under :func:`setforge.transitions.transitions_root`.
A non-TTY invocation without ``--yes`` raises
:class:`WelcomeRequiresInteractive` so the install does NOT mutate state
before the user has had a chance to acknowledge the inventory.

Mirrors :mod:`setforge.cli._confirm`: lazy ``prompt_toolkit`` import via
PEP 562 module-level ``__getattr__`` so cold-start commands don't pay
the import cost; signal handlers wrap the ``radiolist_dialog`` call so
SIGINT/SIGTERM restore the terminal before exiting.
"""

from __future__ import annotations

import os
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import FrameType
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from setforge import transitions
from setforge.cli._helpers import ProfileContext, _iter_all_tracked_files
from setforge.errors import WelcomeRequiresInteractive

__all__ = [
    "OverlayDelta",
    "WelcomeChoice",
    "WelcomeInventory",
    "build_welcome_inventory",
    "is_fresh_host",
    "prompt_welcome",
    "reject_auto_on_fresh_host",
]


def __getattr__(name: str) -> Any:  # noqa: ANN401 — PEP 562 module hook returns Any
    """Lazy ``radiolist_dialog`` import.

    Mirrors :func:`setforge.cli._confirm.__getattr__` so the
    ~140ms ``prompt_toolkit`` import only fires when the dialog is
    actually rendered. Tests monkeypatch this attribute the same way
    they do for ``_confirm`` (``monkeypatch.setattr(
    "setforge.cli._welcome.radiolist_dialog", ...)``).
    """
    if name == "radiolist_dialog":
        from prompt_toolkit.shortcuts import radiolist_dialog

        return radiolist_dialog
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class WelcomeChoice(StrEnum):
    """User's selection from the welcome arrow-key picker."""

    PROCEED = "proceed"
    DRY_RUN_FIRST = "dry-run-first"
    ABORT = "abort"
    ABORT_SHOW_DOCS = "abort-show-docs"


@dataclass(slots=True, frozen=True)
class OverlayDelta:
    """Counts the local.yaml host-overlay deltas for a profile.

    Mockup G calls out six welcome categories — the sixth is "applied
    local.yaml overlays". Today the host-overlay schema (spec B,
    setforge-2by4 / preserve_user_keys overlay) is not yet implemented,
    so every field on a fresh host is zero. The struct is kept distinct
    from :class:`WelcomeInventory` so the implementation of spec B can
    populate it without rewriting the inventory contract — only
    :func:`build_welcome_inventory` would need to learn the overlay
    accessor at that point.
    """

    plugin_add: int = 0
    plugin_remove: int = 0
    extension_add: int = 0
    extension_remove: int = 0
    host_local_sections: int = 0

    @property
    def is_empty(self) -> bool:
        """Return ``True`` when every field is zero (no host overlay applied)."""
        return (
            self.plugin_add == 0
            and self.plugin_remove == 0
            and self.extension_add == 0
            and self.extension_remove == 0
            and self.host_local_sections == 0
        )


@dataclass(slots=True, frozen=True)
class WelcomeInventory:
    """Counts the welcome panel renders for a fresh-host install.

    Built once in :func:`build_welcome_inventory` and passed unchanged
    to :func:`prompt_welcome`. Counts come from the same helpers the
    install pipeline uses downstream so the inventory cannot drift from
    what the install will actually touch.
    """

    tracked_file_count: int
    dst_dirs_to_create: tuple[Path, ...]
    plugin_count: int
    extension_count: int
    bootstrap_count: int
    overlay_delta: OverlayDelta
    profile: str


def is_fresh_host() -> bool:
    """Return True when no transition record exists for any profile.

    Fresh host = the user has never run a state-changing setforge
    command on this machine. The signal is intentionally
    *transition-record absence* rather than ``~/.claude/`` absence: a
    Remote-SSH host's first VSCode-server connection eagerly creates
    ``~/.claude/`` independent of setforge, so binding fresh-host
    detection to that directory would race the editor on day 0.

    Reads :func:`transitions.transitions_root` (honors
    ``SETFORGE_STATE_DIR`` for tests / operators) and walks its
    immediate children looking for a ``meta.json``. Host-wide signal —
    no profile context needed; the welcome fires once per host across
    all profiles, not per profile.
    """
    root = transitions.transitions_root()
    if not root.is_dir():
        return True
    for child in root.iterdir():
        if not child.is_dir():
            continue
        if (child / "meta.json").is_file():
            return False
    return True


def build_welcome_inventory(ctx: ProfileContext) -> WelcomeInventory:
    """Build the :class:`WelcomeInventory` for ``ctx``'s profile.

    Walks ``ctx.resolved.tracked_files`` via
    :func:`_iter_all_tracked_files` (same iterator the deploy loop
    uses), collecting tracked-file count, unique parent directories
    that don't yet exist on the host, and the bootstrap stub count.
    The plugin and extension counts come from the resolved profile
    directly (the welcome surfaces *intent*, not the reconciler's
    pending delta — the dry-run pipeline reports that).
    """
    tracked_file_count = 0
    dst_dirs: set[Path] = set()
    for _tracked, _sub_src, sub_dst in _iter_all_tracked_files(ctx):
        tracked_file_count += 1
        parent = sub_dst.parent
        if not parent.exists():
            dst_dirs.add(parent)
    return WelcomeInventory(
        tracked_file_count=tracked_file_count,
        dst_dirs_to_create=tuple(sorted(dst_dirs)),
        plugin_count=len(ctx.resolved.claude_plugins),
        extension_count=len(ctx.resolved.extensions.include),
        bootstrap_count=len(ctx.resolved.bootstrap),
        overlay_delta=_compute_overlay_delta(ctx),
        profile=ctx.profile,
    )


def _compute_overlay_delta(ctx: ProfileContext) -> OverlayDelta:
    """Return the host-overlay delta for ``ctx``.

    The local.yaml host-overlay schema (spec B: ``preserve_user_keys``
    add/remove, plugin add/remove, host-local section overrides) is not
    yet implemented in setforge; every load returns a zero delta. When
    spec B lands, this helper grows the accessor to the parsed overlay
    block — the welcome surface contract (a zero-shaped
    :class:`OverlayDelta`) stays the same.
    """
    del ctx  # placeholder until spec B's local.yaml overlay schema lands.
    return OverlayDelta()


_DOCS_HINT: str = (
    "config docs: see docs/INSTALL.md in the setforge repo "
    "(https://github.com/raulfrk/setforge#install)"
)


def _render_panel(inventory: WelcomeInventory, *, console: Console) -> None:
    """Print the inventory panel + dirs-to-create table to ``console``."""
    header = (
        "[bold]=== fresh-host detected ===[/bold]\n"
        f"profile=[yellow]{inventory.profile}[/yellow] — "
        "no prior setforge transition records on this host"
    )
    console.print(Panel.fit(header, title="welcome"))

    if inventory.dst_dirs_to_create:
        table = Table(title="dirs that will be created", show_lines=False)
        table.add_column("path")
        for path in inventory.dst_dirs_to_create:
            table.add_row(str(path))
        console.print(table)

    summary = Table(title="this install will touch", show_lines=False)
    summary.add_column("count", justify="right")
    summary.add_column("item")
    summary.add_row(str(inventory.tracked_file_count), "tracked file(s)")
    summary.add_row(str(inventory.plugin_count), "claude plugin(s)")
    summary.add_row(str(inventory.extension_count), "vscode extension(s)")
    summary.add_row(str(inventory.bootstrap_count), "bootstrap stub file(s)")
    summary.add_row(_format_overlay_row(inventory.overlay_delta), "applied local.yaml overlay(s)")
    console.print(summary)


def _format_overlay_row(delta: OverlayDelta) -> str:
    """Return the 6th-row content for the welcome summary table.

    Renders the delta as a compact "Np+/Np-/Nx+/Nx-/Ns" breakdown so
    every channel is visible at a glance. Mockup G uses the phrase
    "1 plugin add, 1 plugin remove, 1 host_local_section"; the compact
    form is the same content in less horizontal space (which matters
    inside the count column of the Rich Table).
    """
    return (
        f"{delta.plugin_add}p+/{delta.plugin_remove}p-/"
        f"{delta.extension_add}x+/{delta.extension_remove}x-/"
        f"{delta.host_local_sections}s"
    )


def _make_signal_handler(
    prev_state: dict[int, Any],
) -> Callable[[int, FrameType | None], None]:
    """Return a SIGINT/SIGTERM handler that restores terminal state then re-raises.

    Mirrors :func:`setforge.wizard._make_signal_handler`: prompt_toolkit
    leaves the terminal in raw mode if interrupted mid-render. The
    handler resets previously-installed handlers, prints a one-line
    cancellation marker, then re-kills the process with the same signal
    so the parent shell sees the correct exit status (128 + signum).
    """

    def _handler(signum: int, _frame: FrameType | None) -> None:
        for sig, handler in prev_state.items():
            signal.signal(sig, handler)
        sys.stderr.write("\nwelcome cancelled — terminal restored\n")
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    return _handler


def _install_terminal_restore(signums: tuple[int, ...]) -> dict[int, Any]:
    """Install temporary SIGINT/SIGTERM handlers; return prior handlers."""
    prev: dict[int, Any] = {}
    handler = _make_signal_handler(prev)
    for sig in signums:
        prev[sig] = signal.signal(sig, handler)
    return prev


def _restore_terminal(prev: dict[int, Any]) -> None:
    """Restore previously-saved signal handlers."""
    for sig, handler in prev.items():
        signal.signal(sig, handler)


_PROMPT_VALUES_FULL: tuple[tuple[WelcomeChoice, str], ...] = (
    (WelcomeChoice.PROCEED, "proceed with install"),
    (WelcomeChoice.DRY_RUN_FIRST, "show dry-run first (preview without writing)"),
    (WelcomeChoice.ABORT, "abort (I want to configure local.yaml first)"),
    (WelcomeChoice.ABORT_SHOW_DOCS, "abort + show config docs"),
)
"""Initial four-choice picker. Default safe = abort."""


_PROMPT_VALUES_REPROMPT: tuple[tuple[WelcomeChoice, str], ...] = (
    (WelcomeChoice.PROCEED, "proceed with install"),
    (WelcomeChoice.ABORT, "abort (I want to configure local.yaml first)"),
    (WelcomeChoice.ABORT_SHOW_DOCS, "abort + show config docs"),
)
"""Re-prompt after dry-run finishes. Drops DRY_RUN_FIRST so the user
cannot loop the dry-run path; recursion is impossible by construction."""


def _run_dialog(
    *,
    values: tuple[tuple[WelcomeChoice, str], ...],
    default: WelcomeChoice,
) -> WelcomeChoice:
    """Run the radiolist dialog under terminal-restore signal handlers.

    Esc returns :attr:`WelcomeChoice.ABORT` — the safe default for an
    info-and-consent panel. The terminal-restore wrapper runs in
    try/finally so the prior signal handlers are restored even if
    ``radiolist_dialog`` raises.
    """
    # Defense-in-depth TTY guard: ``prompt_welcome`` already raises
    # WelcomeRequiresInteractive when stdin is non-TTY, but a future
    # caller that wires _run_dialog without that gate would silently
    # spawn the dialog against a non-TTY stdin and hang. The assert
    # documents the invariant + makes a regression loud.
    assert sys.stdin.isatty(), "_run_dialog requires a TTY; caller must gate"
    prev = _install_terminal_restore((signal.SIGINT, signal.SIGTERM))
    try:
        from setforge.cli import _welcome as _self  # monkeypatch path

        choice = _self.radiolist_dialog(
            title="setforge install — fresh host",
            text="What would you like to do?",
            values=list(values),
            default=default,
        ).run()
    finally:
        _restore_terminal(prev)
    if choice is None:
        return WelcomeChoice.ABORT
    return choice


def _emit_abort(*, show_docs: bool, console: Console) -> None:
    """Print the abort message + optional docs hint."""
    console.print("[red]✗ aborted[/red] — no mutations applied")
    if show_docs:
        console.print(_DOCS_HINT)


def prompt_welcome(
    *,
    inventory: WelcomeInventory,
    yes: bool,
    run_dry_run: Callable[[], None] | None = None,
    console: Console | None = None,
) -> WelcomeChoice:
    """Render the welcome panel; return the user's choice.

    Returns the user's :class:`WelcomeChoice`. The caller decides what
    to do with each branch (typically: ``PROCEED`` → continue the
    install; ``ABORT`` / ``ABORT_SHOW_DOCS`` → return). ``DRY_RUN_FIRST``
    is handled internally by invoking ``run_dry_run`` then re-prompting
    with three choices; the function never returns ``DRY_RUN_FIRST``.

    ``yes=True`` short-circuits to :attr:`WelcomeChoice.PROCEED` without
    rendering — the caller has already consented out-of-band. Non-TTY
    without ``yes`` raises :class:`WelcomeRequiresInteractive`: an
    info-and-consent panel needs both an information surface and a
    consent surface, and a non-TTY caller has neither.

    ``run_dry_run`` is the dry-run pipeline callback the install
    command supplies; passing ``None`` while ``DRY_RUN_FIRST`` is
    reachable raises :class:`RuntimeError` (caller bug).
    """
    if yes:
        return WelcomeChoice.PROCEED
    if not sys.stdin.isatty():
        raise WelcomeRequiresInteractive(
            "setforge install on a fresh host requires --yes when stdin "
            "is not a TTY (no consent surface available)"
        )
    if console is None:
        console = Console()
    _render_panel(inventory, console=console)
    choice = _run_dialog(values=_PROMPT_VALUES_FULL, default=WelcomeChoice.ABORT)
    if choice is WelcomeChoice.PROCEED:
        return choice
    if choice is WelcomeChoice.ABORT:
        _emit_abort(show_docs=False, console=console)
        return choice
    if choice is WelcomeChoice.ABORT_SHOW_DOCS:
        _emit_abort(show_docs=True, console=console)
        return choice
    # choice is DRY_RUN_FIRST — invoke the dry-run callback then reprompt.
    if run_dry_run is None:
        raise RuntimeError(
            "prompt_welcome: DRY_RUN_FIRST chosen but no run_dry_run "
            "callback was supplied"
        )
    run_dry_run()
    _render_panel(inventory, console=console)
    choice = _run_dialog(values=_PROMPT_VALUES_REPROMPT, default=WelcomeChoice.ABORT)
    if choice is WelcomeChoice.ABORT:
        _emit_abort(show_docs=False, console=console)
    elif choice is WelcomeChoice.ABORT_SHOW_DOCS:
        _emit_abort(show_docs=True, console=console)
    return choice


def reject_auto_on_fresh_host(*, auto: str | None) -> None:
    """Fail the install when ``--auto=`` is passed on a fresh host.

    Raises :class:`typer.Exit(2)` with the exact message from SPEC 7
    anti-pattern check 6. ``--auto=*`` reconciles existing drift; on a
    fresh host there is no drift yet, so the flag is meaningless —
    welcome is the only gate. The non-interactive escape hatch is
    ``--yes``, which skips the welcome AND the auto-resolution path.
    """
    if auto is None:
        return
    typer.secho(
        "no drift exists on fresh host; --auto only meaningful when "
        "drift exists. Use --yes to skip welcome.",
        err=True,
        fg=typer.colors.RED,
    )
    raise typer.Exit(2)
