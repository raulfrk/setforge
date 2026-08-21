"""capture / sync subcommands — live → tracked capture flow.

- ``capture`` and ``sync`` drive the ``capture_mod.capture_profile``
  pipeline, with ``--auto={use-live,keep-tracked}`` as the
  non-interactive escape. ``capture`` is the pipeline alone; ``sync``
  also records a transition so ``revert`` can replay it.
"""

import stat
import sys
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from uuid import UUID

import typer
from click import ClickException

from setforge import (
    atomicio,
    operations,
    transitions,
    vscode_extensions,
)
from setforge import (
    capture as capture_mod,
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
)
from setforge.config import (
    Config,
    ResolvedProfile,
    load_config,
    refuse_unmigrated_host_local_leak,
    resolve_effective_profile,
)
from setforge.errors import ExtensionToolMissing
from setforge.file_ownership import FileAction, FileDecision, decide_file, observe_file
from setforge.locking import mutation_locks
from setforge.overlay_provenance import ResolvedExtension
from setforge.ownership import OwnershipError, OwnershipStore, read_owner_id
from setforge.reconcile import store as reconcile_store
from setforge.reconcile.types import content_sha, file_id


@dataclass(frozen=True, slots=True)
class _CaptureSnapshot:
    """Whole-profile inputs and exact outputs frozen for one confirmation."""

    config_hash: str
    effective_hash: str
    ownership: tuple[FileDecision, ...]
    ownership_authorized: tuple[tuple[str, bool], ...]
    preview: tuple[capture_mod.CapturePreview, ...]


def _read_capture_owner_id(repo_root: Path) -> UUID | None:
    try:
        return read_owner_id(repo_root)
    except OwnershipError:
        return None


def _capture_ownership(
    ctx: ProfileContext, owner_id: UUID | None
) -> tuple[tuple[FileDecision, ...], dict[str, bool]]:
    """Read exact claims under the resources lock without reacquiring owner locks."""
    if owner_id is None and not (ctx.repo_root / ".git").exists():
        return (), {
            name: True for _tf, name, _src, _dst in _iter_all_tracked_files(ctx)
        }
    planning_owner = owner_id or UUID(int=0)
    store = OwnershipStore()
    decisions = tuple(
        decide_file(
            observation,
            store.read(observation.resource_id),
            owner_id=planning_owner,
        )
        for tracked, _name, _src, destination in _iter_all_tracked_files(ctx)
        if tracked.symlink is None
        for observation in (observe_file(destination),)
    )
    by_locator = {decision.observation.locator: decision for decision in decisions}
    authorized = {
        name: (
            True
            if tracked.symlink is not None
            else by_locator[str(destination.absolute())].action
            not in {FileAction.ADOPT, FileAction.HOLD}
        )
        for tracked, name, _src, destination in _iter_all_tracked_files(ctx)
    }
    return decisions, authorized


def _build_capture_plan(
    *,
    preview: tuple[capture_mod.CapturePreview, ...],
    ctx: ProfileContext,
) -> AutoPlan:
    """Build a truthful live → tracked plan from exact capture projections."""
    file_changes = [
        FileChange(source=item.dst, dest=item.src, changed=1)
        for item in preview
        if item.action is capture_mod.CaptureAction.UPDATED
    ]
    blockers = [warning for item in preview for warning in item.warnings]
    blockers += [
        f"{item.name}: {item.reason}"
        for item in preview
        if item.action is capture_mod.CaptureAction.SKIPPED and item.reason
    ]
    store_updates = sum(item.store_update for item in preview)
    risks: list[str] = []
    if file_changes:
        risk = (
            f"{len(file_changes)} tracked-side file(s) will be updated with the "
            "promotable capture projection"
        )
        if blockers:
            risk += "; blocked units listed below remain host-only and are not absorbed"
        risks.append(risk)
    if store_updates:
        risks.append(
            f"reconciliation state for {store_updates} file(s) will be refreshed"
        )
    if not risks:
        return AutoPlan(
            direction=AutoDirection.LIVE_TO_TRACKED,
            file_changes=(),
            risks=(),
            revert_command=f"setforge revert --profile={ctx.profile}",
            blockers=tuple(blockers),
        )
    return AutoPlan(
        direction=AutoDirection.LIVE_TO_TRACKED,
        file_changes=tuple(file_changes),
        risks=tuple(risks),
        revert_command=f"setforge revert --profile={ctx.profile}",
        blockers=tuple(blockers),
    )


