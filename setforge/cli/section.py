"""``setforge section`` subcommand — user-section marker management.

``setforge section emit <semantics> <name>`` prints a paste-ready
marker pair to stdout for files setforge cannot safely auto-edit.

``setforge section add`` edits a tracked markdown file
(``.md`` / ``.markdown`` only) to insert a marker pair at a
user-picked anchor line. Scripted via flags, or interactive via
prompt_toolkit dialogs + the bespoke anchor-line TUI picker.
"""

from __future__ import annotations

import hashlib
import re
import sys
import tempfile
from pathlib import Path

import typer
from prompt_toolkit.shortcuts import input_dialog, radiolist_dialog, yes_no_dialog
from rich.console import Console

from setforge._editor import run_editor
from setforge.cli import _CONFIG_OPTION, _PROFILE_OPTION, _resolve_config_arg, app
from setforge.cli._anchor_picker import pick_anchor_line
from setforge.compare import resolve_src
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


def _stdin_is_tty() -> bool:
    """Indirection layer so unit tests can monkeypatch this single function."""
    return sys.stdin.isatty()


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

    The body's sha256 is stamped into the end marker as
    ``hash=<sha256-hex>`` so the resulting pair satisfies the strict
    parser (``extract_sections(allow_legacy=False)``) — install /
    compare / sync all reject hash-less markers on the tracked side.
    """
    body_block = (body if body.endswith("\n") else body + "\n") if body else "\n"
    # ``extract_sections`` returns the content between the markers — which
    # is ``body_block`` literally. Hash that so the embedded ``hash=``
    # matches what install / compare / sync re-derive.
    body_hash = hashlib.sha256(body_block.encode("utf-8")).hexdigest()
    return (
        f"<!-- setforge:user-section start {semantics} {name} -->\n"
        f"{body_block}"
        f"<!-- setforge:user-section end {semantics} {name} hash={body_hash} -->\n"
    )


@section_app.command("emit")
def section_emit(
    semantics: str = typer.Argument(..., help="shared|host-local"),
    name: str = typer.Argument(..., help="lowercase-with-dashes section name"),
) -> None:
    """Print a paste-ready marker pair to stdout."""
    _validate_semantics(semantics)
    _validate_name(name)
    sys.stdout.write(
        _format_marker_pair_with_body(semantics=semantics, name=name, body="")
    )


def _resolve_tracked_file_path(*, config_path: Path, tracked_file_key: str) -> Path:
    """Resolve a ``tracked_files`` key to an absolute path in the config repo.

    Matches the canonical install/compare resolver:
    ``<config-repo-root>/tracked/<tracked_file.src>``.
    """
    cfg = load_config(config_path)
    if tracked_file_key not in cfg.tracked_files:
        raise typer.BadParameter(
            f"tracked_file {tracked_file_key!r} not found in {config_path}"
        )
    repo_root = config_path.resolve().parent
    return resolve_src(cfg.tracked_files[tracked_file_key], repo_root)


def _validate_anchor_line(*, anchor_line: int, total_lines: int) -> None:
    if anchor_line < 1:
        raise typer.BadParameter(f"--anchor-line must be >= 1, got {anchor_line}")
    if anchor_line > total_lines:
        raise typer.BadParameter(
            f"--anchor-line {anchor_line} exceeds file length {total_lines}"
        )


def _check_duplicate_name(*, file_text: str, name: str) -> None:
    sections = extract_sections(file_text, allow_legacy=True)
    if name in sections:
        raise typer.BadParameter(f"section name {name!r} already exists in this file")


def _check_markdown_suffix(*, target: Path) -> None:
    if target.suffix not in _MARKDOWN_SUFFIXES:
        raise typer.BadParameter(
            f"section add only edits markdown (.md/.markdown); got {target.suffix!r}. "
            "Use `setforge section emit <semantics> <name>` to print the marker "
            "pair and paste manually."
        )


def _read_body(*, body_source: str, body_file: Path | None) -> str:
    if body_source == "empty":
        return ""
    if body_source == "file":
        if body_file is None:
            raise typer.BadParameter("--body-source=file requires --body-file")
        return body_file.read_text()
    if body_source == "editor":
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".md", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            run_editor(tmp_path)
            body = tmp_path.read_text()
        finally:
            tmp_path.unlink(missing_ok=True)
        if not body.strip():
            typer.secho(
                "editor returned empty body; aborting.",
                err=True,
                fg=typer.colors.YELLOW,
            )
            raise typer.Exit(1)
        return body
    raise typer.BadParameter(
        f"--body-source must be one of {sorted(_VALID_BODY_SOURCES)}"
    )


def _count_total_lines(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _insert_marker_pair(
    *,
    file_text: str,
    anchor_line: int,
    semantics: str,
    name: str,
    body: str,
) -> str:
    """Return ``file_text`` with a marker pair inserted AFTER ``anchor_line``."""
    lines = file_text.splitlines(keepends=True)
    insertion = _format_marker_pair_with_body(semantics=semantics, name=name, body=body)
    head = "".join(lines[:anchor_line])
    tail = "".join(lines[anchor_line:])
    return head + insertion + tail


def _print_next_steps(*, console: Console, target: Path, profile: str) -> None:
    console.print(f"[green]wrote[/green] marker pair to [bold]{target}[/bold]")
    console.print("next steps:")
    console.print(f"  cd {target.parent}")
    console.print("  git diff")
    console.print(f"  git add {target.name} && git commit && git push")
    console.print(f"  setforge install --profile={profile}")


def _section_add_scripted(
    *,
    config_path: Path,
    profile: str,
    tracked_file: str,
    semantics: str,
    name: str,
    anchor_line: int,
    body_source: str,
    body_file: Path | None,
) -> None:
    """Apply ``section add`` without any prompts."""
    _validate_semantics(semantics)
    _validate_name(name)
    if body_source == "empty" and body_file is not None:
        raise typer.BadParameter(
            "--body-source=empty is mutually exclusive with --body-file"
        )
    if body_source not in _VALID_BODY_SOURCES:
        raise typer.BadParameter(
            f"--body-source must be one of {sorted(_VALID_BODY_SOURCES)}"
        )
    target = _resolve_tracked_file_path(
        config_path=config_path, tracked_file_key=tracked_file
    )
    _check_markdown_suffix(target=target)
    text = target.read_text()
    total_lines = _count_total_lines(text)
    _validate_anchor_line(anchor_line=anchor_line, total_lines=total_lines)
    _check_duplicate_name(file_text=text, name=name)
    body = _read_body(body_source=body_source, body_file=body_file)
    updated = _insert_marker_pair(
        file_text=text,
        anchor_line=anchor_line,
        semantics=semantics,
        name=name,
        body=body,
    )
    target.write_text(updated)
    _print_next_steps(console=Console(), target=target, profile=profile)


def _interactive_pick_tracked_file(*, config_path: Path) -> str:
    cfg = load_config(config_path)
    keys = list(cfg.tracked_files.keys())
    for i, k in enumerate(keys, start=1):
        typer.echo(f"  [{i}] {k}")
    choice = typer.prompt("tracked_file number")
    try:
        return keys[int(choice) - 1]
    except (ValueError, IndexError) as exc:
        raise typer.BadParameter(f"invalid choice: {choice}") from exc


def _interactive_pick_semantics() -> str:
    result = radiolist_dialog(
        title="user-section semantics",
        text=(
            "shared = propagates across hosts via tracked repo\n"
            "host-local = per-host only"
        ),
        values=[("shared", "shared"), ("host-local", "host-local")],
        default="shared",
    ).run()
    if result is None:
        typer.echo("aborted.")
        raise typer.Exit(0)
    return result


def _interactive_pick_name() -> str:
    result = input_dialog(
        title="section name",
        text="lowercase-dashes, <=63 chars",
    ).run()
    if result is None:
        raise typer.Exit(0)
    _validate_name(result)
    return result


def _interactive_pick_body_source() -> str:
    result = radiolist_dialog(
        title="body source",
        text="how should the section body be filled?",
        values=[
            ("empty", "empty - fill in later"),
            ("editor", "editor - open $EDITOR now"),
            ("file", "file - read from a file path"),
        ],
        default="empty",
    ).run()
    if result is None:
        raise typer.Exit(0)
    return result


def _interactive_confirm(*, target: Path, anchor_line: int) -> bool:
    return bool(
        yes_no_dialog(
            title="confirm",
            text=f"insert marker pair into {target} after line {anchor_line}?",
        ).run()
    )


def _section_add_interactive(
    *,
    config_path: Path,
    profile: str,
    tracked_file: str | None,
    semantics: str | None,
    name: str | None,
    anchor_line: int | None,
    body_source: str | None,
    body_file: Path | None,
    yes: bool,
) -> None:
    """Walk every missing flag through a prompt_toolkit dialog."""
    if tracked_file is None:
        tracked_file = _interactive_pick_tracked_file(config_path=config_path)
    if semantics is None:
        semantics = _interactive_pick_semantics()
    if name is None:
        name = _interactive_pick_name()

    target = _resolve_tracked_file_path(
        config_path=config_path, tracked_file_key=tracked_file
    )
    _check_markdown_suffix(target=target)
    text = target.read_text()
    _check_duplicate_name(file_text=text, name=name)

    if anchor_line is None:
        anchor_line = pick_anchor_line(file_text=text, filename=str(target))
        if anchor_line is None:
            typer.echo("aborted.")
            raise typer.Exit(0)
    _validate_anchor_line(anchor_line=anchor_line, total_lines=_count_total_lines(text))

    if body_source is None:
        body_source = _interactive_pick_body_source()
    body = _read_body(body_source=body_source, body_file=body_file)

    if not yes and not _interactive_confirm(target=target, anchor_line=anchor_line):
        typer.echo("aborted.")
        raise typer.Exit(0)

    updated = _insert_marker_pair(
        file_text=text,
        anchor_line=anchor_line,
        semantics=semantics,
        name=name,
        body=body,
    )
    target.write_text(updated)
    _print_next_steps(console=Console(), target=target, profile=profile)


@section_app.command("add")
def section_add(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    tracked_file: str | None = typer.Option(
        None, "--tracked-file", help="tracked_files key from setforge.yaml"
    ),
    semantics: str | None = typer.Option(None, "--semantics", help="shared|host-local"),
    name: str | None = typer.Option(
        None, "--name", help="lowercase-with-dashes section name"
    ),
    anchor_line: int | None = typer.Option(
        None,
        "--anchor-line",
        help="1-indexed line; marker pair inserted AFTER this line",
    ),
    body_source: str | None = typer.Option(
        None, "--body-source", help="empty|editor|file"
    ),
    body_file: Path | None = typer.Option(
        None, "--body-file", help="Path to a file whose contents go between markers."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the final confirm prompt."
    ),
) -> None:
    """Add a user-section marker pair to a tracked markdown file."""
    config_path = _resolve_config_arg(config)

    all_set = (
        tracked_file is not None
        and semantics is not None
        and name is not None
        and anchor_line is not None
        and body_source is not None
    )
    if all_set:
        assert tracked_file is not None
        assert semantics is not None
        assert name is not None
        assert anchor_line is not None
        assert body_source is not None
        _section_add_scripted(
            config_path=config_path,
            profile=profile,
            tracked_file=tracked_file,
            semantics=semantics,
            name=name,
            anchor_line=anchor_line,
            body_source=body_source,
            body_file=body_file,
        )
        return

    if not _stdin_is_tty():
        raise typer.BadParameter(
            "interactive flags missing in non-TTY context; pass --tracked-file, "
            "--semantics, --name, --anchor-line, --body-source, --yes"
        )
    _section_add_interactive(
        config_path=config_path,
        profile=profile,
        tracked_file=tracked_file,
        semantics=semantics,
        name=name,
        anchor_line=anchor_line,
        body_source=body_source,
        body_file=body_file,
        yes=yes,
    )
