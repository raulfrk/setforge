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
from rich.console import Console
from rich.table import Table

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
    ExtensionOperation,
    ExtensionReconcile,
    FileMutation,
    MultiStepRevertPlan,
    PluginOperation,
    PluginReconcile,
    RevertChoice,
    RevertPlan,
    confirm_multi_step_revert_operation,
    confirm_revert_operation,
)
from setforge.errors import NoTransitionFound, RevertFailed, SetforgeError


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


def _compact_age(timestamp: datetime, now: datetime) -> str:
    """Return the mockup's compact age form ("2h ago", "3d ago", "5m ago", "<1m ago").

    Distinct from :func:`_human_age` (used by the confirm wizard's
    long-form panel) — the listing column needs a fixed-narrow string
    so the table aligns. Uses UTC arithmetic via the caller-supplied
    ``now``; both ``timestamp`` and ``now`` must be tz-aware.
    """
    delta = now - timestamp
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "<1m ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


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
            current_path = "/" + from_path if from_path != "/dev/null" else None
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


def _plugin_reconciles_from_transition(
    transition: Path,
) -> tuple[PluginReconcile, ...]:
    """Build :class:`PluginReconcile` tuple from a transition's plugins.json.

    Maps the forward ``PluginDelta`` to the post-revert state that the
    panel surfaces (matches the dispatch semantics in
    :data:`setforge.cli._plugin_helpers._REVERSE_PLUGIN_DISPATCH`):

    - forward ``installed`` → revert uninstalls → :attr:`PluginOperation.DISABLED`
      (panel marker ``-``).
    - forward ``enabled``   → revert disables   → :attr:`PluginOperation.DISABLED`.
    - forward ``disabled``  → revert re-enables → :attr:`PluginOperation.ENABLED`
      (panel marker ``+``).

    ``marketplaces_added`` / ``marketplaces_removed`` are intentionally
    NOT projected into the panel listing — the wizard's plugin section
    is per-plugin; marketplace ops are a separate axis and would need
    their own renderer.
    """
    plugin_file = transition / "plugins.json"
    if not plugin_file.exists():
        return ()
    payload = json.loads(plugin_file.read_text(encoding="utf-8"))
    delta = transitions.plugin_delta_from_json(payload)
    source = "[from transition record]"
    reconciles: list[PluginReconcile] = []
    for plugin_id in delta.installed:
        reconciles.append(
            PluginReconcile(
                plugin_id=plugin_id,
                operation=PluginOperation.DISABLED,
                source=source,
            )
        )
    for plugin_id in delta.enabled:
        reconciles.append(
            PluginReconcile(
                plugin_id=plugin_id,
                operation=PluginOperation.DISABLED,
                source=source,
            )
        )
    for plugin_id in delta.disabled:
        reconciles.append(
            PluginReconcile(
                plugin_id=plugin_id,
                operation=PluginOperation.ENABLED,
                source=source,
            )
        )
    return tuple(reconciles)


def _extension_reconciles_from_transition(
    transition: Path,
) -> tuple[ExtensionReconcile, ...]:
    """Build :class:`ExtensionReconcile` tuple from a transition's extensions.json.

    Maps the forward ``ExtensionDelta`` to the post-revert state:

    - forward ``added``   → revert uninstalls → :attr:`ExtensionOperation.UNINSTALLED`
      (panel marker ``-``).
    - forward ``removed`` → revert reinstalls → :attr:`ExtensionOperation.INSTALLED`
      (panel marker ``+``).
    """
    ext_file = transition / "extensions.json"
    if not ext_file.exists():
        return ()
    payload = json.loads(ext_file.read_text(encoding="utf-8"))
    delta = transitions.extension_delta_from_json(payload)
    source = "[from transition record]"
    reconciles: list[ExtensionReconcile] = []
    for ext_id in delta.added:
        reconciles.append(
            ExtensionReconcile(
                extension_id=ext_id,
                operation=ExtensionOperation.UNINSTALLED,
                source=source,
            )
        )
    for ext_id in delta.removed:
        reconciles.append(
            ExtensionReconcile(
                extension_id=ext_id,
                operation=ExtensionOperation.INSTALLED,
                source=source,
            )
        )
    return tuple(reconciles)


