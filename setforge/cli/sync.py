"""capture / sync subcommands — live → tracked capture flow.

- ``capture`` and ``sync`` drive the ``capture_mod.capture_profile``
  pipeline, with ``--auto={use-live,keep-tracked}`` as the
  non-interactive escape. ``capture`` is the pipeline alone; ``sync``
  also records a transition so ``revert`` can replay it.
"""

import stat
import sys
from datetime import UTC
from pathlib import Path

import typer

from setforge import (
    atomicio,
    section_wizard,
    transitions,
    vscode_extensions,
)
from setforge import (
    capture as capture_mod,
)
from setforge import (
    compare as compare_mod,
)
from setforge import (
    source as source_mod,
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
    SYNC_EXAMPLES,
)
from setforge.cli._helpers import (
    ProfileContext,
    _iter_all_tracked_files,
    _parse_capture_auto,
    _refuse_duplicate_section_names,
    _refuse_legacy_live_markers,
    _resolve_drift_paths,
)
from setforge.cli._install_helpers import (
    _load_validated_host_local_sections,
)
from setforge.config import (
    Config,
    apply_host_local_tracked_file_overrides,
    load_config,
    resolve_profile,
)
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

    Joins the ``CompareReport.entries`` (entries marked DRIFTED with a
    diff or mode drift) against the tracked_file path map. Direction is
    always LIVE_TO_TRACKED — sync absorbs live edits.
    """
    file_changes: list[FileChange] = []
    # sync absorbs ANY drift on use-live (not just unexpected) — the
    # capture writes everything that differs into tracked. The shared
    # ``_resolve_drift_paths`` helper already filters to entries with
    # ``diff or mode_drift``, which matches sync's intent.
    for _entry, sub_src, sub_dst in _resolve_drift_paths(drift_report, ctx):
        file_changes.append(
            FileChange(
                source=sub_dst,
                dest=sub_src,
                changed=1,
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

    When a tracked_file carries drift, capture resolves it: pass
    ``--auto={use-live, keep-tracked}`` for non-interactive contexts, or
    confirm interactively otherwise.
    """
    config = _resolve_config_arg(config)
    auto_enum = _parse_capture_auto(auto)

    cfg = load_config(config)
    # Fold the local.yaml host-local overlay (mode/dst/spans/...) into the
    # tracked_files so capture sees the host-local OVERLAY spans on
    # ``tracked_file.spans`` and can excise their bodies before any tracked
    # write — without this fold the overlay body leaks into the shared repo.
    apply_host_local_tracked_file_overrides(cfg)
    repo_root = config.resolve().parent
    try:
        results = _run_capture(
            cfg, profile, repo_root, config, auto_enum, command="capture"
        )
    except KeyboardInterrupt:
        # Plain ``capture`` takes no snapshot (only ``sync`` records a
        # transition + restorable snapshots), and ``capture_profile`` has
        # no internal rollback — so writes already committed survive.
        # Report that truthfully instead of a false "restored" claim.
        typer.secho(
            "capture cancelled (Ctrl-C); some files may have been partially "
            "written — run `setforge compare` to inspect",
            err=True,
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(130) from None
    _render_capture_results(results)


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

    Symmetric with ``setforge install``'s drift gate: drift is resolved
    interactively, or pass ``--auto=use-live`` (absorb every drift item)
    or ``--auto=keep-tracked`` (refuse) for scripted runs.
    """
    config = _resolve_config_arg(config)
    auto_enum = _parse_capture_auto(auto)

    cfg = load_config(config)
    # Fold the local.yaml host-local overlay (mode/dst/spans/...) so capture
    # sees the host-local OVERLAY spans and excises their bodies before any
    # tracked write (leak gate — see the capture command above).
    apply_host_local_tracked_file_overrides(cfg)
    repo_root = config.resolve().parent
    resolved = resolve_profile(cfg, profile)
    ctx = ProfileContext(
        cfg=cfg, resolved=resolved, repo_root=repo_root, profile=profile
    )
    _refuse_legacy_live_markers(ctx, command="sync")
    _refuse_duplicate_section_names(ctx, command="sync")

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
        # Snapshot the per-host store state (byte bases / spans sidecars /
        # scalar bases) BEFORE _run_capture re-baselines them, so revert
        # restores the stores in lockstep with the tracked patch. Without
        # this, a sync that re-baselines a SHARED base followed by revert
        # would leave the base AHEAD of the reverted tracked src — the
        # corruption direction the codebase guards against.
        state_pre = _capture_sync_store_snapshots(ctx)

        try:
            results = _run_capture(
                cfg, profile, repo_root, config, auto_enum, command="sync"
            )
            _render_capture_results(results)

            _capture_extensions(config, profile)
        except KeyboardInterrupt:
            # capture_profile writes tracked srcs and re-baselines stores
            # one at a time with no internal rollback. Restore the
            # pre-capture file + store snapshots so an interrupted sync
            # leaves no partial tracked writes and no base advanced ahead
            # of its tracked src — then report the truth (files restored).
            _restore_sync_snapshots(file_pre, state_pre)
            typer.secho(
                "sync cancelled (Ctrl-C); files restored from snapshot",
                err=True,
                fg=typer.colors.YELLOW,
            )
            raise typer.Exit(130) from None

        file_post = transitions.snapshot_paths(src_paths)
        if not no_transition:
            _write_sync_transition(
                ctx,
                file_pre=file_pre,
                file_post=file_post,
                state_snapshots=state_pre,
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


def _capture_sync_store_snapshots(
    ctx: ProfileContext,
) -> tuple[transitions.StateSnapshotEntry, ...]:
    """Snapshot the pre-sync state of every store entry capture can re-baseline.

    Sync's analogue of install's ``_capture_store_snapshots`` barrier:
    capture re-baselines a disposition file's byte base
    (``base_store.write_base``) and advances span sidecars, but unlike
    install it had recorded NO ``state_snapshots``, so ``revert`` left the
    base AHEAD of the reverted tracked src (the corruption direction).

    For each non-symlink tracked-file in the profile, a disposition
    declaration snapshots the byte base AND the scalar-base manifest, and
    a span declaration snapshots the spans sidecar manifest — keyed by the
    ``expand_tracked_file`` synthetic ``sub_name`` the stores key by.
    Symlink records are skipped (their capture never touches the stores).
    Must run BEFORE :func:`_run_capture`, before any re-baseline write.
    """
    entries: list[transitions.StateSnapshotEntry] = []
    for tracked_file, sub_name, _sub_src, _sub_dst in _iter_all_tracked_files(ctx):
        if tracked_file.symlink is not None:
            continue
        if tracked_file.disposition is not None:
            entries.append(
                transitions.snapshot_store_state(
                    transitions.SnapshotStore.BASE, ctx.profile, sub_name
                )
            )
            entries.append(
                transitions.snapshot_store_state(
                    transitions.SnapshotStore.SCALAR_BASE, ctx.profile, sub_name
                )
            )
        if tracked_file.spans:
            entries.append(
                transitions.snapshot_store_state(
                    transitions.SnapshotStore.SPANS, ctx.profile, sub_name
                )
            )
    return tuple(entries)


def _write_sync_transition(
    ctx: ProfileContext,
    *,
    file_pre: dict[Path, str | None],
    file_post: dict[Path, str | None],
    state_snapshots: tuple[transitions.StateSnapshotEntry, ...] = (),
) -> None:
    """Write the SYNC transition record + echo the user-visible breadcrumb.

    Encapsulates the :func:`transitions.write_transition` call (with
    the redacted argv, end timestamp, and preserve_user_keys_applied
    metadata) and the trailing ``transition: ...`` /
    ``↩  revert with: ...`` echoes so the caller body stays a flat
    capture-and-write skeleton.

    ``state_snapshots`` carries the pre-sync per-host store state captured
    by :func:`_capture_sync_store_snapshots` so ``revert`` restores the
    byte bases / spans sidecars in lockstep with the tracked patch.

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
        ),
        file_pre,
        file_post,
        None,  # sync's extension change is reflected in the YAML diff
        state_snapshots=state_snapshots,
    )
    typer.echo(f"transition: {target}")
    typer.echo(f"↩  revert with: setforge revert --profile={ctx.profile}")


