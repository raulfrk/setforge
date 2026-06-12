"""Helpers for setforge.cli.install — module-private.

Helpers extracted from ``install()`` body:

- :func:`_check_unexpected_drift`: bare-install drift gate + :class:`typer.Exit`
  on no-resolve.
- :func:`_deploy_all_tracked_files`: two-pass tracked-file deploy —
  a read-only :func:`setforge.deploy.resolve_deploy` pass, the
  ``--strict-spans`` refusal gate, then the write pass
  (:func:`_execute_pending_deploys`).
- :func:`_write_install_transition`: snapshot +
  :func:`setforge.transitions.write_transition` wrapper that returns
  the written target path.
- :func:`_confirm_legacy_drift_or_exit` /
  :func:`_confirm_section_reconcile_or_exit`: auto-confirm confirm-or-exit
  wrappers that pair a plan-builder with
  :func:`setforge.cli._confirm.confirm_auto_operation`.
- :func:`_build_unexpected_drift_plan` /
  :func:`_build_shared_section_plan`: AutoPlan builders used by the
  confirm-or-exit helpers above.

NO ``@app.command`` decorators; NO ``app`` import — this module is
internal-only and stays out of typer's command surface.
"""

from __future__ import annotations

import contextlib
import os
import stat
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Final, assert_never

import typer

from setforge import (
    base_store,
    deploy,
    disposition_merge,
    section_reconcile,
    spans_store,
    transitions,
)
from setforge import (
    claude_plugins as claude_plugins_mod,
)
from setforge import (
    compare as compare_mod,
)
from setforge import config as config_mod
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
from setforge.cli._validate_errors import suggest_close_match
from setforge.compare import (
    CompareStatus,
    DriftClass,
    expand_tracked_file,
    resolve_dst,
    resolve_src,
)
from setforge.config import (
    Config,
    ResolvedProfile,
    SharedSpanCollision,
    TrackedFile,
)
from setforge.errors import (
    ExtensionToolMissing,
    PluginToolMissing,
    SetforgeError,
    SharedSpanReconcileRequiresInteractive,
)
from setforge.host_local_inject import HOST_LOCAL_PROVENANCE_TAG
from setforge.overlay_migration import migrate_local_yaml_overlay_spans
from setforge.section_reconcile import SectionDriftState
from setforge.section_wizard import ReconcileAuto
from setforge.sections import LiveSections, SectionSemantics, strip_shared_markers
from setforge.source import (
    HostLocalSection,
    HostLocalSectionName,
    load_local_host_local_sections,
    validate_host_local_sections_file_type,
)
from setforge.spans import SpanEntry, SpanKind, validate_spans_file_type
from setforge.spans_overlay import SpanOrphan


def _load_validated_host_local_sections(
    cfg: Config, resolved: ResolvedProfile, repo_root: Path
) -> dict[str, dict[HostLocalSectionName, HostLocalSection]]:
    """Load local.yaml host_local_sections + reject non-markdown tracked_files.

    Returns ``{tracked_file_id: {section_name: HostLocalSection}}`` for
    every tracked_file in the resolved profile that declares at least
    one host-local section. tracked_files NOT in the resolved profile
    are dropped silently (no error — the user may target a different
    profile on a different host). Non-markdown ``src`` with declared
    host-local sections raises :class:`ConfigError` via
    :func:`validate_host_local_sections_file_type` BEFORE any file is
    written (anti-smell item: install aborts cleanly).

    Shared between :mod:`setforge.cli.install` and
    :mod:`setforge.cli.compare` so both surfaces validate identically
    before threading the overlay through ``deploy.resolve_deploy`` /
    ``compare_profile`` respectively.
    """
    overlay = load_local_host_local_sections()
    result: dict[str, dict[HostLocalSectionName, HostLocalSection]] = {}
    profile_ids = set(resolved.tracked_files)
    for tf_id, sections_map in overlay.items():
        if tf_id not in profile_ids:
            continue
        tracked_file = cfg.tracked_files[tf_id]
        src = resolve_src(tracked_file, repo_root)
        validate_host_local_sections_file_type(tf_id, len(sections_map), src)
        result[tf_id] = sections_map
    return result


@dataclass(slots=True, frozen=True)
class OverlaySpanMigration:
    """Outcome of the transparent ``local.yaml`` overlay-span rewrite on install.

    ``path`` is the ``local.yaml`` that was (or would be) rewritten;
    ``pre_text`` is its content BEFORE the rewrite (``None`` when the file did
    not exist), captured so the install transition can record the genuine
    pre-migration ``file_pre`` for byte-exact ``revert``. ``migrated`` is
    ``True`` only when at least one ``host_local_sections`` block was actually
    moved — the one-time warning fires on that signal, and the steady-state /
    already-migrated read stays silent.
    """

    path: Path
    pre_text: str | None
    migrated: bool


def migrate_local_overlay_spans_on_install(
    profile: str, *, local_config_path: Path | None = None
) -> OverlaySpanMigration:
    """Transparently retire ``local.yaml host_local_sections`` → OVERLAY spans.

    Auto-on-install, idempotent rewrite mirroring the disposition-base
    seed-on-install pattern (:func:`_plan_disposition_base`): the
    first install after a host adopts the OVERLAY model rewrites its
    ``local.yaml`` in place (legacy ``host_local_sections`` blocks → unified
    ``spans`` OVERLAY entries) via
    :func:`setforge.overlay_migration.migrate_local_yaml_overlay_spans`
    (ruamel round-trip — comments / order / quoting / file mode preserved).

    The PRE-migration text is captured BEFORE the rewrite so the caller can seed
    the install transition's ``file_pre`` with it (via
    :func:`seed_overlay_migration_snapshot`); the rewritten file is then
    snapshotted as ``file_post``, so ``revert`` restores the exact
    pre-migration ``local.yaml`` (bytes; mode is preserved by the round-trip
    write and untouched by ``patch -R``). The one-time, actionable warning
    fires here only when a block was actually moved.

    Idempotent: a steady-state / already-migrated / absent ``local.yaml`` is
    left untouched and reports ``migrated=False`` (no warning, no transition
    delta for the file).

    ``local_config_path`` defaults to :data:`setforge.source.LOCAL_CONFIG_PATH`
    resolved at CALL time (via the module, not a bound import) so the test
    suite's ``conftest`` redirect of that constant — and any future
    host-config relocation — flows through here instead of pinning the
    dev-host real ``~/.config/setforge/local.yaml`` at import.
    """
    from setforge import source

    path = (
        local_config_path if local_config_path is not None else source.LOCAL_CONFIG_PATH
    )
    pre_text: str | None
    try:
        pre_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        pre_text = None
    result = migrate_local_yaml_overlay_spans(path)
    if result.migrated:
        # WHAT changed + HOW to undo it (matches `_warn_auto_migration`).
        typer.secho(
            f"{path}: retired legacy `host_local_sections` into "
            "unified `spans` overlay entries (comments and file mode "
            "preserved). To restore the pre-migration local.yaml, run "
            f"`setforge revert --profile={profile}`.",
            err=True,
            fg=typer.colors.YELLOW,
        )
    return OverlaySpanMigration(
        path=path,
        pre_text=pre_text,
        migrated=result.migrated,
    )


