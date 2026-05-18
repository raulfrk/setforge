"""revert subcommand + transitions inspection subgroup.

``revert`` replays the most recent transition for a profile in reverse
and records its own reverse transition (so a second ``revert`` acts as
redo). ``transitions list`` / ``transitions show`` inspect the recorded
history.
"""

import json
from datetime import UTC
from pathlib import Path

import typer

from setforge import transitions
from setforge.cli import (
    _CONFIG_OPTION,
    _PROFILE_OPTION,
    _resolve_config_arg,
    app,
)
from setforge.cli._plugin_helpers import _write_reverse_transition
from setforge.errors import NoTransitionFound


@app.command()
def revert(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
) -> None:
    """Revert the most recent transition for the named profile.

    Applies the recorded patch in reverse and reverses any extension
    delta (uninstalling what was installed, re-installing what was
    uninstalled). Records its own reverse transition so a second
    revert invocation acts as redo.
    """
    config = _resolve_config_arg(config)
    transition = transitions.load_latest(profile)
    if transition is None:
        raise NoTransitionFound(f"no transition history for profile {profile!r}")

    transitions.ensure_state_dir_writable()
    typer.echo(f"reverting: {transition}")

    meta_payload = json.loads((transition / "meta.json").read_text(encoding="utf-8"))
    touched_paths = [Path(p) for p in meta_payload.get("paths", [])]
    file_pre = transitions.snapshot_paths(touched_paths)

    transitions.apply_patch_reverse(transition)

    target = _write_reverse_transition(transition, profile, touched_paths, file_pre)
    typer.echo(f"transition: {target}")


transitions_app = typer.Typer(
    help="Inspect transition history for install/sync/revert.",
    no_args_is_help=True,
)
app.add_typer(transitions_app, name="transitions")


_TRANSITIONS_LIST_PROFILE_OPTION = typer.Option(
    None,
    "--profile",
    "-p",
    help="Filter to specified profile(s). Repeatable; OR-filter.",
)
_TRANSITIONS_LIST_REVERSE_OPTION = typer.Option(
    False, "--reverse", help="Newest-first instead of oldest-first."
)


@transitions_app.command("list")
def transitions_list(
    profile: list[str] | None = _TRANSITIONS_LIST_PROFILE_OPTION,
    reverse: bool = _TRANSITIONS_LIST_REVERSE_OPTION,
) -> None:
    """List recorded transitions across all profiles."""
    listings = transitions.list_transitions(
        profile_filter=list(profile) if profile else None,
        reverse=reverse,
    )
    if not listings:
        typer.echo("(no transitions)")
        return
    rows = [
        (
            entry.timestamp.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            entry.command,
            entry.profile,
            str(entry.file_count),
            str(entry.ext_count),
            str(entry.plugin_count),
            entry.directory.name,
        )
        for entry in listings
    ]
    headers = (
        "TIMESTAMP",
        "COMMAND",
        "PROFILE",
        "FILES",
        "EXTS",
        "PLUGINS",
        "DIRECTORY",
    )
    widths = [
        max(len(headers[i]), max((len(r[i]) for r in rows), default=0))
        for i in range(len(headers))
    ]
    typer.echo("  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=False)))
    for row in rows:
        typer.echo("  ".join(c.ljust(w) for c, w in zip(row, widths, strict=False)))


@transitions_app.command("show")
def transitions_show(
    prefix: str = typer.Argument(..., help="Dirname or unique-prefix match."),
) -> None:
    """Show metadata and per-file action summary for one transition."""
    target = transitions.resolve_transition_prefix(prefix)
    meta = json.loads((target / "meta.json").read_text(encoding="utf-8"))
    typer.echo(f"DIRECTORY  {target.name}")
    typer.echo(f"COMMAND    {meta.get('command', '')}")
    typer.echo(f"PROFILE    {meta.get('profile', '')}")
    typer.echo(f"TIMESTAMP  {meta.get('timestamp', '')}")
    if "host" in meta:
        typer.echo(f"HOST       {meta['host']}")
    if "version" in meta:
        typer.echo(f"VERSION    {meta['version']}")

    file_actions = transitions.summarize_transition(target)
    if file_actions:
        typer.echo("")
        typer.echo("FILES")
        action_width = max(len(action) for action in file_actions.values())
        for path, action in sorted(file_actions.items()):
            typer.echo(f"  {action.ljust(action_width)}  {path}")

    ext_file = target / "extensions.json"
    if ext_file.exists():
        ext_payload = json.loads(ext_file.read_text(encoding="utf-8"))
        added = ext_payload.get("added", []) or []
        removed = ext_payload.get("removed", []) or []
        if added or removed:
            typer.echo("")
            typer.echo("EXTENSIONS")
            for ext_id in added:
                typer.echo(f"  added    {ext_id}")
            for ext_id in removed:
                typer.echo(f"  removed  {ext_id}")
