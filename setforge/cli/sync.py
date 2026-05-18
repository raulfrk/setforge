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
from setforge.cli._helpers import (
    _iter_all_tracked_files,
    _parse_capture_auto,
    _refuse_legacy_live_markers,
)
from setforge.config import load_config, resolve_profile
from setforge.errors import CaptureRequiresInteractive, ExtensionToolMissing


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
    try:
        results = capture_mod.capture_profile(
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
            "capture cancelled (Ctrl-C); files restored from snapshot",
            err=True,
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(130) from None
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
    _refuse_legacy_live_markers(cfg, resolved, repo_root, command="merge")
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
            "Non-interactive resolution for capture-time drift: "
            "'use-live' absorbs all drift (today's silent-absorb "
            "behavior), 'keep-tracked' rejects all drift. Without TTY "
            "and without --auto, sync exits 1 with "
            "CaptureRequiresInteractive."
        ),
    ),
) -> None:
    """Capture live → tracked for tracked_files and extensions.

    When tracked declares ``preserve_user_keys_deep`` or carries
    non-preserve top-level drift, the merge wizard fires interactively
    so you can review each diverged sub-key / top-level key; this
    behavior is symmetric with ``setforge install``'s drift gate. Pass
    ``--auto=use-live`` to reproduce the pre-`nen.23` silent-absorb
    behavior (e.g. for scripted runs) or ``--auto=keep-tracked`` to
    refuse to absorb drift.
    """
    config = _resolve_config_arg(config)
    auto_enum = _parse_capture_auto(auto)

    cfg = load_config(config)
    repo_root = config.resolve().parent
    resolved = resolve_profile(cfg, profile)
    _refuse_legacy_live_markers(cfg, resolved, repo_root, command="sync")

    if not no_transition:
        transitions.ensure_state_dir_writable()

    src_paths: list[Path] = [
        sub_src for _, sub_src, _ in _iter_all_tracked_files(cfg, resolved, repo_root)
    ]
    src_paths.append(config.resolve())

    file_pre = transitions.snapshot_paths(src_paths)

    try:
        results = capture_mod.capture_profile(
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
            "sync cancelled (Ctrl-C); files restored from snapshot",
            err=True,
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(130) from None
    for result in results:
        typer.echo(f"{result.action.value:>8}  {result.name}")

    try:
        changed = vscode_extensions.capture_extensions(config, profile)
    except ExtensionToolMissing as exc:
        typer.secho(
            f"warning: skipping extension capture — {exc}",
            err=True,
            fg=typer.colors.YELLOW,
        )
    else:
        typer.echo(f"extensions: include {'updated' if changed else 'unchanged'}")

    file_post = transitions.snapshot_paths(src_paths)

    if not no_transition:
        target = transitions.write_transition(
            transitions.make_meta(transitions.TransitionCommand.SYNC, profile),
            file_pre,
            file_post,
            None,  # sync's extension change is reflected in the YAML diff
        )
        typer.echo(f"transition: {target}")
