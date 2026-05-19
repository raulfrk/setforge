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
    transitions,
    vscode_extensions,
)
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
from setforge.cli._helpers import (
    ProfileContext,
    _iter_all_tracked_files,
    _parse_capture_auto,
    _refuse_legacy_live_markers,
    _resolve_drift_paths,
)
from setforge.config import Config, load_config, resolve_profile
from setforge.errors import CaptureRequiresInteractive, ExtensionToolMissing


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


@app.command()
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


@app.command()
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
    cfg = load_config(config)
    repo_root = config.resolve().parent
    resolved = resolve_profile(cfg, profile)
    ctx = ProfileContext(
        cfg=cfg, resolved=resolved, repo_root=repo_root, profile=profile
    )
    _refuse_legacy_live_markers(ctx, command="merge")
    report = compare_mod.compare_profile(cfg, profile, repo_root)

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


@app.command()
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
    top-level drift. Pass ``--auto=use-live`` (pre-`nen.23` silent
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

    if not no_transition:
        transitions.ensure_state_dir_writable()

    # bviv: confirmation gate for sync --auto=use-live (live → tracked).
    if auto_enum is capture_mod.CaptureAuto.USE_LIVE:
        drift_report = compare_mod.compare_profile(cfg, profile, repo_root)
        plan = _build_capture_plan(
            drift_report=drift_report,
            ctx=ctx,
        )
        if not confirm_auto_operation(
            command="sync --auto=use-live",
            profile=profile,
            plan=plan,
            yes=yes,
        ):
            raise typer.Exit(0)

    src_paths = _sync_snapshot_paths(ctx, config)
    file_pre = transitions.snapshot_paths(src_paths)

    results = _run_capture(cfg, profile, repo_root, config, auto_enum, command="sync")
    for result in results:
        typer.echo(f"{result.action.value:>8}  {result.name}")

    _capture_extensions(config, profile)

    file_post = transitions.snapshot_paths(src_paths)
    if not no_transition:
        target = transitions.write_transition(
            transitions.make_meta(transitions.TransitionCommand.SYNC, profile),
            file_pre,
            file_post,
            None,  # sync's extension change is reflected in the YAML diff
        )
        typer.echo(f"transition: {target}")
        typer.echo(f"↩  revert with: setforge revert --profile={profile}")


def _sync_snapshot_paths(ctx: ProfileContext, config: Path) -> list[Path]:
    """Every tracked src path under the profile plus ``setforge.yaml`` itself."""
    paths = [sub_src for _, sub_src, _ in _iter_all_tracked_files(ctx)]
    paths.append(config.resolve())
    return paths


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
    """
    try:
        return capture_mod.capture_profile(
            cfg,
            profile,
            repo_root,
            setforge_yaml_path=config.resolve(),
            auto=auto_enum,
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