def seed_overlay_migration_snapshot(
    migration: OverlaySpanMigration,
    dst_paths: list[Path],
    file_pre: dict[Path, str | None],
) -> None:
    """Record the overlay-span rewrite in the install transition (revert lockstep).

    No-op when ``migration.migrated`` is ``False``. Otherwise appends the
    rewritten ``local.yaml`` to ``dst_paths`` (so ``file_post`` captures its
    post-migration content) and overwrites ``file_pre`` for that path with the
    genuine PRE-migration text, so the recorded patch reverses the rewrite and
    ``revert`` restores the exact pre-migration ``local.yaml`` byte-for-byte.

    Mutates ``dst_paths`` and ``file_pre`` in place.
    """
    if not migration.migrated:
        return
    if migration.path not in dst_paths:
        dst_paths.append(migration.path)
    file_pre[migration.path] = migration.pre_text


def _validate_span_file_types(
    cfg: Config, resolved: ResolvedProfile, repo_root: Path
) -> None:
    """Reject spans declared on non-markdown tracked_files BEFORE deploy.

    Iterates every tracked_file in the resolved profile (whose ``spans``
    list already folds in the host-local overlay via
    :func:`setforge.config.apply_host_local_tracked_file_overrides`) and
    routes it through :func:`setforge.spans.validate_spans_file_type`, so a
    heading-text span anchor on a yaml/json file aborts the install
    cleanly instead of failing as a confusing runtime relocation miss.
    """
    for tf_id in resolved.tracked_files:
        tracked_file = cfg.tracked_files[tf_id]
        if not tracked_file.spans:
            continue
        src = resolve_src(tracked_file, repo_root)
        validate_spans_file_type(tf_id, tracked_file.spans, src)


def _reconcile_shared_spans(
    cfg: Config,
    *,
    profile: str,
    reconcile_user_sections: bool,
    section_auto: ReconcileAuto | None,
) -> frozenset[tuple[str, str]]:
    """Resolve host-local↔shared span intent collisions before the overlay fold.

    Returns the set of ``(tracked_file_id, anchor)`` pairs whose SHARED
    span should win the per-anchor fold — i.e. the collisions the user
    chose to resolve toward the tracked-side (shared) intent. The caller
    threads the result into
    :func:`setforge.config.apply_host_local_tracked_file_overrides` as
    ``prefer_shared_anchors``; an empty set leaves every collision at the
    silent host-local-wins default.

    Routing (mirrors the shared user-section reconcile surface):

    - **No collisions** → empty set, no output (the shared span just
      applies; nothing to reconcile).
    - **Bare install** (``reconcile_user_sections`` False, ``section_auto``
      None) → empty set, NO warning. An intentional host-local shadow must
      not nag; the silent host-local-wins matches shared
      user-sections.
    - ``--auto=use-tracked`` → every collision resolves to the shared
      intent AND an explicit "host-local span X overwritten" risk line is
      printed per collision — never a silent host-local drop.
    - ``--auto=keep-live`` → empty set, no risk line (the host-local
      override is the protected side).
    - ``--reconcile-user-sections`` (no ``--auto``):
      - non-tty → raise :class:`SharedSpanReconcileRequiresInteractive`
        rather than a silent keep-live that buries the collision.
      - tty → per-collision arrow-key prompt; each prompt's outcome adds
        (or omits) the pair from the prefer-shared set.

    ``section_auto`` and ``reconcile_user_sections`` are already mutually
    exclusive at the CLI (:func:`_parse_section_auto`), so at most one of
    the auto / interactive branches fires.
    """
    collisions = config_mod.detect_shared_span_collisions(cfg)
    if not collisions:
        return frozenset()

    if section_auto is ReconcileAuto.USE_TRACKED:
        for collision in collisions:
            typer.secho(
                f"shared-span reconcile: host-local span {collision.anchor!r} "
                f"on tracked_file {collision.tracked_file_id!r} overwritten by "
                "the shared intent (--auto=use-tracked)",
                err=True,
                fg=typer.colors.YELLOW,
            )
        return frozenset((c.tracked_file_id, c.anchor) for c in collisions)
    if section_auto is ReconcileAuto.KEEP_LIVE:
        # Protected side: keep every host-local override, no risk line.
        return frozenset()
    if section_auto is not None:
        assert_never(section_auto)

    if not reconcile_user_sections:
        # Bare install: silent host-local-wins, no nag.
        return frozenset()

    # Interactive reconcile requested.
    if not sys.stdout.isatty():
        raise SharedSpanReconcileRequiresInteractive(
            "setforge install --reconcile-user-sections detected "
            f"{len(collisions)} host-local/shared span collision(s) but stdout "
            "is not a TTY. Re-run with --auto=use-tracked (adopt the shared "
            "intent) or --auto=keep-live (keep the host-local override)."
        )
    return _prompt_shared_span_collisions(collisions, profile=profile)


def _prompt_shared_span_collisions(
    collisions: list[SharedSpanCollision],
    *,
    profile: str,
) -> frozenset[tuple[str, str]]:
    """Per-collision arrow-key prompt; return the adopt-shared pairs.

    Reuses the :func:`setforge.cli._confirm.confirm_auto_operation` gate
    one collision at a time: a "yes" adopts the shared intent for that
    anchor (the pair joins the prefer-shared set), a "no" keeps the
    host-local override. Only reached on the interactive (tty) path; the
    non-tty *stdout* branch (``sys.stdout.isatty()`` False) raises in
    :func:`_reconcile_shared_spans` before here, so the inner
    ``confirm_auto_operation`` ``sys.stdin.isatty()`` gate sees a tty stdin
    on every reachable call.
    """
    prefer: set[tuple[str, str]] = set()
    for collision in collisions:
        plan = AutoPlan(
            direction=AutoDirection.TRACKED_TO_LIVE,
            file_changes=(),
            risks=(
                f"host-local span {collision.anchor!r} on tracked_file "
                f"{collision.tracked_file_id!r} will be overwritten by the "
                "shared intent",
            ),
            revert_command=f"setforge revert --profile={profile}",
        )
        if confirm_auto_operation(
            command="install --reconcile-user-sections",
            profile=profile,
            plan=plan,
            yes=False,
        ):
            prefer.add((collision.tracked_file_id, collision.anchor))
    return frozenset(prefer)


