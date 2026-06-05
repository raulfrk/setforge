"""Install-time interactive wizard for disposition merge conflicts.

Builds the :data:`~setforge.disposition_merge.ConflictResolver` an interactive
install injects into :func:`setforge.disposition_merge.resolve_file` so each
conflicting hunk / path is resolved one at a time at the keyboard, mirroring the
UX of the shared user-section wizard (:mod:`setforge.section_wizard`).

The factory :func:`make_wizard_resolver` returns the callable; per conflict it
renders the two diverging sides (``ours = live`` vs ``theirs = tracked``),
prompts ``[k]eep / [t]ake / [e]dit / [s]kip`` via
:func:`setforge.wizard.read_one_choice`, and maps the keypress to a
:class:`~setforge.disposition_merge.ConflictResolution`:

- ``k`` -> ``KEEP_OURS``    (keep the live side)
- ``t`` -> ``TAKE_THEIRS``  (take the tracked side)
- ``s`` -> ``SKIP``         (keep live, defer re-baselining)
- ``e`` -> ``EDIT``         (open ``$EDITOR`` seeded with ours; read back)

Conflict kind is dispatched by ``isinstance``: a
:class:`~setforge.markdown_merge.LineConflict` is line-based (a ``.md``
tmpfile, lines read back with terminators), a
:class:`~setforge.structural_merge.PathConflict` is structural (a ``.yaml``
tmpfile carrying a serialized scalar / list / dict, parsed back), and a
:class:`~setforge.scalar_merge.ScalarConflict` is a SHALLOW
``preserve_user_keys`` scalar (a ``.yaml`` tmpfile carrying a single scalar,
parsed back and REJECTED if the edit yields a mapping / list). A structural or
scalar edit whose tmpfile does not parse re-prompts the whole conflict rather
than crashing. The :data:`~setforge.scalar_merge.ABSENT` sentinel (a side where
the key is missing) renders as ``(absent)`` and seeds an empty edit buffer.

POSIX-only: the editor sub-action shells out to ``$EDITOR`` via
:func:`setforge._editor.run_editor`; the single-keypress prompter is
:func:`setforge.wizard.read_one_choice` (with its non-tty line-buffered
fallback for tests).
"""

from __future__ import annotations

import io
import tempfile
from pathlib import Path

from rich.console import Console
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from setforge._editor import run_editor
from setforge.disposition_merge import (
    ConflictChoice,
    ConflictResolution,
    ConflictResolver,
)
from setforge.markdown_merge import LineConflict
from setforge.scalar_merge import ABSENT, ScalarConflict
from setforge.structural_merge import PathConflict
from setforge.wizard import read_one_choice

__all__ = ["make_wizard_resolver"]


def make_wizard_resolver(console: Console | None = None) -> ConflictResolver:
    """Return a :data:`ConflictResolver` that prompts the user per conflict.

    ``console`` is the Rich Console for conflict rendering (defaults to a fresh
    ``Console()``). The returned callable is invoked once per conflict, in
    document order, by :func:`setforge.disposition_merge.resolve_file`; it
    renders the conflict, prompts ``k/t/e/s``, and returns the matching
    :class:`~setforge.disposition_merge.ConflictResolution`.
    """
    active_console = console if console is not None else Console()

    def _resolve(
        conflict: LineConflict | PathConflict | ScalarConflict,
    ) -> ConflictResolution:
        if isinstance(conflict, LineConflict):
            return _resolve_line_conflict(conflict, active_console)
        if isinstance(conflict, ScalarConflict):
            return _resolve_scalar_conflict(conflict, active_console)
        return _resolve_path_conflict(conflict, active_console)

    return _resolve


# ---------------------------------------------------------------------------
# Line-based (markdown / arbitrary text) conflict.
# ---------------------------------------------------------------------------


def _resolve_line_conflict(
    conflict: LineConflict, console: Console
) -> ConflictResolution:
    """Render a line-based conflict, prompt, and return its resolution.

    ``EDIT`` opens ``$EDITOR`` on a ``.md`` tmpfile seeded with ours' lines and
    reads the result back with :meth:`str.splitlines` (terminators kept) into
    ``edited_lines``.
    """
    _render_line_conflict(conflict, console)
    choice = read_one_choice("  Choice (k/t/e/s): ", {"k", "t", "e", "s"})
    if choice == "k":
        return ConflictResolution(ConflictChoice.KEEP_OURS)
    if choice == "t":
        return ConflictResolution(ConflictChoice.TAKE_THEIRS)
    if choice == "s":
        return ConflictResolution(ConflictChoice.SKIP)
    edited = _edit_lines(conflict.ours)
    return ConflictResolution(ConflictChoice.EDIT, edited_lines=edited)