def _sync_snapshot_paths(
    ctx: ProfileContext,
    config: Path,
) -> list[Path]:
    """Tracked srcs under the profile + ``setforge.yaml`` + local.yaml.

    Includes :data:`LOCAL_CONFIG_PATH` so any capture-time mutation of
    local.yaml rides the SYNC transition's patch. ``capture`` writes
    local.yaml when a host-local OVERLAY body has been hand-edited and
    the user (or ``--auto=use-live`` -> KEEP) keeps the edit:
    ``_capture_overlay_bodies`` calls
    ``overlay_body_wizard.write_edited_body_to_local`` inside
    ``_run_capture``, AFTER ``file_pre`` is captured here. Snapshotting
    local.yaml in both ``file_pre`` and ``file_post`` lets ``revert``
    restore the pre-edit body instead of silently losing it.

    The PROMOTE wizard also mutates local.yaml, but it fires BEFORE this
    function and records its own ``TransitionCommand.PROMOTE`` snapshot
    (taken pre-mutation). Because ``file_pre`` here is captured AFTER
    promote has already applied, the SYNC snapshot only spans the
    capture-time delta layered on top — the two transitions reverse
    disjoint diffs and do not double-record the same change.
    """
    paths = [sub_src for _, _, sub_src, _ in _iter_all_tracked_files(ctx)]
    paths.append(config.resolve())
    # Resolve LOCAL_CONFIG_PATH off the module so it tracks any runtime
    # override (tests monkeypatch ``setforge.source.LOCAL_CONFIG_PATH``;
    # ``capture`` reads the same attribute lazily when it writes the
    # kept overlay body) — a module-bound import would diverge from the
    # path capture actually mutates.
    paths.append(source_mod.LOCAL_CONFIG_PATH.resolve())
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
    # The legacy preserve_user_sections section-reconcile model was retired at
    # schema 2.0, so no tracked_file is "section-bearing" any more — this list
    # is always empty (the section-promote wizard is inert).
    return []


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