def _check_unexpected_drift(
    drift_report: compare_mod.CompareReport,
    ctx: ProfileContext,
    *,
    auto_accept_tracked: bool,
    auto_accept_live: bool,
) -> None:
    """Reject unexpected drift on a bare install, or return when a flag resolves it.

    The only unexpected-drift axis this gate rejects is ``mode_drift``
    (permission bits). When a ``DRIFTED`` entry carries it and neither
    ``--auto-accept-tracked`` nor ``--auto-accept-live`` is set, print an
    actionable error and raise ``typer.Exit(1)``. With a flag set, the
    confirm gate in :func:`_confirm_legacy_drift_or_exit` has already
    run, so this is a no-op. No-op when nothing carries unexpected drift.
    """
    has_real_unexpected = any(
        e.status == CompareStatus.DRIFTED and e.mode_drift for e in drift_report.entries
    )
    if not has_real_unexpected:
        return

    unexpected_count = sum(
        1
        for e in drift_report.entries
        if e.status == CompareStatus.DRIFTED and e.mode_drift
    )
    if not (auto_accept_tracked or auto_accept_live):
        typer.secho(
            f"permission-mode drift in {unexpected_count} file(s) "
            f"(profile '{ctx.profile}'): "
            f"pass --auto-accept-tracked or --auto-accept-live to resolve",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)


def _build_conflict_resolver(
    *,
    reconcile_user_sections: bool,
    section_auto: ReconcileAuto | None,
) -> disposition_merge.ConflictResolver | None:
    """Build the interactive disposition conflict resolver, or ``None``.

    Returns a keyboard wizard (:func:`setforge.conflict_wizard.make_wizard_resolver`)
    ONLY when the install is in the interactive-reconcile mode AND stdout is a
    tty — the SAME gate the shared user-section wizard uses
    (``reconcile_user_sections`` is the interactive switch; ``section_auto`` and
    ``reconcile_user_sections`` are already mutually exclusive at the CLI). When
    ``section_auto`` is set the auto policy resolves every conflict
    (``merge_auto`` in the driver), so no resolver is built; a non-tty install
    (piped / scripted) likewise gets ``None`` so the bare warn-and-defer path
    is unchanged.

    The tty check is the seam that keeps a non-interactive ``setforge install``
    (CliRunner, CI) from ever prompting: tests inject a scripted resolver by
    monkeypatching this function so no real tty is needed.
    """
    if not reconcile_user_sections or section_auto is not None:
        return None
    if not sys.stdout.isatty():
        return None
    # Local import: pulls rich Console + the wizard machinery only on the
    # interactive path (validate / dry-run cold-start budget).
    from setforge import conflict_wizard

    return conflict_wizard.make_wizard_resolver()


def _deploy_all_tracked_files(
    ctx: ProfileContext,
    *,
    section_decisions: Mapping[Path, dict[str, str]],
    live_sections_map: Mapping[Path, LiveSections],
    host_local_sections_map: Mapping[str, dict[HostLocalSectionName, HostLocalSection]],
    section_auto: ReconcileAuto | None = None,
    conflict_resolver: disposition_merge.ConflictResolver | None = None,
    strict_spans: bool = False,
) -> tuple[transitions.StateSnapshotEntry, ...]:
    """Deploy every tracked_file in two passes: resolve all, THEN write all.

    Returns the pre-install store snapshots captured at the pass-2 barrier
    (see :func:`_capture_store_snapshots`) so the caller records them on
    the install transition for store-state revert.

    **Pass 1 (read-only).** For each regular-file sub-entry: plan the
    disposition base (:func:`_plan_disposition_base` — any migration write is
    DEFERRED), read the spans sidecar, and compute the full merge + span
    overlay in memory via :func:`deploy.resolve_deploy`. Nothing is written;
    the per-file outcomes accumulate as :class:`_PendingDeploy` records
    (bounded by the config-tree size). Symlink-declared tracked_files are
    deferred wholesale (``resolved=None``) — their deploy primitive is
    self-contained and span-free.

    **Refusal gate.** With ``strict_spans`` set, ANY pinned-span orphan
    across the records refuses the whole install
    (:func:`_refuse_on_pinned_orphans`) — every orphan is reported and ZERO
    tracked files, bases, sidecars, or transitions are touched (bootstrap
    stubs are created earlier in the pipeline), so there is no partial
    install to undo.

    **Pass 2 (writes).** :func:`_execute_pending_deploys` replays the records
    in order, per file: apply the deferred base migration → write the
    resolved content (or deploy the symlink) → echo → advance the byte base →
    advance the spans sidecar (lockstep per file, never
    write-all-then-advance-all). The advance-only-AFTER-the-live-write
    ordering is load-bearing: a base that lags live is the safe failure
    direction (the next install re-merges against a stale-but-valid
    ancestor); a base written before or around the live write could end up
    ahead of live, which is corruption. A deferred conflict
    (``merge_conflicts`` non-empty, ``new_base is None``) keeps live and
    warns; its base stays put so the divergence re-surfaces next install.
    After the loop, every base under the profile whose file_id is NOT in
    this run's disposition keep-set is pruned — gated on pass-2 completion,
    so a refused install prunes nothing.

    ``file_id`` is the ``expand_tracked_file`` synthetic ``sub_name``
    (``name`` for plain files, ``name/relpath`` for directory entries) —
    the same stable per-profile identifier the prune keep-set and
    transitions use. ``sub_name`` is always a relative path with no ``..``
    component (``name`` is a config key; ``relpath`` is taken
    ``relative_to`` the src dir), so it satisfies ``base_store``'s
    traversal guard (:func:`setforge.base_store._resolve_target`).

    ``conflict_resolver`` is the OPTIONAL interactive disposition conflict
    resolver (built by :func:`_build_conflict_resolver`), threaded into every
    disposition :func:`deploy.resolve_deploy` call (its prompts fire during
    pass 1, before any write). ``None`` (non-interactive / non-tty /
    ``--auto``) leaves the bare warn-and-defer behavior unchanged.
    """
    profile = ctx.profile
    pending: list[_PendingDeploy] = []
    for name in ctx.resolved.tracked_files:
        tracked_file = ctx.cfg.tracked_files[name]
        host_local = host_local_sections_map.get(name) or None
        src = resolve_src(tracked_file, ctx.repo_root)
        dst = resolve_dst(tracked_file)
        for sub_name, sub_src, sub_dst in expand_tracked_file(name, src, dst):
            pending.append(
                _resolve_one_pending(
                    profile,
                    sub_name,
                    sub_src,
                    sub_dst,
                    tracked_file,
                    host_local=host_local,
                    section_auto=section_auto,
                    conflict_resolver=conflict_resolver,
                )
            )
    if strict_spans:
        _refuse_on_pinned_orphans(pending)
    return _execute_pending_deploys(profile, pending)


