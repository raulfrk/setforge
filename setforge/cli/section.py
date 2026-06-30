"""``setforge section`` subcommand — markerless host-local carve.

``setforge section detect`` diffs each live markdown file against its expected
deploy output, surfaces the regions you hand-edited, and walks an interactive
wizard to carve each into a durable **markerless** host-local span (overlay /
pinned / forked) in ``local.yaml``.

The legacy ``section emit`` / ``section add`` marker-authoring commands were
retired with the user-section marker mechanism — host-local content is now
captured as overlay spans (run ``setforge section detect``) or staged via
``setforge stage``; no marker pairs are ever emitted.
"""

from __future__ import annotations

from pathlib import Path

import typer

from setforge.cli import (
    _CONFIG_OPTION,
    _PROFILE_OPTION,
    _resolve_config_arg,
    app,
)

section_app: typer.Typer = typer.Typer(
    help="Carve hand-edited live regions into markerless host-local spans.",
    no_args_is_help=True,
    rich_markup_mode=None,
)
app.add_typer(section_app, name="section")


@section_app.command("detect")
def section_detect(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    tracked_file: str | None = typer.Option(
        None,
        "--tracked-file",
        help="tracked_files key; omit to scan every markdown tracked_file with drift",
    ),
) -> None:
    """Detect hand-edited regions in live markdown and carve them into spans.

    Diffs each live markdown file against its expected deploy output, surfaces
    the regions you hand-edited, and walks an interactive wizard to carve each
    into a durable markerless host-local span (overlay / pinned / forked).
    """
    from setforge.cli import _detect_helpers

    config_path = _resolve_config_arg(config)
    try:
        _detect_helpers.run_detect(
            config_path=config_path, profile=profile, tracked_file=tracked_file
        )
    except KeyError as exc:
        raise typer.BadParameter(
            f"tracked_file {exc.args[0]!r} is not in profile {profile!r}"
        ) from exc
