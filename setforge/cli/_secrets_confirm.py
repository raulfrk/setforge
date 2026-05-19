"""Arrow-key wizard for the pre-deploy secrets scan prompt (mockup T).

Renders a Rich panel describing one :class:`SecretFinding`, then prompts
the user via ``prompt_toolkit``'s ``radiolist_dialog`` for one of three
actions (ABORT default / ALLOWLIST / SILENCE_ONE_SHOT). Esc returns
:data:`SecretAction.ABORT` — consistent with
:func:`setforge.cli._confirm.confirm_auto_operation`'s ``None``-as-abort
treatment.

The ``radiolist_dialog`` symbol is imported lazily via the module-level
``__getattr__`` so the cold path of non-wizard commands does not pay
the ~140ms ``prompt_toolkit`` import cost. Tests monkeypatch
``setforge.cli._secrets_confirm.radiolist_dialog`` via this same
attribute-access seam (mirrors :mod:`setforge.cli._confirm`).
"""

from __future__ import annotations

import sys
from typing import Any

from rich.console import Console
from rich.panel import Panel

from setforge.secrets import SecretAction, SecretFinding

__all__ = ["prompt_secret_action"]


def __getattr__(name: str) -> Any:  # noqa: ANN401 — PEP 562 module hook returns Any
    if name == "radiolist_dialog":
        from prompt_toolkit.shortcuts import radiolist_dialog

        return radiolist_dialog
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
    Esc / ``None`` from the dialog also returns ABORT (the mockup-T
    default). Tests monkeypatch
    ``setforge.cli._secrets_confirm.radiolist_dialog`` to control the
    return value.
    """
    if yes:
        return SecretAction.ABORT
    if not sys.stdin.isatty():
        return SecretAction.ABORT
    console = Console()
    _render_panel(finding, console)
    from setforge.cli import _secrets_confirm as _self  # monkeypatch seam

    choice = _self.radiolist_dialog(
        title="setforge install — potential secret detected",
        text="How would you like to proceed?",
        values=[
            (SecretAction.ABORT, "Abort install — review and remove the secret"),
            (
                SecretAction.ALLOWLIST,
                "Proceed (allowlist this snippet hash; persisted host-local)",
            ),
            (
                SecretAction.SILENCE_ONE_SHOT,
                "Proceed (silence one-shot — do NOT add to allowlist)",
            ),
        ],
        default=SecretAction.ABORT,
    ).run()
    if choice is None:
        return SecretAction.ABORT
    return choice