def _resolve_one_pending(
    profile: str,
    sub_name: str,
    sub_src: Path,
    sub_dst: Path,
    tracked_file: TrackedFile,
    *,
    host_local: dict[HostLocalSectionName, HostLocalSection] | None,
    section_auto: ReconcileAuto | None,
    conflict_resolver: disposition_merge.ConflictResolver | None,
) -> _PendingDeploy:
    """Resolve one sub-entry's pass-1 record (read-only; no writes).

    The per-``sub_name`` body of :func:`_deploy_all_tracked_files`'s pass-1
    loop: defer a symlink-declared tracked_file wholesale, otherwise plan
    the disposition base, read the spans sidecar, and compute the merge +
    span overlay in memory via :func:`deploy.resolve_deploy`.
    """
    if tracked_file.symlink is not None:
        # Symlink-deployed: deferred wholesale to pass 2
        # (resolved=None). The link lands at ``sub_dst`` and the
        # tracked content lands at
        # ``Path(tracked_file.symlink).expanduser()``. The host-local
        # overlay still composes; the stored base lifecycle is
        # regular-file-only — never wired here.
        return _PendingDeploy(
            sub_name=sub_name,
            sub_src=sub_src,
            sub_dst=sub_dst,
            tracked_file=tracked_file,
            host_local=host_local,
            file_spans=[],
            resolved=None,
            base_plan=None,
        )
    # Stored-base 3-way path is gated on a declared disposition.
    # PLAN the base (a pure read): it is the merge ancestor the
    # driver diffs live/tracked against. Deferred migration writes
    # (base seed / live marker strip) are applied in pass 2; when a
    # live strip is pending, the stripped text overrides the on-disk
    # live as the merge input (``live_text``).
    base_plan: DispositionBasePlan | None = None
    base_text: str | None = None
    live_override: str | None = None
    if tracked_file.disposition is not None:
        base_plan = _plan_disposition_base(profile, sub_name, sub_dst)
        base_text = base_plan.base_text
        live_override = base_plan.deferred_live_strip
    # Span re-overlay path: READ the spans sidecar so the relocation
    # ladder has its derived state. Spans ride the disposition 3-way
    # path AND the disposition=None markerless host-local overlay
    # inject, so load them whenever the tracked_file declares any
    # span, not only on the disposition path.
    file_spans = tracked_file.spans or []
    span_states = spans_store.get_states(profile, sub_name) if file_spans else {}
    # NOTE: a later change adds an upstream rename/delete classifier
    # here, refining each collected orphan with a reason.
    resolved = deploy.resolve_deploy(
        sub_src,
        sub_dst,
        host_local_sections=host_local,
        mode=tracked_file.mode,
        disposition=tracked_file.disposition,
        base_text=base_text,
        merge_auto=section_auto,
        conflict_resolver=conflict_resolver,
        spans=file_spans or None,
        span_states=span_states or None,
        live_text=live_override,
    )
    return _PendingDeploy(
        sub_name=sub_name,
        sub_src=sub_src,
        sub_dst=sub_dst,
        tracked_file=tracked_file,
        host_local=host_local,
        file_spans=file_spans,
        resolved=resolved,
        base_plan=base_plan,
    )


@dataclass(slots=True, frozen=True)
class _PendingDeploy:
    """One pass-1 resolution awaiting its pass-2 write.

    ``resolved`` is the in-memory :class:`deploy.ResolvedDeploy` for a
    regular-file deploy, or ``None`` for a symlink-declared tracked_file
    (deferred wholesale — pass 2 runs :func:`deploy.deploy_symlinked_file`
    end to end). ``base_plan`` carries the disposition base plus its deferred
    migration writes (``None`` when the tracked_file has no disposition or
    deploys via symlink).
    """

    sub_name: str
    sub_src: Path
    sub_dst: Path
    tracked_file: TrackedFile
    host_local: dict[HostLocalSectionName, HostLocalSection] | None
    file_spans: list[SpanEntry]
    resolved: deploy.ResolvedDeploy | None
    base_plan: DispositionBasePlan | None


def _span_orphan_warning(sub_dst: Path, orphan: SpanOrphan) -> str:
    """Render the one-line warning for a span orphan.

    The single wording seam shared by the pass-1 ``--strict-spans`` gate
    (:func:`_refuse_on_pinned_orphans`) and the pass-2 warn
    (:func:`_advance_span_states`). An
    upstream-renamed-or-deleted structural orphan gets the upstream
    attribution plus a did-you-mean over the tracked sibling keys the
    orphan carries (:func:`~setforge.cli._validate_errors.suggest_close_match`
    on the anchor's leaf segment — suggestion rendering lives HERE because
    the merge driver must not import from ``cli``); every other orphan
    keeps the generic could-not-be-relocated wording.
    """
    upstream_reason = (
        disposition_merge.StructuralSpanOrphanReason.UPSTREAM_RENAMED_OR_DELETED
    )
    if orphan.reason != upstream_reason:
        return (
            f"warning: {sub_dst}: span {orphan.anchor!r} ({orphan.kind.value}) "
            f"could not be relocated upstream — region preserved, not dropped"
        )
    message = (
        f"warning: {sub_dst}: span {orphan.anchor!r} ({orphan.kind.value}) "
        f"was renamed or deleted upstream — region preserved, not dropped"
    )
    leaf = orphan.anchor.rpartition(".")[2]
    suggestion = suggest_close_match(leaf, list(orphan.tracked_siblings))
    if suggestion is not None:
        message += f" (did you mean {suggestion!r}?)"
    return message


def _refuse_on_pinned_orphans(pending: list[_PendingDeploy]) -> None:
    """Refuse the whole install when any pass-1 record carries a PINNED orphan.

    The ``--strict-spans`` gate, run BETWEEN pass 1 (read-only resolve) and
    pass 2 (writes): when at least one pinned span orphaned, every collected
    orphan warning is printed (same wording as the pass-2 warn in
    :func:`_advance_span_states` — pass 2 never runs on this path) and ONE
    aggregated :class:`SetforgeError` is raised with a per-file refusal line,
    so the user sees the COMPLETE orphan set instead of one file per attempt.
    No-op when no pinned orphan exists (forked orphans warn in pass 2 but
    never refuse).
    """
    has_pinned = any(
        orphan.kind is SpanKind.PINNED
        for record in pending
        if record.resolved is not None
        for orphan in record.resolved.span_orphans
    )
    if not has_pinned:
        return
    failures: list[str] = []
    for record in pending:
        if record.resolved is None:
            continue
        pinned: list[str] = []
        for orphan in record.resolved.span_orphans:
            typer.secho(
                _span_orphan_warning(record.sub_dst, orphan),
                err=True,
                fg=typer.colors.YELLOW,
            )
            if orphan.kind is SpanKind.PINNED:
                pinned.append(orphan.anchor)
        if pinned:
            joined = ", ".join(repr(a) for a in pinned)
            failures.append(
                f"{record.sub_dst}: --strict-spans: pinned span(s) {joined} "
                f"orphaned (anchor gone upstream); refusing install"
            )
    raise SetforgeError("\n".join(failures))


