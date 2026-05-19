"""revert subcommand + transitions inspection subgroup.

``revert`` replays the most recent transition for a profile in reverse
and records its own reverse transition (so a second ``revert`` acts as
redo). ``transitions list`` / ``transitions show`` inspect the recorded
history.

Per setforge-p1vl (mockup A): revert is gated by a confirm-explain-redo
wizard that shows the full diff, RISKS, and REDO instructions before
applying. ``--yes`` short-circuits the wizard for non-interactive use.
"""

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import typer

from setforge import transitions
from setforge._editor import run_editor
from setforge.cli import (
    _CONFIG_OPTION,
    _PROFILE_OPTION,
    _resolve_config_arg,
    app,
)
from setforge.cli._plugin_helpers import _write_reverse_transition
from setforge.cli._revert_confirm import (
    FileMutation,
    RevertChoice,
    RevertPlan,
    confirm_revert_operation,
)
from setforge.errors import NoTransitionFound


def _human_age(timestamp: datetime, now: datetime) -> str:
    """Return a coarse human-readable age string ("11 minutes ago")."""
    delta = now - timestamp
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds} seconds ago"
    minutes = seconds // 60
    if minutes < 60:
        unit = "minute" if minutes == 1 else "minutes"
        return f"{minutes} {unit} ago"
    hours = minutes // 60
    if hours < 24:
        unit = "hour" if hours == 1 else "hours"
        return f"{hours} {unit} ago"
    days = hours // 24
    unit = "day" if days == 1 else "days"
    return f"{days} {unit} ago"


def _diff_summaries_from_patch(patch_text: str) -> dict[str, str]:
    """Parse a unified diff and return ``{abs_path: "+N -M"}`` per file.

    Counts hunk-body ``+``/``-`` lines (skipping ``+++`` / ``---``
    headers). Paths are rebuilt from the ``+++`` line per
    :func:`transitions._diff_path` (root-relative; prepend ``/``).
    ``/dev/null`` paths use the corresponding ``--- a/<x>`` for deletions.
    """
    summaries: dict[str, str] = {}
    current_path: str | None = None
    plus = 0
    minus = 0
    for line in patch_text.splitlines():
        if line.startswith("--- "):
            from_path = line[4:].split("\t", 1)[0]
            current_path = (
                "/" + from_path if from_path != "/dev/null" else None
            )
            continue
        if line.startswith("+++ "):
            to_path = line[4:].split("\t", 1)[0]
            if to_path != "/dev/null":
                current_path = "/" + to_path
            plus = 0
            minus = 0
            continue
        if line.startswith("@@"):
            continue
        if current_path is None:
            continue
        if line.startswith("+"):
            plus += 1
        elif line.startswith("-"):
            minus += 1
        summaries[current_path] = f"+{plus} -{minus}"
    return summaries


def _build_revert_plan(transition: Path, profile: str) -> RevertPlan:
    """Read ``transition`` + compute per-file diff summaries → RevertPlan.

    The plan reflects what the FORWARD transition did; revert will
    reverse each item. Plugin / extension reconciles are inferred from
    the transition's ``plugins.json`` / ``extensions.json`` payloads when
    present. ``user_edit_collision`` is left empty for v1 — collision
    detection runs at apply time via ``patch --dry-run -R`` (see
    :func:`transitions.apply_patch_reverse`); refusing-on-collision
    preserves the safety contract without re-implementing hunk parsing
    here.
    """
    meta_payload = json.loads((transition / "meta.json").read_text(encoding="utf-8"))
    timestamp = datetime.fromisoformat(meta_payload["timestamp"])
    age = _human_age(timestamp, datetime.now(UTC))

    patch_file = transition / "changes.patch"
    diff_summaries: dict[str, str] = {}
    if patch_file.exists():
        diff_summaries = _diff_summaries_from_patch(
            patch_file.read_text(encoding="utf-8")
        )

    touched = [Path(p) for p in meta_payload.get("paths", [])]
    file_mutations = tuple(
        FileMutation(
            path=p,
            diff_summary=diff_summaries.get(str(p), "+0 -0"),
        )
        for p in touched
    )

    return RevertPlan(
        transition_id=transition.name,
        transition_type=str(meta_payload.get("command", "install")),
        profile=profile,
        age_human=age,
        file_mutations=file_mutations,
        plugin_reconciles=(),
        extension_reconciles=(),
        redo_command=f"setforge revert --profile={profile}",
    )