def _render_line_conflict(conflict: LineConflict, console: Console) -> None:
    """Print the ours (live) and theirs (tracked) line-blocks for ``conflict``."""
    sep = "─" * 57
    console.print(f"\n[dim]{sep}[/dim]")
    console.print(" [bold]line conflict[/bold]")
    console.print(f"[dim]{sep}[/dim]")
    console.print(" [green]ours (live):[/green]")
    _print_lines(conflict.ours, console, style="green")
    console.print(" [yellow]theirs (tracked):[/yellow]")
    _print_lines(conflict.theirs, console, style="yellow")
    _render_choices(console)


def _print_lines(lines: list[str], console: Console, *, style: str) -> None:
    """Print each line of ``lines`` (terminators stripped for display)."""
    if not lines:
        console.print(f"   [{style} dim](empty)[/{style} dim]")
        return
    for line in lines:
        console.print(f"   [{style}]{line.rstrip(chr(10))}[/{style}]")


def _edit_lines(ours: list[str]) -> list[str]:
    """Open ``$EDITOR`` on a ``.md`` tmpfile seeded with ``ours``; read back.

    Returns the edited lines with terminators kept
    (:meth:`str.splitlines` ``keepends=True``).
    """
    with tempfile.NamedTemporaryFile(
        "w", delete=False, suffix=".md", encoding="utf-8"
    ) as fh:
        fh.write("".join(ours))
        tmp_path = Path(fh.name)
    try:
        run_editor(tmp_path)
        return tmp_path.read_text(encoding="utf-8").splitlines(keepends=True)
    finally:
        tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Structural (YAML / JSONC) conflict.
# ---------------------------------------------------------------------------


def _resolve_path_conflict(
    conflict: PathConflict, console: Console
) -> ConflictResolution:
    """Render a structural conflict, prompt, and return its resolution.

    ``EDIT`` opens ``$EDITOR`` on a ``.yaml`` tmpfile seeded with the ours value
    serialized (``yaml.safe_dump``; empty for :data:`ABSENT`), parses the result
    back into ``edited_value``, and RE-PROMPTS the whole conflict when the
    edited buffer fails to parse (so a typo never crashes the install).
    """
    while True:
        _render_path_conflict(conflict, console)
        choice = read_one_choice("  Choice (k/t/e/s): ", {"k", "t", "e", "s"})
        if choice == "k":
            return ConflictResolution(ConflictChoice.KEEP_OURS)
        if choice == "t":
            return ConflictResolution(ConflictChoice.TAKE_THEIRS)
        if choice == "s":
            return ConflictResolution(ConflictChoice.SKIP)
        try:
            value = _edit_value(conflict.ours)
        except YAMLError as exc:
            console.print(f"[red]parse error: {exc}[/red]")
            console.print("[dim]re-prompting this conflict…[/dim]")
            continue
        return ConflictResolution(ConflictChoice.EDIT, edited_value=value)


def _render_path_conflict(conflict: PathConflict, console: Console) -> None:
    """Print the dotted path plus the ours / theirs values for ``conflict``."""
    sep = "─" * 57
    console.print(f"\n[dim]{sep}[/dim]")
    console.print(f" [bold]path conflict[/bold]  [cyan]{conflict.path}[/cyan]")
    console.print(f"[dim]{sep}[/dim]")
    console.print(f" [green]ours (live):[/green]    {_display_value(conflict.ours)}")
    console.print(
        f" [yellow]theirs (tracked):[/yellow] {_display_value(conflict.theirs)}"
    )
    _render_choices(console)


# ---------------------------------------------------------------------------
# Scalar (shallow preserve_user_keys) conflict.
# ---------------------------------------------------------------------------