def _capture_store_snapshots(
    profile: str,
    pending: list[_PendingDeploy],
) -> tuple[transitions.StateSnapshotEntry, ...]:
    """Snapshot the pre-install state of every store entry pass 2 can touch.

    The ONE barrier feeding the transition's ``state_snapshots/`` payload:
    for each regular-file record, a disposition declaration snapshots the
    byte base AND the scalar-base manifest (the scalar store has no
    install-path writer yet, but capturing it here means a future writer
    is covered without a snapshot-schema change), and a span declaration
    snapshots the spans sidecar manifest. Symlink records are skipped —
    their deploy primitive never touches the stores. Absent entries
    capture as ``payload=None`` so revert DELETES what this install
    seeds. Must run at pass-2 entry, before ANY write (pass 1 is
    read-only, so nothing can drift between the barrier and the writes).
    """
    entries: list[transitions.StateSnapshotEntry] = []
    for record in pending:
        tracked_file = record.tracked_file
        if tracked_file.symlink is not None:
            continue
        if tracked_file.disposition is not None:
            entries.append(
                transitions.snapshot_store_state(
                    transitions.SnapshotStore.BASE, profile, record.sub_name
                )
            )
            entries.append(
                transitions.snapshot_store_state(
                    transitions.SnapshotStore.SCALAR_BASE, profile, record.sub_name
                )
            )
        if record.file_spans:
            entries.append(
                transitions.snapshot_store_state(
                    transitions.SnapshotStore.SPANS, profile, record.sub_name
                )
            )
    return tuple(entries)


def _execute_pending_deploys(
    profile: str,
    pending: list[_PendingDeploy],
) -> tuple[transitions.StateSnapshotEntry, ...]:
    """Pass 2: replay the pass-1 records in order, performing every write.

    Snapshots the pre-install store state FIRST (one barrier, before any
    write — see :func:`_capture_store_snapshots`) and returns the entries
    so the caller threads them into the install transition. Then, per
    record: apply the deferred base-migration writes (seed-first order +
    the one-time warning) → write the resolved content via
    :func:`deploy.write_resolved_deploy` (or run
    :func:`deploy.deploy_symlinked_file` for a symlink record) → echo the
    action → advance the disposition byte base → advance the spans sidecar,
    in lockstep per file. After the loop, prune bases keyed on the executed
    disposition set — so a gate refusal (which never reaches this function)
    prunes nothing.
    """
    state_snapshots = _capture_store_snapshots(profile, pending)
    disposition_file_ids: set[str] = set()
    for record in pending:
        tracked_file = record.tracked_file
        if tracked_file.symlink is not None:
            result = deploy.deploy_symlinked_file(
                record.sub_src,
                record.sub_dst,
                tracked_file,
                host_local_sections=record.host_local,
            )
            typer.echo(
                f"{result.action.value:>8}  {record.sub_dst} -> {tracked_file.symlink}"
            )
            _echo_host_local_sections_provenance(record.host_local)
            continue
        if record.resolved is None:
            raise AssertionError(
                f"pending deploy for {record.sub_name!r} has no resolution "
                "and no symlink declaration"
            )
        if record.base_plan is not None:
            disposition_file_ids.add(record.sub_name)
            _apply_deferred_base_migration(
                profile, record.sub_name, record.sub_dst, record.base_plan
            )
        result = deploy.write_resolved_deploy(record.resolved)
        typer.echo(f"{result.action.value:>8}  {record.sub_dst}")
        _echo_host_local_sections_provenance(record.host_local)
        # ADVANCE the disposition base only AFTER the live write.
        if tracked_file.disposition is not None:
            _advance_disposition_base(profile, record.sub_name, record.sub_dst, result)
        # ADVANCE the spans sidecar + warn on orphans AFTER the live write,
        # in lockstep with the byte base.
        if record.file_spans:
            _advance_span_states(
                profile, record.sub_name, record.sub_dst, result, record.file_spans
            )
    # PRUNE after the whole loop: bases whose file_id is not in this run's
    # disposition keep-set (file left the profile, or lost its disposition)
    # are removed. Non-disposition files never have a base, so an empty
    # keep-set still correctly clears any stale bases under the profile.
    base_store.prune(profile, disposition_file_ids)
    return state_snapshots


@dataclass(slots=True, frozen=True)
class DispositionBasePlan:
    """Read-only plan for a disposition file's merge-ancestor base.

    Produced by :func:`_plan_disposition_base` WITHOUT touching the
    filesystem; the deferred writes are applied later by
    :func:`_apply_deferred_base_migration` (immediately before the file's
    deploy write), so a refusal between planning and writing leaves zero
    footprint.

    ``base_text`` is the text the caller threads into
    :func:`deploy.resolve_deploy` as ``base_text`` (``None`` keeps the
    ordinary base-absent, deploy-tracked-verbatim path). ``migrated`` is
    ``True`` ONLY when this install plans an auto-migration (seeding a
    per-host base from the live file, stripping legacy shared-section markers
    where present) — the one-time install warning fires on that signal at
    apply time. A steady-state read (base already present, no markers) and a
    crash-resume completion (base present, live re-stripped without
    re-seeding) both report ``migrated=False`` so the warning fires exactly
    once across the whole migration, never on a resumed or already-migrated
    install.

    ``deferred_seed`` is the byte payload :func:`base_store.write_base` must
    receive (``None`` when no seed is pending); ``deferred_live_strip`` is
    the stripped text the LIVE file must be rewritten to (``None`` when no
    strip is pending). When a strip is pending, disk live ≠ the live the
    merge must see, so the caller threads ``deferred_live_strip`` into
    :func:`deploy.resolve_deploy` as the ``live_text`` override.
    """

    base_text: str | None
    migrated: bool
    deferred_seed: bytes | None = None
    deferred_live_strip: str | None = None


