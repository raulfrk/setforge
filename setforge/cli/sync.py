"""capture / merge / sync subcommands — live → tracked capture flow.

- ``capture`` and ``sync`` drive the ``capture_mod.capture_profile``
  pipeline; the merge wizard fires interactively on drift, with
  ``--auto={use-live,keep-tracked}`` as the non-interactive escape.
  ``capture`` is the pipeline alone; ``sync`` also records a transition
  so ``revert`` can replay it.
- ``merge`` runs the merge wizard standalone via
  ``merge_mod.run_wizard`` — no profile capture, no transition. It has
  its own ``--tracked_file`` filter; no ``--auto`` option (the wizard
  is always interactive for the merge subcommand).
"""

import sys
from datetime import UTC
from pathlib import Path

import typer

from setforge import (
    capture as capture_mod,
)
from setforge import (
    compare as compare_mod,
)
from setforge import (
    merge as merge_mod,
)
from setforge import (
    section_wizard,
    transitions,
    vscode_extensions,
)
from setforge._redact import redact_argv
from setforge.cli import (
    _CONFIG_OPTION,
    _PROFILE_OPTION,
    _resolve_config_arg,
    app,
)
from setforge.cli._confirm import (
    AutoDirection,
    AutoPlan,
    FileChange,
    confirm_auto_operation,
)
from setforge.cli._help_examples import (
    CAPTURE_EXAMPLES,
    MERGE_EXAMPLES,
    SYNC_EXAMPLES,
)
from setforge.cli._helpers import (
    ProfileContext,
    _iter_all_tracked_files,
    _parse_capture_auto,
    _refuse_legacy_live_markers,
    _resolve_drift_paths,
)
from setforge.cli._install_helpers import (
    _compute_preserve_user_keys_applied,
    _load_validated_host_local_sections,
)
from setforge.compare import resolve_dst, resolve_src
from setforge.config import Config, load_config, resolve_profile
from setforge.errors import (
    CaptureRequiresInteractive,
    ConfigError,
    ExtensionToolMissing,
    NoSourceConfigured,
    SourceNotCloned,
)
from setforge.locking import profile_lock
from setforge.source import (
    LOCAL_CONFIG_PATH,
    get_resolved_source,
    load_local_host_local_sections,
)


def _build_capture_plan(
    *,
    drift_report: compare_mod.CompareReport,
    ctx: ProfileContext,
) -> AutoPlan:
    """Build an AutoPlan for the live → tracked capture path (sync --auto=use-live).

    Joins the ``CompareReport.entries`` (entries marked DRIFTED with any
    drift keys) against the tracked_file path map. Direction is always
    LIVE_TO_TRACKED — sync absorbs live edits.
    """
    file_changes: list[FileChange] = []
    # sync absorbs ANY drift on use-live (not just unexpected) — the
    # capture writes everything that differs into tracked. The shared
    # ``_resolve_drift_paths`` helper already filters to entries with
    # ``unexpected_drift_keys or diff``, which matches sync's intent.
    for entry, sub_src, sub_dst in _resolve_drift_paths(drift_report, ctx):
        file_changes.append(
            FileChange(
                source=sub_dst,
                dest=sub_src,
                changed=max(len(entry.unexpected_drift_keys), 1),
            ),
        )
    if not file_changes:
        return AutoPlan(
            direction=AutoDirection.LIVE_TO_TRACKED,
            file_changes=(),
            risks=(),
            revert_command=f"setforge revert --profile={ctx.profile}",
        )
    return AutoPlan(
        direction=AutoDirection.LIVE_TO_TRACKED,
        file_changes=tuple(file_changes),
        risks=(
            f"tracked-side files on {len(file_changes)} file(s) will be overwritten "
            "with live edits — propagates to all hosts via the tracked repo",
        ),
        revert_command=f"setforge revert --profile={ctx.profile}",
    )


@app.command(epilog=CAPTURE_EXAMPLES)
def capture(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    auto: str | None = typer.Option(
        None,
        "--auto",
        help=(
            "Non-interactive resolution for capture-time drift: "
            "'use-live' absorbs all drift (today's silent-absorb "
            "behavior), 'keep-tracked' rejects all drift."
        ),
    ),
) -> None:
    """Capture live → tracked for every tracked_file in the profile.

    When tracked declares ``preserve_user_keys_deep`` or carries
    non-preserve top-level drift, the merge wizard fires interactively;
    pass ``--auto={use-live, keep-tracked}`` for non-interactive
    contexts.
    """
    config = _resolve_config_arg(config)
    auto_enum = _parse_capture_auto(auto)

    cfg = load_config(config)
    repo_root = config.resolve().parent
    results = _run_capture(
        cfg, profile, repo_root, config, auto_enum, command="capture"
    )
    for result in results:
        typer.echo(f"{result.action.value:>8}  {result.name}")