def _resolve_scalar_conflict(
    conflict: ScalarConflict, console: Console
) -> ConflictResolution:
    """Render a scalar conflict, prompt, and return its resolution.

    Mirrors :func:`_resolve_path_conflict` exactly, with one added guard:
    ``EDIT`` opens ``$EDITOR`` on a ``.yaml`` tmpfile seeded with the ours
    scalar, parses the result back, and RE-PROMPTS the whole conflict both when
    the buffer fails to parse (a typo) AND when it parses to a NON-scalar
    (a mapping / list) — a shallow ``preserve_user_keys`` leaf must stay a
    scalar, so a structured edit is rejected rather than written.
    """
    while True:
        _render_scalar_conflict(conflict, console)
        choice = read_one_choice("  Choice (k/t/e/s): ", {"k", "t", "e", "s"})
        if choice == "k":
            return ConflictResolution(ConflictChoice.KEEP_OURS)
        if choice == "t":
            return ConflictResolution(ConflictChoice.TAKE_THEIRS)
        if choice == "s":
            return ConflictResolution(ConflictChoice.SKIP)
        try:
            value = _edit_value(conflict.ours)
        except YAMLError as exc:
            console.print(f"[red]parse error: {exc}[/red]")
            console.print("[dim]re-prompting this conflict…[/dim]")
            continue
        if not _is_plain_scalar(value):
            console.print(
                "[red]not a scalar: a preserve_user_keys leaf must be a "
                "single value, not a mapping or list[/red]"
            )
            console.print("[dim]re-prompting this conflict…[/dim]")
            continue
        return ConflictResolution(ConflictChoice.EDIT, edited_value=value)


def _render_scalar_conflict(conflict: ScalarConflict, console: Console) -> None:
    """Print the path plus the base / ours / theirs scalar sides for ``conflict``."""
    sep = "─" * 57
    console.print(f"\n[dim]{sep}[/dim]")
    console.print(f" [bold]scalar conflict[/bold]  [cyan]{conflict.path}[/cyan]")
    console.print(f"[dim]{sep}[/dim]")
    console.print(f" [dim]base:[/dim]              {_display_value(conflict.base)}")
    console.print(f" [green]ours (live):[/green]    {_display_value(conflict.ours)}")
    console.print(
        f" [yellow]upstream (tracked):[/yellow] {_display_value(conflict.theirs)}"
    )
    _render_choices(console)


def _is_plain_scalar(value: object) -> bool:
    """Return whether an edited value is an acceptable scalar leaf.

    A ``preserve_user_keys`` leaf must be a single scalar (str / int / float /
    bool / ``None``). A mapping or list (and any other container) is rejected so
    the scalar edit re-prompts. ``None`` (YAML ``null``) is a legitimate scalar.
    """
    return isinstance(value, str | int | float | bool | None.__class__)


def _display_value(value: object) -> str:
    """Render ``value`` for the conflict block; ABSENT shows as ``(absent)``."""
    if value is ABSENT:
        return "(absent)"
    return repr(value)


def _edit_value(ours: object) -> object:
    """Open ``$EDITOR`` on a ``.yaml`` tmpfile seeded with ``ours``; parse back.

    Seeds an empty buffer for :data:`ABSENT`. Raises
    :class:`ruamel.yaml.error.YAMLError` when the edited buffer does not parse;
    the caller catches it to re-prompt.
    """
    seed = "" if ours is ABSENT else _dump_value(ours)
    with tempfile.NamedTemporaryFile(
        "w", delete=False, suffix=".yaml", encoding="utf-8"
    ) as fh:
        fh.write(seed)
        tmp_path = Path(fh.name)
    try:
        run_editor(tmp_path)
        text = tmp_path.read_text(encoding="utf-8")
        return _load_value(text)
    finally:
        tmp_path.unlink(missing_ok=True)


def _dump_value(value: object) -> str:
    """Serialize a plain ``value`` to a YAML snippet for the edit seed."""
    yaml = YAML(typ="safe")
    yaml.default_flow_style = False
    buf = io.StringIO()
    yaml.dump(value, buf)
    return buf.getvalue()


def _load_value(text: str) -> object:
    """Parse the edited YAML ``text`` back to a plain python value."""
    return YAML(typ="safe").load(io.StringIO(text))


def _render_choices(console: Console) -> None:
    """Print the ``[k]/[t]/[e]/[s]`` menu (mirrors section_wizard styling)."""
    console.print("")
    console.print(
        "   [bold][[k]][/bold] keep ours (live)    [dim](preserve the live side)[/dim]"
    )
    console.print(
        "   [bold][[t]][/bold] take theirs (tracked) "
        "[dim](overwrite with the tracked side)[/dim]"
    )
    console.print(
        "   [bold][[e]][/bold] edit                "
        "[dim](open $EDITOR seeded with ours)[/dim]"
    )
    console.print(
        "   [bold][[s]][/bold] skip                "
        "[dim](keep live, ask again next install)[/dim]"
    )
    console.print("")