def _plan_disposition_base(
    profile: str,
    file_id: str,
    sub_dst: Path,
) -> DispositionBasePlan:
    """Plan the disposition merge-ancestor base WITHOUT writing anything.

    Reads the stored base for ``file_id`` under ``profile`` and routes:

    * **Base present, live has NO legacy markers** — steady state. The stored
      base is returned verbatim (``migrated=False``, nothing deferred).
    * **Base present, live STILL carries legacy markers** (markdown /
      line-based files only — structured files have no inline markers) —
      routed by :func:`_plan_resume_marker_strip`. When the base is still the
      markerless SEED (``strip_shared_markers(live) == base``) this is the
      crash-resume state (a kill landed AFTER the seed-first base write but
      BEFORE the live strip): the strip is planned as ``deferred_live_strip``
      WITHOUT re-seeding. When the base has been ADVANCED past the seed (a
      ``disposition: shared`` file whose tracked content legitimately carries
      an in-content shared marker — re-installs always re-deploy that marker
      into live and the advance re-baselines to the marker-bearing form), the
      resume stands down: it is steady state, not an interrupted migration.
      Either way the seeded/advanced base is returned (``migrated=False`` —
      the seed ran on a prior install, so no warning fires this run).
    * **Base ABSENT** — a file entering the disposition world for the first
      time, routed by format:

      - **Structured (JSON / JSONC / YAML)** files have no inline markers, so
        the plan seeds the base from the current LIVE text as read by
        :meth:`~pathlib.Path.read_text` (universal-newline) — the EXACT view
        :func:`deploy.resolve_deploy` reads as ``ours``, so base == live ==
        ours at the level the merge parses (CRLF / CR collapse to LF on both
        sides). ``migrated=True`` when a live file existed to seed from; the
        live file itself is never rewritten (no markers to strip).
      - **Markdown / line-based** files plan the SHARED-marker strip via
        :func:`_plan_shared_marker_migration` (``migrated=True`` when a
        marker-bearing live file is to be migrated): ``deferred_seed`` and
        ``deferred_live_strip`` both carry the stripped text, so after apply
        base == stripped == live and the first 3-way merge has zero spurious
        delta (the data-loss invariant).

    The base-absent format paths fall through to ``base_text=None`` /
    ``migrated=False`` — the ordinary base-absent (deploy-tracked-verbatim)
    path — when there is no live file to seed from (and, for markdown, no
    SHARED markers to strip).

    Raises :class:`~setforge.errors.MarkerError` on a malformed marker file
    (via the strip), propagated from the leaf helpers rather than swallowed
    here — BEFORE any write, so a malformed file aborts a still-clean
    install.
    """
    raw = base_store.read_base(profile, file_id)
    if raw is not None:
        base_text = raw.decode("utf-8")
        deferred_live_strip: str | None = None
        if not disposition_merge.is_structural(sub_dst):
            # Base present: plan the completion of an interrupted strip if
            # live still carries legacy markers (crash-resume). No re-seed —
            # the base is the truth.
            deferred_live_strip = _plan_resume_marker_strip(sub_dst, base_text)
        return DispositionBasePlan(
            base_text=base_text,
            migrated=False,
            deferred_live_strip=deferred_live_strip,
        )
    if disposition_merge.is_structural(sub_dst):
        try:
            live_text = sub_dst.read_text(encoding="utf-8")
        except FileNotFoundError:
            return DispositionBasePlan(base_text=None, migrated=False)
        return DispositionBasePlan(
            base_text=live_text,
            migrated=True,
            deferred_seed=live_text.encode("utf-8"),
        )
    stripped = _plan_shared_marker_migration(sub_dst)
    if stripped is None:
        return DispositionBasePlan(base_text=None, migrated=False)
    return DispositionBasePlan(
        base_text=stripped,
        migrated=True,
        deferred_seed=stripped.encode("utf-8"),
        deferred_live_strip=stripped,
    )


def _plan_shared_marker_migration(sub_dst: Path) -> str | None:
    """Compute the SHARED-marker strip for a base-absent markdown file (no writes).

    The EXPAND half of the section→disposition migration, called only when
    ``sub_dst``'s stored base is ABSENT (a file entering the disposition
    world for the first time). Computes the stripped-live text IN MEMORY via
    :func:`setforge.sections.strip_shared_markers` (which parses the WHOLE
    file via the marker state machine first, so a malformed file raises
    :class:`~setforge.errors.MarkerError` before the install writes anything
    — no partial output, no half-migrated file).

    Returns the stripped text when the live file exists AND still carries
    legacy ``shared`` user-section markers; returns ``None`` — leaving the
    caller on the ordinary base-absent (deploy-tracked-verbatim) path — when
    the live file is absent OR carries no SHARED markers. Host-local markers
    and tracked-side markers are NOT touched. The gate is strict on the
    ``(base absent, shared markers present)`` pair (base-absence is the
    caller's precondition; shared-marker presence is ``stripped !=
    live_text``), so a second install — where the base now exists OR the
    markers are already stripped — never re-plans the migration.
    """
    try:
        live_text = sub_dst.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    stripped = strip_shared_markers(live_text)
    if stripped == live_text:
        # No SHARED markers to strip: fall through to the ordinary
        # base-absent path (deploy tracked verbatim, seed base == tracked).
        return None
    return stripped


def _plan_resume_marker_strip(sub_dst: Path, base_text: str) -> str | None:
    """Plan the completion of an interrupted SHARED-marker strip (no writes).

    Targets the crash-resume state: the seed-first base write landed but a
    kill hit BEFORE the live strip, so the stored base is PRESENT yet live
    still carries legacy SHARED markers. In that window the base is the SEED
    — i.e. ``base_text == strip_shared_markers(live)`` — because live is
    byte-unchanged since the seed. Re-stripping the unchanged marker-bearing
    live therefore reproduces the seeded base byte-for-byte, so the plan
    rewrites live to the stripped form to reach base == live == stripped
    WITHOUT touching the base (no re-seed; the seeded base is the truth).

    Two states return ``None`` (nothing to resume):

    * **Steady state, no markers** — a live file with NO SHARED markers
      leaves ``stripped == live_text``.
    * **Steady state, base advanced PAST the seed** — a ``disposition:
      shared`` file whose TRACKED content legitimately carries an in-content
      shared marker deploys that marker into live on every install, and the
      post-deploy advance re-baselines the stored base to the merged,
      marker-BEARING form. On the next install live still carries the marker,
      but the base is no longer the markerless seed, so
      ``strip_shared_markers(live) != base_text``. This is NOT an interrupted
      migration — the strip already ran and was advanced over — so there is
      nothing to resume; the stored byte-base is the merge ancestor
      :func:`deploy.resolve_deploy`'s 3-way driver owns from here.
      Re-stripping live would diverge from the advanced base and corrupt the
      ancestor, so the resume must stand down.

    The resume thus fires ONLY in its genuine window
    (``stripped == base_text``); any other ``(base present, live
    marker-bearing)`` shape is steady state and untouched.
    """
    try:
        live_text = sub_dst.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    stripped = strip_shared_markers(live_text)
    if stripped == live_text:
        # Steady state: no markers left to strip, nothing to resume.
        return None
    if stripped != base_text:
        # Base advanced past the markerless seed (steady-state re-install of a
        # disposition:shared file whose tracked content retains its shared
        # marker): not an interrupted migration. Leave live and the advanced
        # base alone — the 3-way merge driver is the ancestor's owner now.
        return None
    return stripped