def _load_capture_preview(
    config: Path,
    profile: str,
    repo_root: Path,
    *,
    verb: str,
    owner_id: UUID | None,
) -> tuple[ProfileContext, _CaptureSnapshot, tuple[ResolvedExtension, ...]]:
    """Reload effective configuration and build an exact read-only capture plan."""
    cfg = load_config(config)
    refuse_unmigrated_host_local_leak(cfg, verb=verb, profile=profile)
    effective = resolve_effective_profile(cfg, profile, repo_root)
    resolved = effective.resolved
    ctx = ProfileContext(
        cfg=cfg, resolved=resolved, repo_root=repo_root, profile=profile
    )
    if verb == "sync":
        _refuse_duplicate_section_names(ctx, command="sync")
    ownership, authorized = _capture_ownership(ctx, owner_id)
    preview = capture_mod.preview_capture_profile(
        cfg,
        profile,
        repo_root,
        resolved=resolved,
        ownership_authorized=authorized,
    )
    return (
        ctx,
        _CaptureSnapshot(
            config_hash=content_sha(config.read_bytes()),
            effective_hash=content_sha(repr(effective).encode("utf-8")),
            ownership=ownership,
            ownership_authorized=tuple(sorted(authorized.items())),
            preview=preview,
        ),
        tuple(effective.local_overlay.extensions),
    )


def _confirm_capture_plan(
    *,
    command: str,
    ctx: ProfileContext,
    snapshot: _CaptureSnapshot,
    auto_enum: capture_mod.CaptureAuto | None,
    yes: bool,
) -> None:
    """Apply the shared bare/use-live confirmation contract outside locks."""
    plan = _build_capture_plan(preview=snapshot.preview, ctx=ctx)
    if not plan.file_changes and not plan.risks:
        return
    if not yes and not sys.stdin.isatty():
        if auto_enum is None:
            raise ClickException(
                f"setforge {command} found actionable live drift; use "
                "--auto=use-live --yes to capture it or "
                "--auto=keep-tracked to refuse it"
            )
        raise ClickException(
            f"setforge {command} --auto=use-live requires --yes when stdin is not a TTY"
        )
    if not confirm_auto_operation(
        command=command,
        profile=ctx.profile,
        plan=plan,
        yes=yes,
    ):
        raise typer.Exit(0)


def _render_keep_tracked(
    preview: tuple[capture_mod.CapturePreview, ...],
) -> None:
    """Render the non-mutating keep-tracked refusal without taking locks."""
    results = [
        capture_mod.CaptureResult(
            name=item.name,
            action=(
                capture_mod.CaptureAction.SKIPPED
                if item.action is capture_mod.CaptureAction.UPDATED or item.store_update
                else item.action
            ),
            reason="keep-tracked"
            if item.action is capture_mod.CaptureAction.UPDATED or item.store_update
            else item.reason,
            warnings=item.warnings,
        )
        for item in preview
    ]
    _render_capture_results(results)