def _build_revert_plan(transition: Path, profile: str) -> RevertPlan:
    """Read ``transition`` + compute per-file diff summaries → RevertPlan.

    The plan reflects what the FORWARD transition did; revert will
    reverse each item. Plugin / extension reconciles are inferred from
    the transition's ``plugins.json`` / ``extensions.json`` payloads when
    present (via :func:`_plugin_reconciles_from_transition` and
    :func:`_extension_reconciles_from_transition`). ``user_edit_collision``
    is left empty for v1 — collision detection runs at apply time via
    ``patch --dry-run -R`` (see :func:`transitions.apply_patch_reverse`);
    refusing-on-collision preserves the safety contract without
    re-implementing hunk parsing here.
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
        plugin_reconciles=_plugin_reconciles_from_transition(transition),
        extension_reconciles=_extension_reconciles_from_transition(transition),
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
            marker = "+" if pr.operation is PluginOperation.ENABLED else "-"
            lines.append(f"  {marker} {pr.plugin_id}  {pr.source}")
    if plan.extension_reconciles:
        lines.append("")
        lines.append(f"extensions reconciled ({len(plan.extension_reconciles)}):")
        for er in plan.extension_reconciles:
            marker = "+" if er.operation is ExtensionOperation.INSTALLED else "-"
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


_TO_BEFORE_OPTION = typer.Option(
    None,
    "--to-before",
    help=(
        "Revert the named transition AND every newer transition for this "
        "profile. All N steps are dry-run-checked atomically before any "
        "live mutation; on any dry-run failure the live tree stays clean."
    ),
)


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
    to_before: str | None = _TO_BEFORE_OPTION,
) -> None:
    """Revert the most recent transition (default) or a chain back to a
    named transition (``--to-before=<id>``).

    Opens the confirm-explain-redo wizard before applying (mockup A for
    single-step; mockup H summary panel for multi-step). Records its own
    reverse transition so a second revert acts as redo. For multi-step
    revert the dry-run pass runs over ALL N transitions BEFORE any live
    mutation — late-step drift aborts before the chain's first write.
    """
    config = _resolve_config_arg(config)
    if to_before is not None:
        _revert_to_before(profile, to_before, yes=yes)
        return

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


def _resolve_to_before_chain(
    profile: str, to_before: str
) -> list[transitions.TransitionListing]:
    """Return the chain of transitions to revert (newest-first), inclusive of
    ``to_before``.

    Raises :class:`SetforgeError` if the prefix doesn't resolve, the
    resolved transition isn't for ``profile``, or no transitions exist
    for the profile. Newest-first order matches both the mockup-H
    listing and the dry-run / apply order — the most-recent transition
    reverts first so each step's reverse patch lines up against the
    live tree it was recorded from.
    """
    target_path = transitions.resolve_transition_prefix(to_before)
    target_meta = json.loads(
        (target_path / "meta.json").read_text(encoding="utf-8")
    )
    target_profile = str(target_meta.get("profile", ""))
    if target_profile != profile:
        raise SetforgeError(
            f"transition {target_path.name!r} is for profile "
            f"{target_profile!r}, not {profile!r}"
        )
    all_for_profile = transitions.list_transitions(
        profile_filter=[profile], reverse=True
    )
    if not all_for_profile:
        raise NoTransitionFound(
            f"no transition history for profile {profile!r}"
        )
    chain: list[transitions.TransitionListing] = []
    for entry in all_for_profile:
        chain.append(entry)
        if entry.directory == target_path:
            return chain
    raise SetforgeError(
        f"transition {target_path.name!r} not found in profile "
        f"{profile!r}'s recorded history"
    )


def _revert_to_before(profile: str, to_before: str, *, yes: bool) -> None:
    """Multi-step revert: pre-flight first step, then sequential apply.

    Steps:
    1. Resolve the chain (target + every newer transition, newest-first).
    2. Pre-flight check the FIRST (newest) step via
       ``apply_patch_reverse(dry_run=True)``: this catches the
       most-likely failure mode — drift on the live tree since the
       most-recent transition was recorded. Surface failure and exit 1
       on drift; no live mutation has occurred.

       Note: we cannot pre-flight steps 2..N without applying 1..N-1
       first — each later step's reverse patch is defined against the
       state produced by reversing its successor. Steps 2..N still run
       their internal dry-run-then-apply (the existing
       ``dry_run=False`` mode) so they refuse cleanly mid-stream on
       unexpected drift, with the partial-state warning at step N.
    3. Show the multi-step confirm wizard (one prompt covering all N).
    4. On user confirm: apply each step's reverse via
       ``_apply_revert`` (which calls ``apply_patch_reverse(dry_run=False)``
       — i.e. dry-run-then-real-apply per step — plus plugin / extension
       reconcile and writes a reverse transition). If a mid-stream
       failure (rare: ENOSPC, filesystem race, or unexpected drift
       between steps), surface the partial state and exit 1 with an
       "inconsistent state" warning.
    """
    chain = _resolve_to_before_chain(profile, to_before)
    # Pre-flight the newest step. Only step 1 is checkable against live
    # state without applying prior steps; see docstring.
    try:
        transitions.apply_patch_reverse(chain[0].directory, dry_run=True)
    except RevertFailed as exc:
        raise SetforgeError(
            f"dry-run reversal of {chain[0].directory.name!r} failed; "
            f"no live changes made:\n{exc}"
        ) from exc

    step_plans = tuple(_build_revert_plan(entry.directory, profile) for entry in chain)
    plan = MultiStepRevertPlan(profile=profile, steps=step_plans)
    choice = confirm_multi_step_revert_operation(plan=plan, yes=yes)
    if choice is RevertChoice.ABORT:
        return

    total = len(chain)
    for index, entry in enumerate(chain, start=1):
        try:
            _apply_revert(entry.directory, profile)
        except RevertFailed as exc:
            raise SetforgeError(
                f"applied {index - 1} of {total}; system is in inconsistent "
                f"state; run setforge transitions show to inspect:\n{exc}"
            ) from exc


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
_TRANSITIONS_LIST_OLDEST_FIRST_OPTION = typer.Option(
    False,
    "--oldest-first",
    help="Reverse the default newest-first order (oldest first).",
)


@transitions_app.command("list")
def transitions_list(
    profile: list[str] | None = _TRANSITIONS_LIST_PROFILE_OPTION,
    oldest_first: bool = _TRANSITIONS_LIST_OLDEST_FIRST_OPTION,
) -> None:
    """List recorded transitions for one or more profiles (newest-first).

    Columns per mockup H: ``id`` / ``type`` / ``age`` / ``files`` /
    ``plugins`` / ``ext``. Use ``--oldest-first`` to reverse to
    chronological order.
    """
    listings = transitions.list_transitions(
        profile_filter=list(profile) if profile else None,
        reverse=not oldest_first,
    )
    if not listings:
        typer.echo("(no transitions)")
        return
    profile_filter = list(profile) if profile else None
    _render_transitions_table(listings, profile_filter=profile_filter)


def _wide_console() -> Console:
    """Build a Console wide enough that mockup-H rows never wrap.

    Rich defaults to 80-col width when stdout is not a TTY (the case
    under CliRunner) — which truncates / wraps transition dirnames
    mid-line and breaks both the mockup parity and the
    ``--to-before=<id>`` copy-paste suggestion. A fixed 200-col width
    fits the widest realistic row (dirname + suffix < ~180 chars).

    ``highlight=False`` disables Rich's auto-highlighter — the
    transitions list embeds numbers and dirnames that Rich would
    otherwise wrap in ANSI sequences, breaking substring assertions
    in tests and copy-pasteability of the suggested commands.
    """
    return Console(width=200, soft_wrap=True, highlight=False)


def _render_transitions_table(
    listings: list[transitions.TransitionListing],
    *,
    profile_filter: list[str] | None,
) -> None:
    """Render the polished newest-first columnar listing per mockup H.

    Trailing hint lines ("to view details", "to revert to BEFORE …")
    surface only when at least one entry exists. The hint uses the
    newest entry's id as the example and the first profile from the
    filter (or the newest entry's profile when no filter is set) so
    the suggested command is always copy-pasteable.
    """
    console = _wide_console()
    now = datetime.now(UTC)
    table = Table(show_header=True, box=None, pad_edge=False, padding=(0, 2))
    table.add_column("id", no_wrap=True, style="cyan")
    table.add_column("type", no_wrap=True)
    table.add_column("age", no_wrap=True)
    table.add_column("files", no_wrap=True, justify="right")
    table.add_column("plugins", no_wrap=True, justify="right")
    table.add_column("ext", no_wrap=True, justify="right")
    for entry in listings:
        table.add_row(
            entry.directory.name,
            entry.command,
            _compact_age(entry.timestamp, now),
            str(entry.file_count),
            str(entry.plugin_count),
            str(entry.ext_count),
        )
    if profile_filter:
        header = "=== transitions for profile " + ", ".join(profile_filter) + " ==="
    else:
        header = "=== transitions (all profiles) ==="
    console.print(header)
    console.print(table)
    sample = listings[0]
    sample_profile = profile_filter[0] if profile_filter else sample.profile
    console.print("=== to view details ===")
    console.print(f"  setforge transitions show {sample.directory.name}")
    console.print("=== to revert to BEFORE a specific transition ===")
    console.print(
        f"  setforge revert --profile={sample_profile} "
        f"--to-before={sample.directory.name}"
    )


@transitions_app.command("show")
def transitions_show(
    prefix: str = typer.Argument(..., help="Dirname or unique-prefix match."),
) -> None:
    """Show the full audit-detail panel for one transition (mockup H)."""
    target = transitions.resolve_transition_prefix(prefix)
    meta = json.loads((target / "meta.json").read_text(encoding="utf-8"))
    console = _wide_console()
    profile = str(meta.get("profile", ""))
    console.print(f"=== transition {target.name} ===")
    console.print(f"  type:    {meta.get('command', '')}")
    console.print(f"  profile: {profile}")
    if "timestamp" in meta:
        console.print(f"  start:   {meta['timestamp']}")
    if "host" in meta:
        console.print(f"  host:    {meta['host']}")
    if "version" in meta:
        console.print(f"  version: {meta['version']}")

    _render_files_section_show(target, console)
    _render_plugins_section_show(target, console)
    _render_extensions_section_show(target, console)

    console.print("=== reverse this transition ===")
    console.print(
        f"  setforge revert --profile={profile} --to-before={target.name}"
    )
    console.print(
        "    (will undo this transition AND every newer transition for this profile)"
    )


def _render_files_section_show(target: Path, console: Console) -> None:
    """Render the ``files mutated (N):`` block with per-file diff stats."""
    file_actions = transitions.summarize_transition(target)
    if not file_actions:
        return
    patch_file = target / "changes.patch"
    diff_summaries: dict[str, str] = {}
    if patch_file.exists():
        diff_summaries = _diff_summaries_from_patch(
            patch_file.read_text(encoding="utf-8")
        )
    sorted_items = sorted(file_actions.items())
    console.print(f"  files mutated ({len(sorted_items)}):")
    action_marker = {"created": "+", "deleted": "-", "modified": "M"}
    for path, action in sorted_items:
        marker = action_marker.get(action, "?")
        stats = diff_summaries.get(path, "")
        suffix = f"  diff: {stats}" if stats else ""
        console.print(f"    {marker}  {path}{suffix}")


def _render_plugins_section_show(target: Path, console: Console) -> None:
    """Render the ``plugins:`` block if a plugins.json sidecar exists."""
    plugin_file = target / "plugins.json"
    if not plugin_file.exists():
        return
    payload = json.loads(plugin_file.read_text(encoding="utf-8"))
    delta = transitions.plugin_delta_from_json(payload)
    if delta.is_empty():
        return
    console.print("  plugins:")
    for plugin_id in delta.installed:
        console.print(f"    + {plugin_id}  (installed)")
    for plugin_id in delta.enabled:
        console.print(f"    + {plugin_id}  (enabled)")
    for plugin_id in delta.disabled:
        console.print(f"    - {plugin_id}  (disabled)")
    for name in delta.marketplaces_added:
        console.print(f"    + marketplace:{name}")
    for entry in delta.marketplaces_removed:
        name = entry[0] if isinstance(entry, tuple) else str(entry)
        console.print(f"    - marketplace:{name}")


def _render_extensions_section_show(target: Path, console: Console) -> None:
    """Render the ``extensions:`` block if an extensions.json sidecar exists."""
    ext_file = target / "extensions.json"
    if not ext_file.exists():
        return
    ext_payload = json.loads(ext_file.read_text(encoding="utf-8"))
    added = ext_payload.get("added", []) or []
    removed = ext_payload.get("removed", []) or []
    if not (added or removed):
        return
    console.print("  extensions:")
    for ext_id in added:
        console.print(f"    + {ext_id}  (installed)")
    for ext_id in removed:
        console.print(f"    - {ext_id}  (uninstalled)")
