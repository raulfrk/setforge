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

import sys
from collections.abc import Mapping
from datetime import UTC
from pathlib import Path
from typing import assert_never

import typer

from setforge import (
    claude_plugins as claude_plugins_mod,
)
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
from setforge import (
    vscode_extensions as vscode_extensions_mod,
)
from setforge._redact import redact_argv
from setforge.cli._confirm import (
    AutoDirection,
    AutoPlan,
    FileChange,
    confirm_auto_operation,
)
from setforge.cli._helpers import (
    ProfileContext,
    _extract_live_sections_map,
    _iter_all_tracked_files,
    _iter_section_tracked_files,
    _resolve_drift_paths,
    _resolve_section_decisions,
)
from setforge.compare import CompareStatus
from setforge.errors import ExtensionToolMissing, PluginToolMissing, SetforgeError
from setforge.section_reconcile import SectionDriftState
from setforge.section_wizard import ReconcileAuto
from setforge.sections import LiveSections, SectionSemantics


def _compute_preserve_user_keys_applied(ctx: ProfileContext) -> bool | None:
    """Return whether any tracked_file declares a preserve_user_keys overlay.

    Approximates the SPEC 3 "applied" semantics at the granularity available
    without instrumenting :func:`setforge.deploy.copy_atomic`:

    - ``None`` — profile has no tracked_files; the concept doesn't apply.
    - ``True`` — at least one tracked_file declares ``preserve_user_keys``
      or ``preserve_user_keys_deep``; deploy.copy_atomic will exercise
      its overlay path for that file (matched or not, the overlay logic
      ran). Widening to "matched a live key" is a separate bd (per SPEC 3
      anti-pattern check 7).
    - ``False`` — tracked_files exist but none declare an overlay.
    """
    saw_tracked_file = False
    for tracked_file, _src, _dst in _iter_all_tracked_files(ctx):
        saw_tracked_file = True
        if tracked_file.preserve_user_keys or tracked_file.preserve_user_keys_deep:
            return True
    if not saw_tracked_file:
        return None
    return False


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
    if not (auto_accept_tracked or auto_accept_live):
        typer.secho(
            f"unexpected drift in {unexpected_count} file(s): "
            f"run 'setforge merge --profile={ctx.profile}' to resolve, "
            f"or pass --auto-accept-tracked or --auto-accept-live",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    # Both wizard branches share their entire call shape except for the
    # ``auto_accept`` sentinel ('k' keeps tracked, 'u' adopts live);
    # collapse the if/elif arms to a single dispatch on the flag.
    auto_accept = "k" if auto_accept_tracked else "u"
    merge_mod.run_wizard(
        drift_report,
        ctx.cfg,
        ctx.repo_root,
        setforge_yaml_path=config.resolve(),
        profile=ctx.profile,
        auto_accept=auto_accept,
    )


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
            mode=tracked_file.mode,
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
    *,
    source_dir: Path | None = None,
    reconcile_outcomes: tuple[transitions.ReconcileOutcome, ...] = (),
    preserve_user_keys_applied: bool | None = None,
) -> Path:
    """Write the install transition record; return the target directory path.

    Two arguments carry schema-bump backward-compat history: ``source_dir``
    (setforge-xra8 — when set and pointing at a git repo,
    :func:`transitions.make_meta` records HEAD's sha so ``setforge
    status`` can compute commits-since-last-install) and
    ``reconcile_outcomes`` (setforge-k0uj — defaults to empty so
    pre-bump callers keep working; when non-empty, serialized to
    ``reconcile_outcomes.json`` alongside ``extensions.json`` /
    ``plugins.json`` so ``install --retry-failed`` can rebuild the
    skipped-ids set on the next invocation).

    ``preserve_user_keys_applied`` is the setforge-8ohd schema-bump
    kwarg; computed by :func:`_compute_preserve_user_keys_applied` at
    the install call site. ``command_line`` is captured from
    ``sys.argv[1:]`` here (via :func:`setforge._redact.redact_argv`) so
    callers don't have to thread it through, and ``end_timestamp`` is
    stamped at the moment of write — both align with the spec's
    "stamp at the point the command body returns successfully" model.
    """
    return transitions.write_transition(
        transitions.make_meta(
            transitions.TransitionCommand.INSTALL,
            profile,
            source_dir=source_dir,
            end_timestamp=transitions.now_utc().astimezone(UTC).isoformat(),
            command_line=redact_argv(sys.argv[1:]),
            preserve_user_keys_applied=preserve_user_keys_applied,
        ),
        file_pre,
        file_post,
        ext_delta,
        plugin_delta=plugin_delta,
        reconcile_outcomes=reconcile_outcomes,
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
        try:
            live_text = sub_dst.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        tracked_text = sub_src.read_text(encoding="utf-8")
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


def _run_predeploy_gates(
    *,
    drift_report: compare_mod.CompareReport,
    ctx: ProfileContext,
    config: Path,
    auto_accept_tracked: bool,
    auto_accept_live: bool,
    section_auto: ReconcileAuto | None,
    yes: bool,
) -> None:
    """Run the three pre-deploy confirm/reject gates in their fixed order.

    Bundles the unexpected-drift confirm (``--auto-accept-{tracked,live}``)
    + the legacy unexpected-drift wizard hand-off + the section-reconcile
    confirm (``--auto=use-tracked``) into one orchestrator so
    :func:`install` reads as a high-level pipeline rather than 50+ LoC
    of three nearly-identical confirm shells. Each gate is independent
    and short-circuits when its triggering flag is unset; the order
    matches the pre-extraction body verbatim so flag interactions stay
    unchanged.
    """
    _confirm_legacy_drift_or_exit(
        drift_report=drift_report,
        ctx=ctx,
        auto_accept_tracked=auto_accept_tracked,
        auto_accept_live=auto_accept_live,
        yes=yes,
    )
    _check_unexpected_drift(
        drift_report,
        ctx,
        config,
        auto_accept_tracked=auto_accept_tracked,
        auto_accept_live=auto_accept_live,
    )
    _confirm_section_reconcile_or_exit(
        ctx=ctx,
        section_auto=section_auto,
        yes=yes,
    )


# ---------------------------------------------------------------------------
# setforge-lnvq: dry-run pipeline.
#
# ``_dry_run_pipeline`` is the orchestrator-level branch entered when
# ``setforge install --dry-run`` is invoked. It reuses every read-only
# helper the real pipeline calls (``compare_mod.compare_profile``,
# ``_extract_live_sections_map``, ``_resolve_section_decisions``,
# ``claude_plugins.reconcile(dry_run=True)``,
# ``vscode_extensions.reconcile(dry_run=True)``) and emits ``WOULD ``-
# prefixed lines for every mutating verb the real pipeline would invoke.
#
# Anti-pattern guards (per spec SPEC 4):
#
# - Boundary-not-leaf: the ``if dry_run:`` branch lives in
#   :func:`setforge.cli.install.install` exactly once. ``dry_run`` is
#   NOT threaded into ``deploy`` / ``transitions`` / ``compare`` /
#   ``merge`` — the dry-run path bypasses those modules entirely
#   (``deploy.bootstrap_local`` / ``transitions.ensure_state_dir_writable`` /
#   ``transitions.write_transition`` / ``secrets_mod.append_to_allowlist`` /
#   ``section_reconcile.stamp_tracked_baseline`` are all unreachable).
# - No new ``_simulate_*`` / ``_dry_*`` diff-or-merge function: every
#   compute step here delegates to the same shared helpers the real
#   pipeline uses, so a future change to the diff algorithm reflects
#   in dry-run output automatically.
# - WOULD only on mutating verbs (``deploy`` / ``inject`` / ``install`` /
#   ``uninstall`` / ``enable`` / ``disable``); section headers and read
#   counts go unprefixed.
# - No ``confirm_auto_operation`` call from the dry-run path: the two
#   call sites in :func:`_confirm_legacy_drift_or_exit` /
#   :func:`_confirm_section_reconcile_or_exit` are inside
#   :func:`_run_predeploy_gates`, which the dry-run pipeline never
#   invokes — even under ``--auto=*`` + ``--dry-run``.
# ---------------------------------------------------------------------------

_DRY_RUN_HEADER: str = "=== DRY-RUN MODE — NOTHING WILL BE MUTATED ==="
"""First line of every dry-run invocation. Unambiguous opener for users + log
scanners."""

_DRY_RUN_FINAL_LINE: str = "=== rerun without --dry-run to apply for real ==="
"""Last line of every dry-run invocation. Exact-match string the acceptance
gate `tail -1 | rg -q '...'` checks against; do NOT reformat without
updating the spec + every consumer."""


def _dry_run_pipeline(
    *,
    ctx: ProfileContext,
    section_auto: ReconcileAuto | None,
) -> None:
    """Simulate every install phase without mutating filesystem or state.

    Called from :func:`setforge.cli.install.install` when ``--dry-run``
    is set. Walks the same 8 phases the real pipeline performs (profile
    resolve, host overlay, drift gate, file deploys, section reconcile,
    plugin reconcile, extension reconcile, transition record) and prints
    a ``WOULD ``-prefixed action line per mutating verb. Calls only
    read-only helpers; never writes files, never touches the transition
    state dir, never invokes the bviv confirm wizard, never runs git
    fetch (the source-layer git check runs BEFORE this function but is
    itself read-only).
    """
    typer.echo(_DRY_RUN_HEADER)
    _dry_run_emit_profile_summary(ctx)
    drift_report = compare_mod.compare_profile(ctx.cfg, ctx.profile, ctx.repo_root)
    # Pre-extract live user-sections via the SAME helper the real
    # pipeline calls (anti-pattern check #3 — no parallel compute).
    # In dry-run the map is informational (a count surface); the real
    # pipeline forwards it to ``deploy.copy_atomic`` for the
    # ``precomputed_live_sections`` fast path. Calling it here keeps
    # the dry-run output's section-aware tracked_file count consistent
    # with what the real pipeline observes on this profile.
    live_sections_map = _extract_live_sections_map(ctx)
    _dry_run_emit_drift_gate(drift_report, live_sections_map=live_sections_map)
    _dry_run_emit_deploys(ctx, drift_report)
    _dry_run_emit_section_reconcile(ctx, section_auto=section_auto)
    _dry_run_emit_plugin_reconcile(ctx)
    _dry_run_emit_extension_reconcile(ctx)
    _dry_run_emit_transition_path(ctx)
    typer.echo(_DRY_RUN_FINAL_LINE)


def _dry_run_emit_profile_summary(ctx: ProfileContext) -> None:
    """Emit the ``=== resolving profile + host overlay ===`` block.

    Two phases of the spec's 8-phase walk: ``profile resolve`` and
    ``host overlay``. ``host overlay`` is a placeholder block today
    (the current build has no ``~/.config/setforge/local.yaml`` host
    overlay surface) so it reports zero overlays — the line shape stays
    stable for the day the overlay layer lands. Counts are READ
    operations and stay unprefixed; the section headers are unprefixed
    per the WOULD-rule.
    """
    typer.echo("=== resolving profile + host overlay ===")
    typer.echo(f"profile {ctx.profile}")
    typer.echo(f"  tracked_files:  {len(ctx.resolved.tracked_files)}")
    typer.echo(
        "  extensions:     "
        f"{len(ctx.resolved.extensions.include)} declared "
        f"({len(ctx.resolved.extensions.exclude)} excluded)"
    )
    typer.echo(f"  claude_plugins: {len(ctx.resolved.claude_plugins)}")
    typer.echo(f"  bootstrap:      {len(ctx.resolved.bootstrap)}")
    typer.echo("  host overlay:   none (host-local layer not yet enabled)")


def _dry_run_emit_drift_gate(
    drift_report: compare_mod.CompareReport,
    *,
    live_sections_map: Mapping[Path, LiveSections],
) -> None:
    """Emit the ``=== would-be drift gate ===`` block.

    The drift gate is a READ in the real pipeline too (it computes
    unexpected drift over the existing live tree) — counts stay
    unprefixed. When unexpected drift IS present, surface the count so
    users can see what the real install would gate on, but do NOT
    invoke the bviv confirm wizard (the dry-run path is the preview;
    short-circuiting before the confirm is a hard requirement per spec).
    ``live_sections_map`` is the read-only output of
    :func:`_extract_live_sections_map`; the count is informational.
    """
    typer.echo("=== would-be drift gate ===")
    unexpected = sum(
        1
        for e in drift_report.entries
        if e.status == CompareStatus.DRIFTED and e.unexpected_drift_keys
    )
    typer.echo(f"unexpected drift in {unexpected} file(s)")
    typer.echo(
        f"section-aware tracked_files with live present: {len(live_sections_map)}"
    )


def _dry_run_emit_deploys(
    ctx: ProfileContext, drift_report: compare_mod.CompareReport
) -> None:
    """Emit the ``=== would-be deploy ===`` block.

    One WOULD line per tracked_file entry, keyed off the same
    :class:`CompareStatus` the real pipeline uses (MISSING / DRIFTED /
    UNCHANGED). The shared compare report is the single source of
    truth — there is no parallel ``_dry_run_compute_deploys`` function
    re-implementing the diff (anti-pattern check #3).

    The compare report's entries iterate in the same order
    :func:`_iter_all_tracked_files` does (both walk
    ``ctx.resolved.tracked_files`` then ``expand_tracked_file``), so a
    pair-wise zip joins them deterministically — no name-suffix
    heuristic needed.
    """
    typer.echo("=== would-be deploy ===")
    walk = list(_iter_all_tracked_files(ctx))
    if len(walk) != len(drift_report.entries):
        # Defensive: a future expand_tracked_file divergence between
        # the two callers would silently mis-pair entries. Surface the
        # mismatch loudly rather than print a half-correct report.
        raise SetforgeError(
            f"dry-run: tracked-file walk length ({len(walk)}) does not match "
            f"compare report length ({len(drift_report.entries)}); refusing "
            f"to render a deploy preview against an inconsistent join"
        )
    for (_tracked, _sub_src, sub_dst), entry in zip(
        walk, drift_report.entries, strict=True
    ):
        match entry.status:
            case CompareStatus.MISSING:
                typer.echo(f"  WOULD install   {sub_dst}")
            case CompareStatus.DRIFTED:
                typer.echo(f"  WOULD update    {sub_dst}")
            case CompareStatus.UNCHANGED:
                typer.echo(f"  WOULD noop      {sub_dst}")
            case _ as never:
                assert_never(never)
    for raw in ctx.resolved.bootstrap:
        path = Path(str(raw)).expanduser()
        if not path.exists():
            typer.echo(f"  WOULD bootstrap {path}")


def _dry_run_emit_section_reconcile(
    ctx: ProfileContext, *, section_auto: ReconcileAuto | None
) -> None:
    """Emit the ``=== would-be section reconcile ===`` block.

    Reuses the read-only :func:`_resolve_section_decisions` helper from
    the shared CLI surface so the dry-run output draws on the SAME
    classifier the real pipeline uses (anti-pattern check #3). When
    ``section_auto`` is :data:`ReconcileAuto.USE_TRACKED`, surface every
    shared-drifted section that WOULD be overwritten by the tracked
    body; under ``KEEP_LIVE`` and ``None``, no shared section would
    change (the bare-install default keeps live silently).
    """
    typer.echo("=== would-be section reconcile ===")
    # ``interactive=False`` keeps the section wizard quiet under
    # dry-run; the helper still emits the bare-install warning per
    # section-drifted file when ``section_auto`` is None, which is
    # informational stderr output, not a mutation.
    decisions = _resolve_section_decisions(
        ctx, section_auto=section_auto, interactive=False
    )
    if not decisions:
        typer.echo("  no shared-section drift to reconcile")
        return
    for dst_path, body_map in decisions.items():
        for section_name in body_map:
            typer.echo(
                f"  WOULD inject  '{section_name}' into {dst_path} (tracked body)"
            )


def _dry_run_emit_plugin_reconcile(ctx: ProfileContext) -> None:
    """Emit the ``=== would-be plugin reconcile ===`` block.

    Reuses :func:`setforge.claude_plugins.reconcile` with
    ``dry_run=True`` so the dry-run report mirrors what the real
    reconciler would compute. When ``claude`` is not on PATH the
    reconcile raises :class:`PluginToolMissing`; surface that as a
    skip-warn line (no failure exit — dry-run is informational).

    Short-circuits the subprocess work entirely when neither the
    profile NOR the top-level config declares anything plugin-related
    (no ``claude_plugins`` entries, no ``marketplaces``). The
    underlying ``claude_plugins.reconcile`` calls ``list_installed``
    + ``list_marketplaces`` unconditionally — each subprocess can
    block up to 30s on a misconfigured ``claude``; the short-circuit
    keeps dry-run snappy on profiles that don't touch the plugin
    layer at all.
    """
    typer.echo("=== would-be plugin reconcile ===")
    if not ctx.resolved.claude_plugins and not ctx.cfg.marketplaces:
        typer.echo("  nothing declared")
        return
    try:
        report = claude_plugins_mod.reconcile(ctx.cfg, ctx.resolved, dry_run=True)
    except PluginToolMissing as exc:
        typer.echo(f"  skipped (plugin tool unavailable: {exc})")
        return
    for mp_name in report.marketplaces_added:
        typer.echo(f"  WOULD add-marketplace {mp_name}")
    for plugin, marketplace in report.to_install:
        typer.echo(f"  WOULD install  {plugin}@{marketplace}")
    for pid in report.to_enable:
        typer.echo(f"  WOULD enable   {pid}")
    for pid in report.to_disable:
        typer.echo(f"  WOULD disable  {pid}")
    if not (
        report.marketplaces_added
        or report.to_install
        or report.to_enable
        or report.to_disable
    ):
        typer.echo("  nothing to reconcile")


def _dry_run_emit_extension_reconcile(ctx: ProfileContext) -> None:
    """Emit the ``=== would-be extension reconcile ===`` block.

    Reuses :func:`setforge.vscode_extensions.reconcile` with
    ``dry_run=True``. When the ``code`` binary is missing the
    reconciler raises :class:`ExtensionToolMissing`; surface that as a
    skip-warn line (parallel to :func:`_dry_run_emit_plugin_reconcile`).

    Short-circuits the ``code --list-extensions`` subprocess when the
    profile declares no extensions (parallel to the plugin
    short-circuit — same rationale: keep dry-run snappy on profiles
    that don't touch the extension layer at all).
    """
    typer.echo("=== would-be extension reconcile ===")
    ext = ctx.resolved.extensions
    if not (ext.include or ext.exclude):
        typer.echo("  nothing declared")
        return
    try:
        report = vscode_extensions_mod.reconcile(ext, dry_run=True)
    except ExtensionToolMissing as exc:
        typer.echo(f"  skipped (extension tool unavailable: {exc})")
        return
    for ext_id in report.to_install:
        typer.echo(f"  WOULD install   {ext_id}")
    for ext_id in report.to_uninstall:
        typer.echo(f"  WOULD uninstall {ext_id}")
    if not (report.to_install or report.to_uninstall):
        typer.echo("  nothing to reconcile")


def _dry_run_emit_transition_path(ctx: ProfileContext) -> None:
    """Emit the ``=== would-be transition record ===`` block.

    Computes the would-be transition directory PATH (one line, prefixed
    ``WOULD record``) without ever calling
    :func:`transitions.ensure_state_dir_writable`,
    :func:`transitions.write_meta`, or
    :func:`transitions.write_transition`. The state dir is NOT created
    on disk; the path is computed via
    :func:`transitions.transition_dirname` against ``now_utc()``.
    """
    typer.echo("=== would-be transition record ===")
    dirname = transitions.transition_dirname(
        transitions.now_utc(),
        transitions.TransitionCommand.INSTALL.value,
        ctx.profile,
    )
    target = transitions.transitions_root() / dirname
    typer.echo(f"  WOULD record  {target}")