@app.command(epilog=MERGE_EXAMPLES)
def merge(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    tracked_file: str | None = typer.Option(
        None,
        "--tracked_file",
        help="Narrow the walk to one tracked_file entry key.",
    ),
) -> None:
    """Interactively resolve unexpected drift for every tracked_file in the profile.

    Exits 0 with a "no unexpected drift; nothing to do." message when
    ``compare`` reports no unexpected drift — the wizard runs only when
    there's work to do.
    """
    config = _resolve_config_arg(config)
    cfg = load_config(config)
    repo_root = config.resolve().parent
    resolved = resolve_profile(cfg, profile)
    ctx = ProfileContext(
        cfg=cfg, resolved=resolved, repo_root=repo_root, profile=profile
    )
    _refuse_legacy_live_markers(ctx, command="merge")
    # Round-2: thread the local.yaml host_local_sections
    # overlay so the merge wizard's drift display does NOT surface
    # already-injected host-local sections as DRIFTED (false-positive
    # against the live file that just received them via install).
    host_local_sections_map = _load_validated_host_local_sections(
        cfg, resolved, repo_root
    )
    report = compare_mod.compare_profile(
        cfg, profile, repo_root, host_local_sections=host_local_sections_map
    )

    if not report.has_unexpected_drift:
        typer.echo("no unexpected drift; nothing to do.")
        raise typer.Exit(0)

    try:
        merge_mod.run_wizard(
            report,
            cfg,
            repo_root,
            setforge_yaml_path=config.resolve(),
            profile=profile,
            tracked_file_filter=tracked_file,
        )
    except KeyboardInterrupt:
        typer.secho(
            "merge cancelled (Ctrl-C); files restored from snapshot",
            err=True,
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(130) from None


@app.command(epilog=SYNC_EXAMPLES)
def sync(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    no_transition: bool = typer.Option(
        False,
        "--no-transition",
        hidden=True,
        help="Skip writing a transition record (testing / debugging).",
    ),
    auto: str | None = typer.Option(
        None,
        "--auto",
        help=(
            "Non-interactive capture-time drift resolution: 'use-live' "
            "absorbs all drift; 'keep-tracked' rejects all drift. Without "
            "TTY and without --auto, sync exits 1 with "
            "CaptureRequiresInteractive."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the --auto=use-live confirmation prompt (for non-interactive use).",
    ),
) -> None:
    """Capture live → tracked for tracked_files and extensions.

    Symmetric with ``setforge install``'s drift gate: the merge wizard
    fires interactively for ``preserve_user_keys_deep`` and non-preserve
    top-level drift. Pass ``--auto=use-live`` (pre-`capture-wizard` silent
    absorb) or ``--auto=keep-tracked`` (refuse) for scripted runs.
    """
    config = _resolve_config_arg(config)
    auto_enum = _parse_capture_auto(auto)

    cfg = load_config(config)
    repo_root = config.resolve().parent
    resolved = resolve_profile(cfg, profile)
    ctx = ProfileContext(
        cfg=cfg, resolved=resolved, repo_root=repo_root, profile=profile
    )
    _refuse_legacy_live_markers(ctx, command="sync")

    with profile_lock(profile):
        if not no_transition:
            transitions.ensure_state_dir_writable()

        _run_capture_confirm_gate(ctx, auto_enum=auto_enum, yes=yes)

        _run_promote_wizard(
            ctx,
            auto_enum=auto_enum,
            no_transition=no_transition,
        )

        src_paths = _sync_snapshot_paths(ctx, config)
        file_pre = transitions.snapshot_paths(src_paths)

        results = _run_capture(
            cfg, profile, repo_root, config, auto_enum, command="sync"
        )
        for result in results:
            typer.echo(f"{result.action.value:>8}  {result.name}")

        _capture_extensions(config, profile)

        file_post = transitions.snapshot_paths(src_paths)
        if not no_transition:
            _write_sync_transition(
                ctx,
                file_pre=file_pre,
                file_post=file_post,
            )


def _run_capture_confirm_gate(
    ctx: ProfileContext,
    *,
    auto_enum: capture_mod.CaptureAuto | None,
    yes: bool,
) -> None:
    """Run the auto-confirm ``sync --auto=use-live`` drift-confirm gate.

    No-op unless ``auto_enum`` is :attr:`CaptureAuto.USE_LIVE`. Compares
    live vs tracked with the host_local_sections overlay threaded
    (round-2 so injected host-local sections do not
    inflate the drift count), renders the auto-operation confirm panel,
    and exits 0 cleanly when the user declines.
    """
    if auto_enum is not capture_mod.CaptureAuto.USE_LIVE:
        return
    host_local_sections_map = _load_validated_host_local_sections(
        ctx.cfg, ctx.resolved, ctx.repo_root
    )
    drift_report = compare_mod.compare_profile(
        ctx.cfg,
        ctx.profile,
        ctx.repo_root,
        host_local_sections=host_local_sections_map,
    )
    plan = _build_capture_plan(drift_report=drift_report, ctx=ctx)
    if not confirm_auto_operation(
        command="sync --auto=use-live",
        profile=ctx.profile,
        plan=plan,
        yes=yes,
    ):
        raise typer.Exit(0)


def _write_sync_transition(
    ctx: ProfileContext,
    *,
    file_pre: dict[Path, str | None],
    file_post: dict[Path, str | None],
) -> None:
    """Write the SYNC transition record + echo the user-visible breadcrumb.

    Encapsulates the :func:`transitions.write_transition` call (with
    the redacted argv, end timestamp, and preserve_user_keys_applied
    metadata) and the trailing ``transition: ...`` /
    ``↩  revert with: ...`` echoes so the caller body stays a flat
    capture-and-write skeleton.

    Skips the write entirely when capture produced no file mutations
    (``file_pre == file_post``). An empty SYNC transition would shadow a
    preceding ``TransitionCommand.PROMOTE`` record in
    :func:`transitions.load_latest`, so ``setforge revert`` after a
    sync-with-promote would reverse the no-op SYNC instead of the
    promote (round-4 round-trip regression).
    """
    if file_pre == file_post:
        return
    target = transitions.write_transition(
        transitions.make_meta(
            transitions.TransitionCommand.SYNC,
            ctx.profile,
            end_timestamp=transitions.now_utc().astimezone(UTC).isoformat(),
            command_line=redact_argv(sys.argv[1:]),
            preserve_user_keys_applied=_compute_preserve_user_keys_applied(ctx),
        ),
        file_pre,
        file_post,
        None,  # sync's extension change is reflected in the YAML diff
    )
    typer.echo(f"transition: {target}")
    typer.echo(f"↩  revert with: setforge revert --profile={ctx.profile}")


def _sync_snapshot_paths(
    ctx: ProfileContext,
    config: Path,
) -> list[Path]:
    """Every tracked src path under the profile plus ``setforge.yaml`` itself.

    Excludes :data:`LOCAL_CONFIG_PATH`: the promote wizard fires (and
    mutates local.yaml) BEFORE this function is called, so any
    file_pre/file_post snapshot captured here would be byte-identical.
    The promote path's own ``TransitionCommand.PROMOTE`` record
    (written inside ``_run_promote_wizard``) snapshots local.yaml
    pre-mutation, so ``revert`` rolls the drop back independently of
    the surrounding SYNC transition.
    """
    paths = [sub_src for _, _, sub_src, _ in _iter_all_tracked_files(ctx)]
    paths.append(config.resolve())
    return paths


def _run_promote_wizard(
    ctx: ProfileContext,
    *,
    auto_enum: capture_mod.CaptureAuto | None,
    no_transition: bool,
) -> list[section_wizard.PromoteOutcome]:
    """Walk host-local promotables; prompt + dispatch promote per spec auto-promote.

    Skipped when ``--auto`` is set (sync's non-interactive paths cannot
    drive the radiolist confirm dialog) or when no overlays are
    declared. Each successful promote is recorded as a standalone
    ``TransitionCommand.PROMOTE`` transition so ``setforge revert``
    rolls the multi-file mutation back independently of the surrounding
    SYNC transition.
    """
    overlays = load_local_host_local_sections()
    if not overlays:
        return []
    if auto_enum is not None:
        # Non-interactive sync: keep all promotables host-local
        # silently. The wizard's prompt requires a TTY.
        return []

    if not no_transition:
        transitions.ensure_state_dir_writable()
    snapshot_base = transitions.state_root() / "snapshots"
    snapshot_base.mkdir(parents=True, exist_ok=True)

    # Narrow the source-resolution failure modes that legitimately
    # short-circuit promote (no source configured, source clone missing,
    # malformed local.yaml). Anything else propagates — silently skipping
    # `check_source_clean` for an unknown error would nullify the
    # anti-smell 13 safety gate. When we DO catch one of the expected
    # cases, warn the user so the dirty-checkout skip is visible.
    try:
        source = get_resolved_source()
    except (NoSourceConfigured, SourceNotCloned, ConfigError) as exc:
        typer.secho(
            f"warning: source unresolved ({exc.__class__.__name__}); "
            "skipping check_source_clean. Promote will not detect a "
            "dirty source-repo checkout.",
            err=True,
            fg=typer.colors.YELLOW,
        )
        source = None

    tracked_files = _iter_promotable_tracked_files(ctx)
    if not tracked_files:
        return []

    outcomes = section_wizard.run_host_local_promote_wizard(
        tracked_files=tracked_files,
        overlays=overlays,
        local_yaml_path=LOCAL_CONFIG_PATH,
        profile=ctx.profile,
        snapshot_base=snapshot_base,
        source=source,
        interactive=sys.stdin.isatty(),
    )

    if no_transition:
        return outcomes
    for outcome in outcomes:
        if outcome.action is not section_wizard.SectionAction.PROMOTE:
            continue
        assert outcome.file_pre is not None
        assert outcome.file_post is not None
        target = transitions.write_transition(
            transitions.make_meta(
                transitions.TransitionCommand.PROMOTE,
                ctx.profile,
                end_timestamp=transitions.now_utc().astimezone(UTC).isoformat(),
                command_line=redact_argv(sys.argv[1:]),
            ),
            outcome.file_pre,
            outcome.file_post,
            None,
        )
        typer.echo(f"promote transition: {target}")
    return outcomes


def _iter_promotable_tracked_files(
    ctx: ProfileContext,
) -> list[tuple[str, Path, Path]]:
    """Yield ``(tracked_file_id, tracked_path, live_path)`` per profile entry.

    Built from ``ctx.resolved.tracked_files`` so the iteration order
    matches the user-visible profile order. Symlink-deployed
    tracked_files use the symlink path as ``live_path`` — the wizard's
    promote dispatch reads/writes through it identically.
    """
    out: list[tuple[str, Path, Path]] = []
    for name in ctx.resolved.tracked_files:
        tracked_file = ctx.cfg.tracked_files[name]
        if not tracked_file.preserve_user_sections:
            continue
        src = resolve_src(tracked_file, ctx.repo_root)
        dst = resolve_dst(tracked_file)
        out.append((name, src, dst))
    return out


def _capture_extensions(config: Path, profile: str) -> None:
    """Capture vscode-extension include changes; surface tool-missing as a warning."""
    try:
        changed = vscode_extensions.capture_extensions(config, profile)
    except ExtensionToolMissing as exc:
        typer.secho(
            f"warning: skipping extension capture — {exc}",
            err=True,
            fg=typer.colors.YELLOW,
        )
        return
    typer.echo(f"extensions: include {'updated' if changed else 'unchanged'}")


def _run_capture(
    cfg: Config,
    profile: str,
    repo_root: Path,
    config: Path,
    auto_enum: capture_mod.CaptureAuto | None,
    *,
    command: str,
) -> list[capture_mod.CaptureResult]:
    """Run ``capture_profile`` with the standard CLI error mapping.

    Centralizes the ``CaptureRequiresInteractive`` → exit-1 and
    ``KeyboardInterrupt`` → exit-130 mapping shared between
    :func:`capture` and :func:`sync`. ``command`` names the caller so
    the Ctrl-C message reads "<command> cancelled".

    Loads the local.yaml host_local_sections overlay so
    capture-back filters out the names install would have injected from
    local.yaml. Without this, host-local marker pairs in the live file
    round-trip into tracked sources on the next sync.
    """
    host_local_sections_map = load_local_host_local_sections()
    try:
        return capture_mod.capture_profile(
            cfg,
            profile,
            repo_root,
            setforge_yaml_path=config.resolve(),
            auto=auto_enum,
            host_local_sections_map=host_local_sections_map,
        )
    except CaptureRequiresInteractive as exc:
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    except KeyboardInterrupt:
        typer.secho(
            f"{command} cancelled (Ctrl-C); files restored from snapshot",
            err=True,
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(130) from None
