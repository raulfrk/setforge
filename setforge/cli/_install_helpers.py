"""Helpers for setforge.cli.install — module-private.

Helpers extracted from ``install()`` body:

- :func:`_check_unexpected_drift`: drift gate + wizard hand-off + :class:`typer.Exit`
  on no-resolve.
- :func:`_deploy_all_tracked_files`: per-tracked-file
  :func:`setforge.deploy.copy_atomic` loop + tracked-baseline stamp.
- :func:`_write_install_transition`: snapshot +
  :func:`setforge.transitions.write_transition` wrapper that returns
  the written target path.
- :func:`_confirm_legacy_drift_or_exit` /
  :func:`_confirm_section_reconcile_or_exit`: bviv confirm-or-exit
  wrappers that pair a plan-builder with
  :func:`setforge.cli._confirm.confirm_auto_operation`.
- :func:`_build_unexpected_drift_plan` /
  :func:`_build_shared_section_plan`: AutoPlan builders used by the
  confirm-or-exit helpers above.

NO ``@app.command`` decorators; NO ``app`` import — this module is
internal-only and stays out of typer's command surface.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import assert_never

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
from setforge.cli._confirm import (
    AutoDirection,
    AutoPlan,
    FileChange,
    confirm_auto_operation,
)
from setforge.cli._helpers import (
    ProfileContext,
    _iter_all_tracked_files,
    _iter_section_tracked_files,
    _resolve_drift_paths,
)
from setforge.compare import CompareStatus
from setforge.section_reconcile import SectionDriftState
from setforge.section_wizard import ReconcileAuto
from setforge.sections import LiveSections, SectionSemantics


def _check_unexpected_drift(
    drift_report: compare_mod.CompareReport,
    ctx: ProfileContext,
    config: Path,
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
            ctx.cfg,
            ctx.repo_root,
            setforge_yaml_path=config.resolve(),
            profile=ctx.profile,
            auto_accept="k",
        )
    elif auto_accept_live:
        merge_mod.run_wizard(
            drift_report,
            ctx.cfg,
            ctx.repo_root,
            setforge_yaml_path=config.resolve(),
            profile=ctx.profile,
            auto_accept="u",
        )
    else:
        typer.secho(
            f"unexpected drift in {unexpected_count} file(s): "
            f"run 'setforge merge --profile={ctx.profile}' to resolve, "
            f"or pass --auto-accept-tracked or --auto-accept-live",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)


def _deploy_all_tracked_files(
    ctx: ProfileContext,
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
    for tracked_file, sub_src, sub_dst in _iter_all_tracked_files(ctx):
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


def _build_unexpected_drift_plan(
    *,
    drift_report: compare_mod.CompareReport,
    ctx: ProfileContext,
    direction: AutoDirection,
) -> AutoPlan:
    """Build an AutoPlan from a drift report for legacy --auto-accept-* paths.

    Delegates name → (sub_src, sub_dst) resolution to the shared
    ``_resolve_drift_paths`` helper, then filters to entries with
    ``unexpected_drift_keys`` (install's legacy gate ignores
    diff-only entries). ``changed`` is the number of unexpected keys.
    """
    file_changes: list[FileChange] = []
    for entry, sub_src, sub_dst in _resolve_drift_paths(drift_report, ctx):
        # install's legacy --auto-accept-* gate ONLY surfaces entries
        # with unexpected-drift keys; entries that drift only via diff
        # body fall through to the bare-install warning path.
        if not entry.unexpected_drift_keys:
            continue
        match direction:
            case AutoDirection.TRACKED_TO_LIVE:
                source, dest = sub_src, sub_dst
            case AutoDirection.LIVE_TO_TRACKED:
                source, dest = sub_dst, sub_src
            case _ as never:
                assert_never(never)
        file_changes.append(
            FileChange(
                source=source,
                dest=dest,
                changed=len(entry.unexpected_drift_keys),
            ),
        )
    if not file_changes:
        return AutoPlan(
            direction=direction,
            file_changes=(),
            risks=(),
            revert_command=f"setforge revert --profile={ctx.profile}",
        )
    risk_target = "live" if direction is AutoDirection.TRACKED_TO_LIVE else "tracked"
    return AutoPlan(
        direction=direction,
        file_changes=tuple(file_changes),
        risks=(
            f"{risk_target} values on {len(file_changes)} file(s) will be overwritten",
            # The gate fires AFTER the unexpected-drift filter, which
            # already excludes preserve_user_keys overlays — surface
            # that reassurance to the user.
            "host-local keys covered by preserve_user_keys are not affected",
        ),
        revert_command=f"setforge revert --profile={ctx.profile}",
    )


def _build_shared_section_plan(*, ctx: ProfileContext) -> AutoPlan:
    """Build an AutoPlan from shared-section drift across tracked markdown files.

    Walks ``_iter_section_tracked_files`` and runs
    ``classify_section_drift`` on each pair, collecting tracked_files where
    any ``shared`` section has a non-``NO_DRIFT`` state. The plan's
    ``changed`` column counts drifted shared sections per file.
    """
    file_changes: list[FileChange] = []
    for sub_src, sub_dst in _iter_section_tracked_files(ctx):
        if not sub_dst.exists():
            continue
        tracked_text = sub_src.read_text(encoding="utf-8")
        live_text = sub_dst.read_text(encoding="utf-8")
        drifts = section_reconcile.classify_section_drift(tracked_text, live_text)
        shared_drifted = [
            d
            for d in drifts.values()
            if d.semantics is SectionSemantics.SHARED
            and d.state is not SectionDriftState.NO_DRIFT
        ]
        if not shared_drifted:
            continue
        file_changes.append(
            FileChange(
                source=sub_src,
                dest=sub_dst,
                changed=len(shared_drifted),
            ),
        )
    if not file_changes:
        return AutoPlan(
            direction=AutoDirection.TRACKED_TO_LIVE,
            file_changes=(),
            risks=(),
            revert_command=f"setforge revert --profile={ctx.profile}",
        )
    return AutoPlan(
        direction=AutoDirection.TRACKED_TO_LIVE,
        file_changes=tuple(file_changes),
        risks=(
            f"shared user-section bodies on {len(file_changes)} file(s) "
            "will be overwritten with tracked-side content",
            # The gate only surfaces ``shared`` sections; ``host-local``
            # sections never participate in section reconcile and stay
            # untouched regardless of --auto* flag.
            "host-local sections are not affected",
        ),
        revert_command=f"setforge revert --profile={ctx.profile}",
    )


def _confirm_legacy_drift_or_exit(
    *,
    drift_report: compare_mod.CompareReport,
    ctx: ProfileContext,
    auto_accept_tracked: bool,
    auto_accept_live: bool,
    yes: bool,
) -> None:
    """Render the legacy unexpected-drift confirm wizard; ``typer.Exit(0)`` on decline.

    Wraps the ``install --auto-accept-{tracked,live}`` confirm block.
    No-op when neither flag is set.
    """
    if not (auto_accept_tracked or auto_accept_live):
        return
    direction = (
        AutoDirection.TRACKED_TO_LIVE
        if auto_accept_tracked
        else AutoDirection.LIVE_TO_TRACKED
    )
    flag = "--auto-accept-tracked" if auto_accept_tracked else "--auto-accept-live"
    plan = _build_unexpected_drift_plan(
        drift_report=drift_report,
        ctx=ctx,
        direction=direction,
    )
    if not confirm_auto_operation(
        command=f"install {flag}",
        profile=ctx.profile,
        plan=plan,
        yes=yes,
    ):
        raise typer.Exit(0)


def _confirm_section_reconcile_or_exit(
    *,
    ctx: ProfileContext,
    section_auto: ReconcileAuto | None,
    yes: bool,
) -> None:
    """Render the section-reconcile confirm wizard; ``typer.Exit(0)`` on decline.

    Wraps the ``install --auto=use-tracked`` confirm block. No-op
    unless ``section_auto`` is ``USE_TRACKED`` (the only mutating
    section-reconcile mode).
    """
    if section_auto is not ReconcileAuto.USE_TRACKED:
        return
    plan = _build_shared_section_plan(ctx=ctx)
    if not confirm_auto_operation(
        command="install --auto=use-tracked",
        profile=ctx.profile,
        plan=plan,
        yes=yes,
    ):
        raise typer.Exit(0)