def _render_capture_results(results: list[capture_mod.CaptureResult]) -> None:
    """Render the per-file action lines (stdout) + capture warnings (stderr).

    Shared between :func:`capture` and :func:`sync`. A warning marks content
    the writeback deliberately did NOT capture (e.g. a host value at a span
    path absent in tracked), so it goes to stderr where scripted callers
    keep it apart from the action listing.
    """
    for result in results:
        typer.echo(f"{result.action.value:>8}  {result.name}")
        for warning in result.warnings:
            typer.secho(f"warning: {warning}", err=True, fg=typer.colors.YELLOW)


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

    Maps ``CaptureRequiresInteractive`` → exit-1. ``KeyboardInterrupt``
    is NOT swallowed here: ``capture_profile`` performs no internal
    snapshot/restore, so the caller owns the Ctrl-C contract — ``sync``
    restores from the pre-capture snapshot it took and ``capture`` reports
    the partial-write truth. ``command`` is retained for call-site parity
    but no longer drives a (false) "restored from snapshot" message.

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


def _restore_sync_snapshots(
    file_pre: dict[Path, str | None],
    state_pre: tuple[transitions.StateSnapshotEntry, ...],
) -> None:
    """Restore the tracked srcs / configs and stored bases to pre-sync state.

    Invoked from :func:`sync`'s Ctrl-C handler so an interrupted capture
    leaves NO partially-written tracked srcs and NO base advanced ahead
    of its (now restored) tracked src — the corruption direction the
    transition machinery exists to prevent. Mirrors the file+state restore
    ``revert`` performs, but in-process from the snapshots ``sync`` took
    before :func:`_run_capture`.

    Per path in ``file_pre``: ``None`` (absent pre-sync) → unlinked;
    text → rewritten atomically, preserving the file's current permission
    bits (falling back to 0o644 when it was created during the aborted
    capture) so a restore never demotes an executable or 0o644 config to
    the 0600 mkstemp default. Store state is restored via
    :func:`transitions.restore_state_snapshots`.
    """
    for path, pre_text in file_pre.items():
        if pre_text is None:
            path.unlink(missing_ok=True)
            continue
        if path.exists() and path.read_text(encoding="utf-8") == pre_text:
            continue
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
        atomicio.atomic_write_text(path, pre_text, mode=mode)
    transitions.restore_state_snapshots(state_pre)