def _apply_deferred_base_migration(
    profile: str,
    file_id: str,
    sub_dst: Path,
    plan: DispositionBasePlan,
) -> None:
    """Apply a plan's deferred base-migration writes in the crash-safe order.

    No-op for a steady-state plan (nothing deferred). Otherwise:

    1. Seed the stored base from ``deferred_seed`` FIRST.
    2. THEN rewrite live to ``deferred_live_strip``, preserving the file's
       EXISTING mode (0600 stays 0600) via the fchmod-before-replace pattern
       (no symlink-follow, no mode widening).
    3. Fire the one-time :func:`_warn_auto_migration` when the plan is a
       genuine auto-migration (``migrated`` True) — never on a crash-resume
       completion or a steady-state read.

    **Crash-safe ordering (never base-absent-after-strip).** Seeding the base
    before the live strip means a kill between the two leaves base-PRESENT +
    live-still-marker-bearing — a state the next install's
    :func:`_plan_resume_marker_strip` completes WITHOUT re-seeding. The
    inverted order (strip first) would, on a kill, leave
    base-absent-after-strip: a re-run would see the markers already stripped,
    fall through to the base-absent verbatim-deploy path, and skip the clean
    migration — the loss this ordering prevents.
    """
    if plan.deferred_seed is not None:
        base_store.write_base(profile, file_id, plan.deferred_seed)
    if plan.deferred_live_strip is not None:
        existing_mode = stat.S_IMODE(sub_dst.stat().st_mode)
        _atomic_rewrite_preserving_mode(
            sub_dst, plan.deferred_live_strip, existing_mode
        )
    if plan.migrated:
        _warn_auto_migration(sub_dst, profile)


def _atomic_rewrite_preserving_mode(path: Path, content: str, mode: int) -> None:
    """Atomically write ``content`` to ``path`` at ``mode`` (fchmod-before-replace).

    Mirrors :func:`setforge.deploy._atomic_write`'s safety contract for a
    live rewrite that is NOT a tracked-source deploy: a same-directory temp
    file gets ``content`` and ``mode`` applied to its fd via
    :func:`os.fchmod` BEFORE :func:`os.replace`, so the final mode lands in
    the same FS object (closing the TOCTOU symlink-swap window a path-based
    chmod would open) and a pre-existing ``path`` symlink is REPLACED rather
    than followed. ``mode`` is the file's existing mode, so 0600 stays 0600
    — the rewrite never widens permissions.
    """
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fchmod(fh.fileno(), mode)
        os.replace(tmp_path, path)
    finally:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)


def _warn_auto_migration(sub_dst: Path, profile: str) -> None:
    """Emit the one-time actionable warning for an auto-on-install migration.

    Fired only when an auto-migration actually ran this install
    (``DispositionBasePlan.migrated`` True) — never on a steady-state or
    crash-resumed install. States WHAT changed (a per-host base was seeded from
    the current live file; any legacy SHARED-section markers were stripped —
    host-local markers are left in place, and structured files have none) and
    HOW to undo it (``setforge revert``), so the silent strip + seed surfaces a
    visible, reversible footprint to the user.
    """
    typer.secho(
        f"{sub_dst}: first install under a stored-base disposition: seeded a "
        "per-host base from your current live file; any legacy shared-section "
        "markers were stripped. To restore the pre-migration state, run "
        f"`setforge revert --profile={profile}`.",
        err=True,
        fg=typer.colors.YELLOW,
    )


def _advance_disposition_base(
    profile: str,
    file_id: str,
    sub_dst: Path,
    result: deploy.DeployResult,
) -> None:
    """Advance the disposition byte-base AFTER a clean live write, or warn.

    ADVANCE to ``result.new_base`` when the driver signalled re-baselining; a
    :func:`base_store.write_base` failure PROPAGATES (no suppress) — base
    lagging live is the safe failure direction, base ahead of live is
    corruption. A deferred conflict (``new_base is None`` with non-empty
    ``merge_conflicts``) keeps live and WARNs so the user knows it re-surfaces
    next install.
    """
    if result.new_base is not None:
        base_store.write_base(profile, file_id, result.new_base.encode("utf-8"))
    elif result.merge_conflicts:
        typer.secho(
            f"warning: {sub_dst}: merge conflict kept live, base not advanced "
            f"— conflict re-surfaces next install (re-run with "
            f"--auto=use-tracked to resolve)",
            err=True,
            fg=typer.colors.YELLOW,
        )


def _advance_span_states(
    profile: str,
    file_id: str,
    sub_dst: Path,
    result: deploy.DeployResult,
    file_spans: list[SpanEntry],
) -> None:
    """Advance the spans sidecar, prune left spans, and warn on orphans.

    Writes every recomputed :class:`~setforge.spans_store.SpanState` from
    the deploy AND prunes any anchor no longer in the file's intent — both
    in LOCKSTEP with the byte base just advanced (Invariant I5). Each
    orphan emits a loud per-span warning; an orphan NEVER aborts the
    default install (Invariant I6). The ``--strict-spans`` escalation lives
    in :func:`_refuse_on_pinned_orphans`, which fires BEFORE any write —
    a pinned orphan under strict mode never reaches this advance.
    """
    if result.new_span_states is not None:
        spans_store.set_states(profile, file_id, result.new_span_states)
    spans_store.prune(profile, file_id, {span.anchor for span in file_spans})
    for orphan in result.span_orphans:
        typer.secho(
            _span_orphan_warning(sub_dst, orphan),
            err=True,
            fg=typer.colors.YELLOW,
        )


