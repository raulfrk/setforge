"""Helpers for setforge.cli.install — module-private.

Three helpers extracted from ``install()`` body per setforge-pm6:

- :func:`_check_unexpected_drift`: drift gate + wizard hand-off + :class:`typer.Exit`
  on no-resolve.
- :func:`_deploy_all_tracked_files`: per-tracked-file
  :func:`setforge.deploy.copy_atomic` loop + tracked-baseline stamp.
- :func:`_write_install_transition`: snapshot +
  :func:`setforge.transitions.write_transition` wrapper that returns
  the written target path.

NO ``@app.command`` decorators; NO ``app`` import — this module is
internal-only and stays out of typer's command surface.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import typer

from setforge import (
    compare as compare_mod,
)
from setforge import (
    deploy,
    section_reconcile,
    transitions,
)
from setforge import (
    merge as merge_mod,
)
from setforge.cli._helpers import _iter_all_tracked_files
from setforge.compare import CompareStatus
from setforge.config import Config, ResolvedProfile
from setforge.sections import LiveSections


def _check_unexpected_drift(
    drift_report: compare_mod.CompareReport,
    cfg: Config,
    repo_root: Path,
    config: Path,
    profile: str,
    *,
    auto_accept_tracked: bool,
    auto_accept_live: bool,
) -> None:
    """Resolve any unexpected drift before deploy, or raise :class:`typer.Exit`.

    Branches:

    - ``auto_accept_tracked``: run the merge wizard with ``auto_accept='k'``
      (overwrite live with tracked).
    - ``auto_accept_live``: run the merge wizard with ``auto_accept='u'``
      (update tracked from live).
    - neither: print an actionable error pointing at ``setforge merge`` and
      raise ``typer.Exit(1)``.

    No-op when no ``DRIFTED`` entry carries unexpected-drift keys.
    """
    has_real_unexpected = any(
        e.status == CompareStatus.DRIFTED and e.unexpected_drift_keys
        for e in drift_report.entries
    )
    if not has_real_unexpected:
        return

    unexpected_count = sum(
        1
        for e in drift_report.entries
        if e.status == CompareStatus.DRIFTED and e.unexpected_drift_keys
    )
    if auto_accept_tracked:
        merge_mod.run_wizard(
            drift_report,
            cfg,
            repo_root,
            setforge_yaml_path=config.resolve(),
            profile=profile,
            auto_accept="k",
        )
    elif auto_accept_live:
        merge_mod.run_wizard(
            drift_report,
            cfg,
            repo_root,
            setforge_yaml_path=config.resolve(),
            profile=profile,
            auto_accept="u",
        )
    else:
        typer.secho(
            f"unexpected drift in {unexpected_count} file(s): "
            f"run 'setforge merge --profile={profile}' to resolve, "
            f"or pass --auto-accept-tracked or --auto-accept-live",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)


def _deploy_all_tracked_files(
    cfg: Config,
    resolved: ResolvedProfile,
    repo_root: Path,
    *,
    section_decisions: Mapping[Path, dict[str, str]],
    live_sections_map: Mapping[Path, LiveSections],
) -> None:
    """Deploy each tracked_file via :func:`deploy.copy_atomic` + stamp baselines.

    Echoes the per-file ``copy_atomic`` action to stdout (preserving the
    pre-refactor format) and stamps tracked-side embedded section hashes
    after each ``preserve_user_sections`` deploy so the three-way
    classifier has a baseline on the next install.
    """
    for tracked_file, sub_src, sub_dst in _iter_all_tracked_files(
        cfg, resolved, repo_root
    ):
        override = section_decisions.get(sub_dst)
        precomputed = live_sections_map.get(sub_dst)
        result = deploy.copy_atomic(
            sub_src,
            sub_dst,
            preserve_user_sections=tracked_file.preserve_user_sections,
            preserve_user_keys=tracked_file.preserve_user_keys or None,
            preserve_user_keys_deep=tracked_file.preserve_user_keys_deep or None,
            section_bodies_override=override,
            precomputed_live_sections=precomputed,
        )
        typer.echo(f"{result.action.value:>8}  {sub_dst}")
        if tracked_file.preserve_user_sections:
            section_reconcile.stamp_tracked_baseline(sub_src)


def _write_install_transition(
    profile: str,
    file_pre: Mapping[Path, str | None],
    file_post: Mapping[Path, str | None],
    ext_delta: transitions.ExtensionDelta | None,
    plugin_delta: transitions.PluginDelta | None,
) -> Path:
    """Write the install transition record; return the target directory path."""
    return transitions.write_transition(
        transitions.make_meta(transitions.TransitionCommand.INSTALL, profile),
        file_pre,
        file_post,
        ext_delta,
        plugin_delta=plugin_delta,
    )