def _render_plan_to_editor(plan: RevertPlan) -> Path:
    """Write a human-readable rendering of ``plan`` to a tmp file → Path.

    Used by APPLY_WITH_EDITOR to let the user review the plan in their
    ``$EDITOR`` before re-prompting. The file is read-only-ish: we never
    parse the editor's output back; this is a review gesture, not a
    plan-editor.
    """
    fd, name = tempfile.mkstemp(prefix="setforge-revert-plan-", suffix=".txt")
    target = Path(name)
    lines = [
        f"transition: {plan.transition_id}",
        f"  type:    {plan.transition_type}",
        f"  profile: {plan.profile}",
        f"  age:     {plan.age_human}",
        "",
        f"files affected ({len(plan.file_mutations)}):",
    ]
    for fm in plan.file_mutations:
        lines.append(f"  M  {fm.path}  (line-delta: {fm.diff_summary})")
    if plan.plugin_reconciles:
        lines.append("")
        lines.append(f"plugins reconciled ({len(plan.plugin_reconciles)}):")
        for pr in plan.plugin_reconciles:
            marker = "+" if pr.operation == "enabled" else "-"
            lines.append(f"  {marker} {pr.plugin_id}  {pr.source}")
    if plan.extension_reconciles:
        lines.append("")
        lines.append(f"extensions reconciled ({len(plan.extension_reconciles)}):")
        for er in plan.extension_reconciles:
            marker = "+" if er.operation == "installed" else "-"
            lines.append(f"  {marker} {er.extension_id}  {er.source}")
    lines.append("")
    lines.append(f"REDO: {plan.redo_command}")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.close(fd)
    return target


def _apply_revert(transition: Path, profile: str) -> None:
    """Apply the reverse transition and print the post-success summary."""
    transitions.ensure_state_dir_writable()
    typer.echo(f"reverting: {transition}")

    meta_payload = json.loads((transition / "meta.json").read_text(encoding="utf-8"))
    touched_paths = [Path(p) for p in meta_payload.get("paths", [])]
    file_pre = transitions.snapshot_paths(touched_paths)

    transitions.apply_patch_reverse(transition)

    target = _write_reverse_transition(transition, profile, touched_paths, file_pre)
    typer.echo(f"transition: {target}")
    typer.echo(f"to REDO this revert: setforge revert --profile={profile}")


@app.command()
def revert(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirm-explain-redo prompt (non-interactive use).",
    ),
) -> None:
    """Revert the most recent transition for the named profile.

    Opens the confirm-explain-redo wizard before applying (mockup A).
    Applies the recorded patch in reverse and reverses any extension /
    plugin delta. Records its own reverse transition so a second revert
    invocation acts as redo.
    """
    config = _resolve_config_arg(config)
    transition = transitions.load_latest(profile)
    if transition is None:
        raise NoTransitionFound(f"no transition history for profile {profile!r}")

    plan = _build_revert_plan(transition, profile)
    choice = confirm_revert_operation(plan=plan, yes=yes)
    if choice is RevertChoice.ABORT:
        return
    if choice is RevertChoice.APPLY_WITH_EDITOR:
        target = _render_plan_to_editor(plan)
        try:
            run_editor(target)
        finally:
            target.unlink(missing_ok=True)
        # Re-prompt after editor closes so the user can still abort.
        choice = confirm_revert_operation(plan=plan, yes=yes)
        if choice is RevertChoice.ABORT:
            return

    _apply_revert(transition, profile)


transitions_app: typer.Typer = typer.Typer(
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