def _echo_host_local_sections_provenance(
    host_local_sections: dict[HostLocalSectionName, HostLocalSection] | None,
) -> None:
    """Print a per-section ``injected ... <HOST_LOCAL_PROVENANCE_TAG>`` line.

    No-op when ``host_local_sections`` is ``None`` or empty. The
    provenance tag (see ``HOST_LOCAL_PROVENANCE_TAG`` in
    :mod:`setforge.host_local_inject`) matches the mockup in
    SPEC 1 so users grepping install output can locate
    every host-local injection at a glance.
    """
    if not host_local_sections:
        return
    names = ", ".join(sorted(host_local_sections))
    plural = "s" if len(host_local_sections) != 1 else ""
    typer.echo(
        f"    injected {len(host_local_sections)} host-local section{plural} "
        f"{HOST_LOCAL_PROVENANCE_TAG}: {names}"
    )


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
    state_snapshots: tuple[transitions.StateSnapshotEntry, ...] = (),
) -> Path:
    """Write the install transition record; return the target directory path.

    ``state_snapshots`` carries the pre-install store state captured at
    the pass-2 barrier (:func:`_capture_store_snapshots`); ``revert``
    restores those entries in lockstep with the ``changes.patch`` reverse.

    Two arguments carry schema-bump backward-compat history: ``source_dir``
    (when set and pointing at a git repo,
    :func:`transitions.make_meta` records HEAD's sha so ``setforge
    status`` can compute commits-since-last-install) and
    ``reconcile_outcomes`` (defaults to empty so
    pre-bump callers keep working; when non-empty, serialized to
    ``reconcile_outcomes.json`` alongside ``extensions.json`` /
    ``plugins.json`` so ``install --retry-failed`` can rebuild the
    skipped-ids set on the next invocation).

    ``preserve_user_keys_applied`` is retained on the transition record for
    back-compat with pre-2.0 records; the install path no longer computes it
    and always passes ``None``. ``command_line`` is captured from
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
        state_snapshots=state_snapshots,
    )


def _build_unexpected_drift_plan(
    *,
    drift_report: compare_mod.CompareReport,
    ctx: ProfileContext,
    direction: AutoDirection,
) -> AutoPlan:
    """Build an AutoPlan from a drift report for the --auto-accept-* paths.

    Delegates name → (sub_src, sub_dst) resolution to the shared
    ``_resolve_drift_paths`` helper, then surfaces the one unexpected
    drift axis this gate owns: ``mode_drift`` (permission bits → a
    per-file risk line). A mode-only drift therefore yields a non-empty
    plan (a risk line) so the confirm gate actually fires instead of
    silently auto-proceeding on an empty plan while deploy reapplies the
    tracked mode. Diff-only entries fall through to the bare-install path.
    """
    mode_risks: list[str] = []
    for entry, _sub_src, sub_dst in _resolve_drift_paths(drift_report, ctx):
        if (
            entry.mode_drift
            and entry.live_mode is not None
            and entry.tracked_mode is not None
        ):
            # install always reapplies the tracked mode on deploy (it cannot
            # write the live mode back into setforge.yaml), so the transition
            # is live → tracked regardless of --auto-accept direction.
            mode_risks.append(
                f"{sub_dst}: permission mode "
                f"{entry.live_mode:#o} → {entry.tracked_mode:#o} "
                f"(reset to tracked on deploy)"
            )
    return AutoPlan(
        direction=direction,
        file_changes=(),
        risks=tuple(mode_risks),
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
    auto_accept_tracked: bool,
    auto_accept_live: bool,
    section_auto: ReconcileAuto | None,
    yes: bool,
) -> None:
    """Run the three pre-deploy confirm/reject gates in their fixed order.

    Bundles the unexpected-drift confirm (``--auto-accept-{tracked,live}``)
    + the bare-install unexpected-drift reject + the section-reconcile
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
        auto_accept_tracked=auto_accept_tracked,
        auto_accept_live=auto_accept_live,
    )
    _confirm_section_reconcile_or_exit(
        ctx=ctx,
        section_auto=section_auto,
        yes=yes,
    )


def revert_symlink_deployment(dst: Path, expected_target: str) -> bool:
    """Unlink a symlink installed by setforge — refusing if the user mutated it.

    Contract for ``setforge revert`` of a tracked_file deployed with
    ``symlink:`` declared:

    - If ``dst`` does not exist as a symlink AND does not exist as a
      regular file, return ``False`` — nothing to revert (install
      never landed, or revert already ran). Idempotency.
    - If ``dst`` is a *regular file*, raise :class:`SetforgeError` —
      the user replaced setforge's symlink with their own content;
      revert refuses to delete user data.
    - If ``dst`` is a symlink whose ``os.readlink`` does NOT equal
      ``expected_target``, raise :class:`SetforgeError` — the user
      retargeted setforge's symlink; revert refuses to unlink an
      object that is no longer what setforge installed.
    - Otherwise (``dst`` is a symlink with the expected target):
      :func:`Path.unlink` with ``missing_ok=False``. Returns ``True``
      to signal the link was removed.

    ``missing_ok=False`` (NOT ``True``) is intentional: a successful
    ``is_symlink()`` probe means the link MUST be unlink-able, and
    swallowing :class:`FileNotFoundError` here would mask a TOCTOU
    race (something deleted the link between the probe and the
    unlink) that the caller should see.
    """
    if dst.is_symlink():
        actual = os.readlink(dst)
        if actual != expected_target:
            raise SetforgeError(
                f"refusing to unlink {dst}: symlink target changed since "
                f"deploy ({actual!r} != {expected_target!r}). Re-point or "
                f"remove the link manually if you want revert to proceed."
            )
        dst.unlink(missing_ok=False)
        return True
    if dst.exists():
        raise SetforgeError(
            f"refusing to unlink {dst}: a regular file is present where "
            f"setforge previously installed a symlink "
            f"(target {expected_target!r}). Remove the file manually if "
            f"you want revert to proceed."
        )
    return False


# ---------------------------------------------------------------------------
# Dry-run pipeline.
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

_DRY_RUN_HEADER: Final[str] = "=== DRY-RUN MODE — NOTHING WILL BE MUTATED ==="
"""First line of every dry-run invocation. Unambiguous opener for users + log
scanners."""

_DRY_RUN_FINAL_LINE: Final[str] = "=== rerun without --dry-run to apply for real ==="
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
    state dir, never invokes the auto-confirm confirm wizard, never runs git
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
    _dry_run_emit_host_local_inject(ctx)
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
    unprefixed. The count reports files whose drift is CLASSIFIED
    unexpected or conflicted (the compare-level
    :class:`~setforge.compare.DriftClass`); the live install gate
    (:func:`_check_unexpected_drift`) trips only on permission-mode
    drift (``mode_drift``), so this count can include diff-only drift
    that a real install does not reject. The dry-run path
    never invokes the auto-confirm wizard (short-circuiting before the
    confirm is a hard requirement per spec). ``live_sections_map`` is
    the read-only output of :func:`_extract_live_sections_map`; the
    count is informational.
    """
    typer.echo("=== would-be drift gate ===")
    unexpected = sum(
        1
        for e in drift_report.entries
        if e.drift_class in (DriftClass.UNEXPECTED, DriftClass.CONFLICTED)
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
    for (_tracked, _sub_name, _sub_src, sub_dst), entry in zip(
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


def _dry_run_emit_host_local_inject(ctx: ProfileContext) -> None:
    """Emit the ``=== would-be host-local section inject ===`` block.

    Per SPEC 1's mockup, each ``WOULD inject`` line carries
    a ``HOST_LOCAL_PROVENANCE_TAG`` so users can
    distinguish host-local injections from shared section reconcile
    operations (which produce their own WOULD lines via
    :func:`_dry_run_emit_section_reconcile`). No-op when local.yaml is
    absent or declares no host-local sections for tracked_files in
    this profile.
    """
    typer.echo("=== would-be host-local section inject ===")
    overlay = load_local_host_local_sections()
    profile_ids = set(ctx.resolved.tracked_files)
    matched: list[tuple[str, HostLocalSectionName, Path]] = []
    for tf_id, sections_map in overlay.items():
        if tf_id not in profile_ids:
            continue
        dst = resolve_dst(ctx.cfg.tracked_files[tf_id])
        for section_name in sections_map:
            matched.append((tf_id, section_name, dst))
    if not matched:
        typer.echo("  no host-local sections to inject")
        return
    for tf_id, section_name, dst in matched:
        typer.echo(
            f"  WOULD inject  '{section_name}' into {dst} "
            f"{HOST_LOCAL_PROVENANCE_TAG} (tracked_file {tf_id!r})"
        )


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
