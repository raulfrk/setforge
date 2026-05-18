"""install subcommand — orchestrates tracked-file deploy + extension/plugin reconcile.

Wires section-marker reconcile, deploy.copy_atomic, extension/plugin
reconcile, and the transition snapshot. Imports ``app`` from
:mod:`setforge.cli` so the ``@app.command()`` registration fires at
module import time; ``setforge/cli/__init__.py`` imports this module at
the bottom for the side effect.
"""

from pathlib import Path

import typer

from setforge import (
    compare as compare_mod,
)
from setforge import (
    deploy,
    transitions,
)
from setforge.cli import (
    _CONFIG_OPTION,
    _PROFILE_OPTION,
    _resolve_config_arg,
    app,
)
from setforge.cli._helpers import (
    _extract_live_sections_map,
    _iter_all_tracked_files,
    _parse_section_auto,
    _resolve_section_decisions,
)
from setforge.cli._install_helpers import (
    _check_unexpected_drift,
    _deploy_all_tracked_files,
    _write_install_transition,
)
from setforge.cli._plugin_helpers import _reconcile_extensions, _reconcile_plugins
from setforge.config import load_config, resolve_profile


@app.command()
def install(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    no_transition: bool = typer.Option(
        False,
        "--no-transition",
        hidden=True,
        help="Skip writing a transition record (testing / debugging).",
    ),
    auto_accept_tracked: bool = typer.Option(
        False,
        "--auto-accept-tracked",
        help="Non-interactively resolve unexpected drift by keeping tracked values.",
    ),
    auto_accept_live: bool = typer.Option(
        False,
        "--auto-accept-live",
        help="Non-interactively resolve unexpected drift by adopting live values.",
    ),
    reconcile_user_sections: bool = typer.Option(
        False,
        "--reconcile-user-sections",
        help=(
            "Interactively reconcile drifted `shared` user-sections. "
            "Mutually exclusive with --auto."
        ),
    ),
    auto: str | None = typer.Option(
        None,
        "--auto",
        help=(
            "Non-interactive section reconciliation: 'use-tracked' "
            "deploys tracked-side updates into every shared section; "
            "'keep-live' silences shared-drift warnings and keeps live. "
            "Mutually exclusive with --reconcile-user-sections."
        ),
    ),
) -> None:
    """Deploy tracked → live for every tracked_file in the profile."""
    config = _resolve_config_arg(config)
    # Mutual-exclusivity guard for the legacy unexpected-drift flags.
    if auto_accept_tracked and auto_accept_live:
        typer.secho(
            "error: --auto-accept-tracked and --auto-accept-live are"
            " mutually exclusive",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(2)

    # Mutual-exclusivity guard for the new section-reconcile flags.
    section_auto = _parse_section_auto(auto, reconcile_user_sections)

    cfg = load_config(config)
    repo_root = config.resolve().parent
    resolved = resolve_profile(cfg, profile)

    if not no_transition:
        transitions.ensure_state_dir_writable()
    deploy.validate_srcs_exist(cfg, resolved, repo_root)
    deploy.bootstrap_local(resolved.bootstrap)

    # P4.3: check for unexpected drift before deploying.
    # Only DRIFTED entries (existing live files that diverge from tracked
    # in unexpected ways) gate install. MISSING entries are expected on
    # first install and are handled by deploy below.
    drift_report = compare_mod.compare_profile(cfg, profile, repo_root)
    _check_unexpected_drift(
        drift_report,
        cfg,
        repo_root,
        config,
        profile,
        auto_accept_tracked=auto_accept_tracked,
        auto_accept_live=auto_accept_live,
    )

    # Resolve user-section drift (shared sections) into per-tracked_file
    # decisions BEFORE the deploy loop so wizard prompts and the
    # bare-install warning fire once, deterministically.
    section_decisions = _resolve_section_decisions(
        cfg,
        resolved,
        repo_root,
        section_auto=section_auto,
        interactive=reconcile_user_sections,
    )

    # Pre-extract live user-sections for every section-bearing tracked_file
    # so deploy.copy_atomic can skip its own re-read + re-parse pass.
    # See `precomputed_live_sections` on copy_atomic.
    live_sections_map = _extract_live_sections_map(cfg, resolved, repo_root)

    dst_paths: list[Path] = [
        sub_dst for _, _, sub_dst in _iter_all_tracked_files(cfg, resolved, repo_root)
    ]
    dst_paths.extend(Path(str(p)).expanduser() for p in resolved.bootstrap)

    file_pre = transitions.snapshot_paths(dst_paths)

    _deploy_all_tracked_files(
        cfg,
        resolved,
        repo_root,
        section_decisions=section_decisions,
        live_sections_map=live_sections_map,
    )

    ext_delta = _reconcile_extensions(resolved)
    plugin_delta = _reconcile_plugins(cfg, resolved)

    file_post = transitions.snapshot_paths(dst_paths)

    if not no_transition:
        target = _write_install_transition(
            profile, file_pre, file_post, ext_delta, plugin_delta
        )
        typer.echo(f"transition: {target}")
