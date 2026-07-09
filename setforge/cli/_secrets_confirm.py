"""Arrow-key wizard for the pre-deploy secrets scan prompt (mockup T).

Renders a Rich panel describing one :class:`SecretFinding`, then prompts
the user via the themed ``button_bar`` widget for one of three actions
(ABORT default / ALLOWLIST / SILENCE_ONE_SHOT). Esc returns
:data:`SecretAction.ABORT` — consistent with
:func:`setforge.cli._confirm.confirm_auto_operation`'s :data:`CANCEL`-as-abort
treatment.
"""

from __future__ import annotations

import sys
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel

from setforge.secrets import SecretAction, SecretFinding

__all__ = ["prompt_secret_action"]


def __getattr__(name: str) -> Any:  # noqa: ANN401 — PEP 562 module hook returns Any
    if name == "button_bar":
        from setforge.ui.widgets import button_bar

        return button_bar
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _render_panel(finding: SecretFinding, console: Console) -> None:
    """Render the mockup-T panel describing a single finding."""
    body = (
        f"[bold]rule:[/bold]      {finding.secret_kind}\n"
        f"[bold]file:[/bold]      {finding.file_path}:{finding.line_number}\n"
        f"[bold]snippet:[/bold]   {finding.snippet!r}"
    )
    console.print(
        Panel.fit(
            body,
            title="[yellow]⚠ POTENTIAL SECRET DETECTED[/yellow]",
            border_style="yellow",
        )
    )


def prompt_secret_action(finding: SecretFinding, yes: bool = False) -> SecretAction:
    """Render mockup-T panel, prompt arrow-key action, return user's choice.

    Short-circuits to :data:`SecretAction.ABORT` when ``yes=True`` —
    non-interactive callers MUST NOT silently bypass a secret finding
    (auto-bypass would defeat the defense-in-depth goal of the scan).
    Non-TTY stdin also returns ABORT, emitting a yellow stderr warning
    first so the abort is not silent. Esc / :data:`CANCEL` from the widget
    also returns ABORT (the mockup-T default). Tests monkeypatch
    ``setforge.cli._secrets_confirm.button_bar`` to control the
    return value.
    """
    if yes:
        return SecretAction.ABORT
    if not sys.stdin.isatty():
        typer.secho(
            "warning: potential secret detected but no TTY is available to "
            "confirm — aborting install",
            err=True,
            fg=typer.colors.YELLOW,
        )
        return SecretAction.ABORT
    console = Console(stderr=True)
    _render_panel(finding, console)
    from setforge.cli import _secrets_confirm as _self  # monkeypatch seam
    from setforge.ui.widgets import CANCEL, Button

    choice = _self.button_bar(
        [
            Button("Abort install — review and remove the secret", SecretAction.ABORT),
            Button(
                "Proceed (allowlist this snippet hash; persisted host-local)",
                SecretAction.ALLOWLIST,
            ),
            Button(
                "Proceed (silence one-shot — do NOT add to allowlist)",
                SecretAction.SILENCE_ONE_SHOT,
            ),
        ],
        title="setforge install — potential secret detected",
        body="How would you like to proceed?",
        initial=0,
    )
    if choice is CANCEL:
        return SecretAction.ABORT
    return choice
