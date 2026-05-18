"""``setforge section`` subcommand — user-section marker management.

``setforge section emit <semantics> <name>`` prints a paste-ready
marker pair to stdout for files setforge cannot safely auto-edit.

``setforge section add`` edits a tracked markdown file
(``.md`` / ``.markdown`` only) to insert a marker pair at a
user-picked anchor line. Scripted via flags, or interactive via
prompt_toolkit dialogs + the bespoke anchor-line TUI picker.
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

import typer
from rich.console import Console

from setforge._editor import run_editor
from setforge.cli import _CONFIG_OPTION, _PROFILE_OPTION, _resolve_config_arg, app
from setforge.cli._anchor_picker import pick_anchor_line
from setforge.config import load_config
from setforge.sections import SectionSemantics, extract_sections

section_app: typer.Typer = typer.Typer(
    help="Manage user-section markers in tracked markdown files.",
    no_args_is_help=True,
)
app.add_typer(section_app, name="section")

_NAME_PATTERN: re.Pattern[str] = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_MARKDOWN_SUFFIXES: frozenset[str] = frozenset({".md", ".markdown"})
_VALID_SEMANTICS: frozenset[str] = frozenset(s.value for s in SectionSemantics)
_VALID_BODY_SOURCES: frozenset[str] = frozenset({"empty", "editor", "file"})


def _validate_name(name: str) -> None:
    if not _NAME_PATTERN.fullmatch(name):
        raise typer.BadParameter(
            f"name {name!r} must match {_NAME_PATTERN.pattern} "
            "(lowercase letter start, lowercase/digit/dash, max 63 chars)"
        )


def _validate_semantics(semantics: str) -> None:
    if semantics not in _VALID_SEMANTICS:
        raise typer.BadParameter(
            f"semantics {semantics!r} not in {{shared, host-local}}"
        )


def _format_marker_pair_with_body(*, semantics: str, name: str, body: str) -> str:
    """Render a marker pair around ``body``.

    Always emits exactly one newline between the start marker and the
    body, and exactly one newline between the body and the end marker.
    Empty bodies render a single blank line between the markers.
    """
    body_block = (body if body.endswith("\n") else body + "\n") if body else "\n"
    return (
        f"<!-- setforge:user-section start {semantics} {name} -->\n"
        f"{body_block}"
        f"<!-- setforge:user-section end {semantics} {name} -->\n"
    )


@section_app.command("emit")
def section_emit(
    semantics: str = typer.Argument(..., help="shared|host-local"),
    name: str = typer.Argument(..., help="lowercase-with-dashes section name"),
) -> None:
    """Print a paste-ready marker pair to stdout."""
    _validate_semantics(semantics)
    _validate_name(name)
    sys.stdout.write(_format_marker_pair_with_body(semantics=semantics, name=name, body=""))