def _require_same_preview(
    before: _CaptureSnapshot,
    locked: _CaptureSnapshot,
) -> None:
    if locked != before:
        raise ClickException(
            "capture plan changed after confirmation; no files were written — "
            "run the command again"
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
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Confirm --auto=use-live without an interactive prompt.",
    ),
) -> None:
    """Capture live → tracked for every tracked_file in the profile.

    When a tracked_file carries drift, capture resolves it: pass
    ``--auto={use-live, keep-tracked}`` for non-interactive contexts, or
    confirm interactively otherwise.
    """
    config = _resolve_config_arg(config)
    auto_enum = _parse_capture_auto(auto)
    if yes and auto_enum is None:
        raise typer.BadParameter("--yes requires --auto")

    repo_root = config.resolve().parent
    owner_id = _read_capture_owner_id(repo_root)
    with mutation_locks(resources=True, config_dir=repo_root, profile=profile):
        operations.refuse_active(profile)
        initial_ctx, initial_snapshot, _initial_extensions = _load_capture_preview(
            config, profile, repo_root, verb="capture", owner_id=owner_id
        )
    if auto_enum is capture_mod.CaptureAuto.KEEP_TRACKED:
        _render_keep_tracked(initial_snapshot.preview)
        return
    _confirm_capture_plan(
        command="capture",
        ctx=initial_ctx,
        snapshot=initial_snapshot,
        auto_enum=auto_enum,
        yes=yes,
    )
    with mutation_locks(resources=True, config_dir=repo_root, profile=profile):
        operations.refuse_active(profile)
        locked_ctx, locked_snapshot, _locked_extensions = _load_capture_preview(
            config, profile, repo_root, verb="capture", owner_id=owner_id
        )
        _require_same_preview(initial_snapshot, locked_snapshot)
        try:
            results = _run_capture(
                locked_ctx.cfg,
                profile,
                repo_root,
                config,
                auto_enum,
                resolved=locked_ctx.resolved,
                ownership_authorized=dict(locked_snapshot.ownership_authorized),
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
            "absorbs all drift; 'keep-tracked' rejects all drift."
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
    if yes and auto_enum is None:
        raise typer.BadParameter("--yes requires --auto")

    repo_root = config.resolve().parent
    owner_id = _read_capture_owner_id(repo_root)
    with mutation_locks(resources=True, config_dir=repo_root, profile=profile):
        operations.refuse_active(profile)
        initial_ctx, initial_snapshot, _initial_extensions = _load_capture_preview(
            config, profile, repo_root, verb="sync", owner_id=owner_id
        )
    if auto_enum is capture_mod.CaptureAuto.KEEP_TRACKED:
        _render_keep_tracked(initial_snapshot.preview)
        return
    _confirm_capture_plan(
        command="sync",
        ctx=initial_ctx,
        snapshot=initial_snapshot,
        auto_enum=auto_enum,
        yes=yes,
    )
    with (
        mutation_locks(resources=True, config_dir=repo_root, profile=profile),
        operations.recover_on_error(profile, "sync"),
    ):
        operations.refuse_active(profile)
        ctx, locked_snapshot, locked_extensions = _load_capture_preview(
            config, profile, repo_root, verb="sync", owner_id=owner_id
        )
        _require_same_preview(initial_snapshot, locked_snapshot)
        cfg = ctx.cfg
        resolved = ctx.resolved
        if not no_transition:
            transitions.ensure_state_dir_writable()

        src_paths = _sync_snapshot_paths(ctx, config)
        file_pre = transitions.snapshot_paths(src_paths)
        # Snapshot the per-host store state (byte bases / spans sidecars /
        # scalar bases) BEFORE _run_capture re-baselines them, so revert
        # restores the stores in lockstep with the tracked patch. Without
        # this, a sync that re-baselines a SHARED base followed by revert
        # would leave the base AHEAD of the reverted tracked src — the
        # corruption direction the codebase guards against.
        state_pre = _capture_sync_store_snapshots(ctx)
        journal = operations.prepare(
            command="sync",
            profile=profile,
            config_dir=repo_root,
            resources_lock=False,
            command_line=tuple(redact_argv(sys.argv[1:])),
            paths=tuple(src_paths),
            state_snapshots=state_pre,
        )
        journal = operations.begin_checkpoint(
            journal,
            name="capture-files-and-stores",
            kind=operations.CheckpointKind.REVERSIBLE,
            recovery="restore captured tracked/config paths and reconcile stores",
        )

        try:
            results = _run_capture(
                cfg,
                profile,
                repo_root,
                config,
                auto_enum,
                resolved=resolved,
                ownership_authorized=dict(locked_snapshot.ownership_authorized),
            )
            _render_capture_results(results)

            _capture_extensions(
                config,
                profile,
                overlay_extensions=list(locked_extensions),
            )
        except (KeyboardInterrupt, OSError) as exc:
            # capture_profile writes tracked srcs and re-baselines stores
            # one at a time with no internal rollback, so a Ctrl-C OR a
            # mid-capture OSError (e.g. ENOSPC) can leave a partial tracked
            # write and a base advanced ahead of its src. Restore the
            # pre-capture file + store snapshots on both, guarding the
            # restore so a restore-time failure never masks the original.
            recovered = False
            try:
                operations.recover_files(journal)
                operations.complete(journal)
                recovered = True
            except BaseException as recovery_error:
                exc.add_note(f"automatic recovery failed: {recovery_error}")
            if isinstance(exc, KeyboardInterrupt):
                typer.secho(
                    "sync cancelled (Ctrl-C); "
                    + (
                        "files restored from snapshot journal"
                        if recovered
                        else "recovery incomplete; run `setforge recover --profile="
                        f"{profile} --apply`"
                    ),
                    err=True,
                    fg=typer.colors.YELLOW,
                )
                raise typer.Exit(130) from None
            # OSError: snapshots restored; propagate so the user sees it.
            raise

        journal = operations.finish_checkpoint(journal)
        file_post = transitions.snapshot_paths(src_paths)
        if not no_transition:
            _write_sync_transition(
                ctx,
                file_pre=file_pre,
                file_post=file_post,
                state_snapshots=state_pre,
            )
        operations.complete(journal)


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
    saw_reconcile = False
    for tracked_file, sub_name, _sub_src, _sub_dst in _iter_all_tracked_files(ctx):
        if tracked_file.symlink is not None:
            continue
        # A reconcile file (with a recorded base): sync's staged capture
        # re-baselines local + index (+ preserves drafts), so revert must
        # restore the whole reconcile store, not just live. Snapshot BASE + the
        # local/drafts legs; INDEX once per profile, below.
        if reconcile_store.read_base(ctx.profile, file_id(sub_name)) is not None:
            saw_reconcile = True
            entries.append(
                transitions.snapshot_store_state(
                    transitions.SnapshotStore.BASE, ctx.profile, sub_name
                )
            )
            entries.extend(transitions.reconcile_file_snapshots(ctx.profile, sub_name))
    if saw_reconcile:
        entries.append(
            transitions.snapshot_store_state(
                transitions.SnapshotStore.INDEX, ctx.profile, ctx.profile
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

    Includes :data:`LOCAL_CONFIG_PATH` so any transition-spanning mutation
    of local.yaml is revertable. Capture itself no longer writes local.yaml
    (host-local content is a LOCAL unit in the reconcile store); the include
    is retained so that a local.yaml write landing inside the SYNC
    transition lets ``revert`` restore the pre-mutation state rather than
    silently losing it.

    The PROMOTE wizard mutates local.yaml, but it fires BEFORE this function
    and records its own ``TransitionCommand.PROMOTE`` snapshot (taken
    pre-mutation), so the two transitions reverse disjoint diffs and do not
    double-record the same change.
    """
    paths = [sub_src for _, _, sub_src, _ in _iter_all_tracked_files(ctx)]
    paths.append(config.resolve())
    # Resolve LOCAL_CONFIG_PATH off the module so it tracks any runtime
    # override (tests monkeypatch ``setforge.source.LOCAL_CONFIG_PATH``) —
    # a module-bound import would diverge from the runtime value.
    paths.append(source_mod.LOCAL_CONFIG_PATH.resolve())
    return paths


def _capture_extensions(
    config: Path,
    profile: str,
    *,
    overlay_extensions: list[ResolvedExtension],
) -> None:
    """Capture vscode-extension include changes; surface tool-missing as a warning."""
    try:
        changed = vscode_extensions.capture_extensions(
            config, profile, overlay_extensions=overlay_extensions
        )
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
        reason = f" ({result.reason})" if result.reason else ""
        typer.echo(f"{result.action.value:>8}  {result.name}{reason}")
        for warning in result.warnings:
            typer.secho(f"warning: {warning}", err=True, fg=typer.colors.YELLOW)


def _run_capture(
    cfg: Config,
    profile: str,
    repo_root: Path,
    config: Path,
    auto_enum: capture_mod.CaptureAuto | None,
    *,
    resolved: ResolvedProfile,
    ownership_authorized: dict[str, bool],
) -> list[capture_mod.CaptureResult]:
    """Run ``capture_profile``.

    ``KeyboardInterrupt`` is NOT swallowed here: ``capture_profile``
    performs no internal snapshot/restore, so the caller owns the Ctrl-C
    contract — ``sync`` restores from the pre-capture snapshot it took and
    ``capture`` reports the partial-write truth.

    The already-resolved path/package overlays are passed in. No legacy
    host-local *section-body* overlay is loaded: host-local content is now a
    LOCAL unit in the reconcile store, and ``capture_profile``'s per-hunk staged path
    (:func:`setforge.capture._capture_staged_plain`) already promotes ONLY
    the SHARED hunks into tracked and keeps LOCAL host-only content out, so
    the legacy local.yaml ``host_local_sections`` strip is redundant.
    """
    return capture_mod.capture_profile(
        cfg,
        profile,
        repo_root,
        setforge_yaml_path=config.resolve(),
        auto=auto_enum,
        resolved=resolved,
        ownership_authorized=ownership_authorized,
    )


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
