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
- :func:`_confirm_legacy_drift_or_exit`: auto-confirm confirm-or-exit
  wrapper that pairs :func:`_build_unexpected_drift_plan` with
  :func:`setforge.cli._confirm.confirm_auto_operation`.

NO ``@app.command`` decorators; NO ``app`` import — this module is
internal-only and stays out of typer's command surface.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC
from pathlib import Path
from typing import Final, assert_never

import typer

from setforge import (
    base_store,
    deploy,
    disposition_merge,
    reconcile,
    reconcile_apply,
    transitions,
)
from setforge import (
    claude_plugins as claude_plugins_mod,
)
from setforge import (
    compare as compare_mod,
)
from setforge import (
    vscode_extensions as vscode_extensions_mod,
)
from setforge._redact import redact_argv
from setforge.cli._confirm import (
    AutoDirection,
    AutoPlan,
    confirm_auto_operation,
)
from setforge.cli._helpers import (
    ProfileContext,
    _iter_all_tracked_files,
    _refuse_duplicate_section_names,
    _resolve_drift_paths,
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
    TrackedFile,
)
from setforge.errors import (
    ExtensionToolMissing,
    PluginToolMissing,
    SetforgeError,
)
from setforge.host_local_inject import HOST_LOCAL_PROVENANCE_TAG
from setforge.overlay_migration import migrate_local_yaml_overlay_spans
from setforge.reconcile import FileId
from setforge.reconcile.claude_merge import make_claude_merge_fn
from setforge.reconcile.structured_units import structured_format
from setforge.reconcile.wizard import _claude_merge_unavailable
from setforge.section_mode import ReconcileAuto
from setforge.source import (
    HostLocalSection,
    HostLocalSectionName,
    load_local_host_local_sections,
    validate_host_local_sections_file_type,
)
from setforge.span_types import (
    SpanEntry,
    SpanKind,
)
from setforge.spans_overlay import SpanOrphan
from setforge.ui.widgets import Button, Cancelled, button_bar


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
    host_local_sections_map: Mapping[str, dict[HostLocalSectionName, HostLocalSection]],
    section_auto: ReconcileAuto | None = None,
    conflict_resolver: disposition_merge.ConflictResolver | None = None,
    strict_spans: bool = False,
) -> DeployOutcome:
    """Deploy every tracked_file in two passes: resolve all, THEN write all.

    Returns a :class:`DeployOutcome` carrying the pre-install store
    snapshots captured at the pass-2 barrier (see
    :func:`_capture_store_snapshots`) AND the per-path pre-install mode map,
    so the caller records both on the install transition for store-state +
    file-mode revert.

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


def _is_utf8(data: bytes) -> bool:
    """Return True iff ``data`` decodes as UTF-8."""
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _plain_reconcile_content(
    outcome: reconcile_apply.ReconcileOutcome,
    live_bytes: bytes | None,
) -> str | None:
    """The live str to write for a plain-reconcile outcome, or None to fall back.

    ``WRITE`` writes the merged bytes — except a merge that resolved to
    deletion (``ABSENT`` content) returns ``None`` so the caller falls back
    to the legacy verbatim path (the str-based deploy writer cannot express
    a deletion; a rare edge for a locally-deleted file). ``NOOP`` /
    ``DEFERRED`` / ``CANCELLED`` keep the current live content (empty when
    live was absent), so :func:`deploy.write_resolved_deploy` detects a NOOP
    and never clobbers a local edit with the tracked source.
    """
    if outcome.kind is reconcile_apply.ReconcileKind.WRITE:
        if not isinstance(outcome.content, bytes):
            return None
        return outcome.content.decode("utf-8")
    live = live_bytes if live_bytes is not None else b""
    return live.decode("utf-8")


def _seed_prompt_interactive(
    display_path: str,
) -> reconcile_apply.SeedChoice | Cancelled:
    """Themed prompt to seed a divergent live file that has no merge base.

    The base is recorded from the upstream either way; the choice is whether
    live keeps its local edits (recommended — they become a local change atop
    the base) or is replaced with the upstream version. Esc / Ctrl-C cancels
    and aborts the file. A later enrichment adds a divergence diff/preview
    around this same decision.
    """
    return button_bar(
        [
            Button("Keep live", reconcile_apply.SeedChoice.KEEP_LIVE),
            Button("Take upstream", reconcile_apply.SeedChoice.TAKE_UPSTREAM),
        ],
        title=f"seed merge base — {display_path}",
        body=(
            f"{display_path} already exists and differs from upstream, and no "
            "merge base is recorded. Keep your local edits, or replace them "
            "with the upstream version?"
        ),
    )


def _auto_side(section_auto: ReconcileAuto | None) -> reconcile_apply.AutoSide | None:
    """Map the install ``--auto`` mode onto the reconcile resolution side.

    ``keep-live`` keeps the live side (OURS); ``use-tracked`` takes the
    upstream side (THEIRS); ``None`` (no ``--auto``) leaves conflicts to
    defer / the wizard.
    """
    if section_auto is ReconcileAuto.KEEP_LIVE:
        return reconcile_apply.AutoSide.OURS
    if section_auto is ReconcileAuto.USE_TRACKED:
        return reconcile_apply.AutoSide.THEIRS
    return None


def _resolve_plain_reconcile(
    profile: str,
    sub_name: str,
    sub_src: Path,
    sub_dst: Path,
    tracked_file: TrackedFile,
    *,
    interactive: bool,
    section_auto: ReconcileAuto | None,
) -> _PendingDeploy | None:
    """Resolve a PLAIN tracked file through the 3-way reconcile engine.

    Returns the pass-1 record, or ``None`` to fall back to the legacy
    verbatim path. The caller has already established eligibility (no
    disposition, no spans, no host-local overlay, regular file); this
    function adds the final gate — both live and tracked must be UTF-8, since
    the deploy write path and the conflict wizard are text-based (a binary
    plain file stays verbatim). The merged content overrides the verbatim
    scaffold's ``content``; the store ``record`` rides pass 2. ``section_auto``
    (``--auto``) resolves a conflict non-interactively by taking a side.
    """
    scaffold = deploy.resolve_deploy(sub_src, sub_dst, mode=tracked_file.mode)
    tracked_bytes = sub_src.read_bytes()
    live_bytes = scaffold.real_dst.read_bytes() if scaffold.dst_existed else None
    if not _is_utf8(tracked_bytes) or (
        live_bytes is not None and not _is_utf8(live_bytes)
    ):
        return None
    fid = reconcile.file_id(sub_name)
    # Claude-merge is offered (button visible) ONLY in the interactive wizard;
    # in a non-interactive / --auto / CI run it stays the unavailable stub so
    # it is never auto-invoked (D6). The factory returns a closure — claude is
    # spawned only if the user presses the button.
    claude_merge = (
        make_claude_merge_fn(display_path=str(sub_dst))
        if interactive
        else _claude_merge_unavailable
    )
    outcome = reconcile_apply.reconcile_plain_file(
        profile,
        fid,
        live=reconcile.ABSENT if live_bytes is None else live_bytes,
        tracked=tracked_bytes,
        interactive=interactive,
        auto=_auto_side(section_auto),
        display_path=str(sub_dst),
        claude_merge=claude_merge,
        # Only consulted on the interactive seed path; the non-interactive
        # seed keeps live without prompting.
        seed_prompt=_seed_prompt_interactive,
    )
    new_content = _plain_reconcile_content(outcome, live_bytes)
    if new_content is None:
        return None
    return _PendingDeploy(
        sub_name=sub_name,
        sub_src=sub_src,
        sub_dst=sub_dst,
        tracked_file=tracked_file,
        host_local=None,
        file_spans=[],
        resolved=replace(scaffold, content=new_content),
        reconcile=(fid, outcome),
    )


def _resolve_structured_reconcile(
    profile: str,
    sub_name: str,
    sub_src: Path,
    sub_dst: Path,
    tracked_file: TrackedFile,
    *,
    interactive: bool,
    section_auto: ReconcileAuto | None,
) -> _PendingDeploy | None:
    """Resolve a STRUCTURED (yaml/json/jsonc) tracked file via the key-aware engine.

    Sibling of :func:`_resolve_plain_reconcile` for a file whose ``dst`` is a
    structured format: it merges at key granularity
    (:func:`~setforge.reconcile_apply.reconcile_structured_file`), so an
    independent-key upstream change does not false-conflict with a host edit the
    way the line 3-way would. Returns ``None`` (the caller falls back to the line
    path, then to verbatim) when ``dst`` is not a structured format or either side
    is not UTF-8. Eligibility (no disposition/spans/host-local overlay) is the
    caller's; a genuine same-key collision is handled inside the engine (it
    delegates to the line wizard / ``--auto`` / DEFERRED).
    """
    fmt = structured_format(sub_dst)
    if fmt is None:
        return None
    scaffold = deploy.resolve_deploy(sub_src, sub_dst, mode=tracked_file.mode)
    tracked_bytes = sub_src.read_bytes()
    live_bytes = scaffold.real_dst.read_bytes() if scaffold.dst_existed else None
    if not _is_utf8(tracked_bytes) or (
        live_bytes is not None and not _is_utf8(live_bytes)
    ):
        return None
    fid = reconcile.file_id(sub_name)
    claude_merge = (
        make_claude_merge_fn(display_path=str(sub_dst))
        if interactive
        else _claude_merge_unavailable
    )
    outcome = reconcile_apply.reconcile_structured_file(
        profile,
        fid,
        live=reconcile.ABSENT if live_bytes is None else live_bytes,
        tracked=tracked_bytes,
        fmt=fmt,
        interactive=interactive,
        auto=_auto_side(section_auto),
        display_path=str(sub_dst),
        claude_merge=claude_merge,
        seed_prompt=_seed_prompt_interactive,
    )
    new_content = _plain_reconcile_content(outcome, live_bytes)
    if new_content is None:
        return None
    return _PendingDeploy(
        sub_name=sub_name,
        sub_src=sub_src,
        sub_dst=sub_dst,
        tracked_file=tracked_file,
        host_local=None,
        file_spans=[],
        resolved=replace(scaffold, content=new_content),
        reconcile=(fid, outcome),
    )


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
    loop: defer a symlink-declared tracked_file wholesale, otherwise route the
    file through the unified per-unit reconcile engine (structured, then plain).
    A binary / non-utf8 / deletion-edge file the reconcile engine cannot
    classify falls back to a verbatim tracked deploy.
    """
    if tracked_file.symlink is not None:
        # Symlink-deployed: deferred wholesale to pass 2
        # (resolved=None). The link lands at ``sub_dst`` and the
        # tracked content lands at
        # ``Path(tracked_file.symlink).expanduser()``. The stored base
        # lifecycle is regular-file-only — never wired here.
        return _PendingDeploy(
            sub_name=sub_name,
            sub_src=sub_src,
            sub_dst=sub_dst,
            tracked_file=tracked_file,
            host_local=host_local,
            file_spans=[],
            resolved=None,
        )
    # Every non-symlink file flows through the unified 3-way reconcile engine
    # (a local edit merges against the recorded base rather than being silently
    # overwritten). Structured (YAML/JSON/JSONC) first, then plain (line/
    # markdown); a binary / non-utf8 / deletion-edge file returns None from both
    # and falls back to the verbatim tracked deploy below. Host-local *section*
    # seeding/injection was retired with the disposition/spans overlay model, so
    # a file carrying host_local sections is reconciled like any other (its
    # existing live content is preserved by the 3-way; no section is injected).
    reconciled = _resolve_structured_reconcile(
        profile,
        sub_name,
        sub_src,
        sub_dst,
        tracked_file,
        interactive=conflict_resolver is not None,
        section_auto=section_auto,
    ) or _resolve_plain_reconcile(
        profile,
        sub_name,
        sub_src,
        sub_dst,
        tracked_file,
        interactive=conflict_resolver is not None,
        section_auto=section_auto,
    )
    if reconciled is not None:
        return reconciled
    # Binary / non-utf8 / deletion-edge: the reconcile engine cannot diff it,
    # so deploy the tracked bytes verbatim.
    resolved = deploy.resolve_deploy(
        sub_src,
        sub_dst,
        host_local_sections=host_local,
        mode=tracked_file.mode,
    )
    return _PendingDeploy(
        sub_name=sub_name,
        sub_src=sub_src,
        sub_dst=sub_dst,
        tracked_file=tracked_file,
        host_local=host_local,
        file_spans=[],
        resolved=resolved,
    )


@dataclass(slots=True, frozen=True)
class _PendingDeploy:
    """One pass-1 resolution awaiting its pass-2 write.

    ``resolved`` is the in-memory :class:`deploy.ResolvedDeploy` for a
    regular-file deploy, or ``None`` for a symlink-declared tracked_file
    (deferred wholesale — pass 2 runs :func:`deploy.deploy_symlinked_file`
    end to end).
    """

    sub_name: str
    sub_src: Path
    sub_dst: Path
    tracked_file: TrackedFile
    host_local: dict[HostLocalSectionName, HostLocalSection] | None
    file_spans: list[SpanEntry]
    resolved: deploy.ResolvedDeploy | None
    reconcile: tuple[FileId, reconcile_apply.ReconcileOutcome] | None = None


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
        # A reconcile file records its merge base in the base_store
        # (SnapshotStore.BASE) — snapshot it so revert restores the pre-install
        # base. Without this, a revert undoes the live file but leaves the base
        # advanced, and the next install would treat the reverted-away file as a
        # deliberate deletion (theirs == base) instead of re-deploying it.
        if record.reconcile is not None:
            # A reconcile file's install advances base AND local + index
            # (reconcile.record), so revert must restore all of them. BASE +
            # the local/drafts legs here; the per-profile INDEX once, below.
            entries.append(
                transitions.snapshot_store_state(
                    transitions.SnapshotStore.BASE, profile, record.sub_name
                )
            )
            entries.extend(
                transitions.reconcile_file_snapshots(profile, record.sub_name)
            )
    # The reconcile index is one doc per profile — snapshot it ONCE, LAST (so a
    # torn restore leaves a prunable orphan, never an index pointing at unwritten
    # local/draft bytes — parity with reconcile.record's index-last ordering).
    if any(record.reconcile is not None for record in pending):
        entries.append(
            transitions.snapshot_store_state(
                transitions.SnapshotStore.INDEX, profile, profile
            )
        )
    return tuple(entries)


@dataclass(slots=True, frozen=True)
class DeployOutcome:
    """The pass-2 deploy outputs the caller threads into the transition.

    ``state_snapshots`` is the pre-install store state captured at the
    pass-2 barrier (:func:`_capture_store_snapshots`). ``prior_modes`` maps
    each live path whose MODE this install changed to the permission bits it
    held BEFORE the install chmod-ed it (from
    :attr:`deploy.DeployResult.prior_mode`) — the data ``revert`` needs to
    chmod the path back in lockstep with the content patch reverse. A deploy
    that touched no mode (fresh CREATE, true NOOP, mode-already-matched
    UPDATE, or a symlink record) contributes nothing.

    ``deferred_reconcile`` lists the live paths of plain reconcile files whose
    conflict was DEFERRED (kept live, base not advanced) — the caller gates a
    non-zero exit on them for a non-interactive run, since the install left
    real conflicts unresolved.
    """

    state_snapshots: tuple[transitions.StateSnapshotEntry, ...]
    prior_modes: dict[Path, int]
    deferred_reconcile: tuple[Path, ...] = ()


def _execute_pending_deploys(
    profile: str,
    pending: list[_PendingDeploy],
) -> DeployOutcome:
    """Pass 2: replay the pass-1 records in order, performing every write.

    Snapshots the pre-install store state FIRST (one barrier, before any
    write — see :func:`_capture_store_snapshots`) and returns it together
    with the per-path pre-install mode map so the caller threads both into
    the install transition. Then, per
    record: apply the deferred base-migration writes (seed-first order +
    the one-time warning) → write the resolved content via
    :func:`deploy.write_resolved_deploy` (or run
    :func:`deploy.deploy_symlinked_file` for a symlink record) → echo the
    action → advance the disposition byte base → advance the spans sidecar,
    in lockstep per file. Each write whose :class:`deploy.DeployResult`
    reports a ``prior_mode`` (the live mode it overwrote) records that mode
    under the deployed path, keyed by the SAME path the transition snapshots
    — ``real_dst`` (symlink-resolved), so the revert chmod targets the file
    the patch reverse rewrites. After the loop, prune bases keyed on the
    executed disposition set — so a gate refusal (which never reaches this
    function) prunes nothing.
    """
    state_snapshots = _capture_store_snapshots(profile, pending)
    prior_modes: dict[Path, int] = {}
    deferred_reconcile: list[Path] = []
    # Files whose byte base must SURVIVE the end-of-run prune: disposition
    # files AND plain files routed through the reconcile engine (both persist
    # a base in the shared base_store, keyed by file_id). A plain file absent
    # from this set would have its reconcile base pruned every run.
    base_keep_ids: set[str] = set()
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
        result = deploy.write_resolved_deploy(record.resolved)
        if result.prior_mode is not None:
            # ``result.dst`` is the symlink-resolved real_dst — the SAME
            # path the transition snapshots and the patch reverse rewrites,
            # so revert's chmod target lines up with its content restore.
            prior_modes[result.dst] = result.prior_mode
        typer.echo(f"{result.action.value:>8}  {record.sub_dst}")
        _echo_host_local_sections_provenance(record.host_local)
        # ADVANCE the reconcile store only AFTER the live write (the same
        # lockstep, same safe-failure-direction reasoning as the disposition
        # byte base below): a base that lags live re-merges safely next run.
        if record.reconcile is not None:
            base_keep_ids.add(record.sub_name)
            _advance_reconcile_store(profile, record)
            if record.reconcile[1].kind is reconcile_apply.ReconcileKind.DEFERRED:
                deferred_reconcile.append(record.sub_dst)
    # PRUNE after the whole loop: bases whose file_id is not in this run's
    # keep-set (a file left the profile, lost its disposition, or stopped
    # being a reconcile-eligible plain file) are removed. The keep-set spans
    # disposition + plain-reconcile files, so an engine-routed plain file's
    # base survives instead of being pruned every run.
    base_store.prune(profile, base_keep_ids)
    return DeployOutcome(
        state_snapshots=state_snapshots,
        prior_modes=prior_modes,
        deferred_reconcile=tuple(deferred_reconcile),
    )


def _advance_reconcile_store(profile: str, record: _PendingDeploy) -> None:
    """Advance the reconcile store for a plain file AFTER its live write.

    A ``WRITE`` outcome ``record``s the new base (= tracked) + the deployed
    content (a :func:`reconcile.record` failure PROPAGATES — base lagging
    live is the safe failure direction). A ``DEFERRED`` outcome (a non-TTY /
    ``--auto`` conflict, or a skipped region) keeps live, does NOT
    re-baseline, and WARNs so the divergence re-surfaces on the next
    interactive install. ``NOOP`` / ``CANCELLED`` leave the store untouched.
    """
    assert record.reconcile is not None
    fid, outcome = record.reconcile
    if outcome.kind is reconcile_apply.ReconcileKind.WRITE:
        assert isinstance(outcome.content, bytes)
        assert outcome.new_base is not None
        reconcile.record(profile, fid, base=outcome.new_base, local=outcome.content)
        if outcome.seeded:
            typer.secho(
                f"warning: {record.sub_dst}: kept your local file and seeded "
                f"the merge base from upstream — re-run interactively to adopt "
                f"the upstream version",
                err=True,
                fg=typer.colors.YELLOW,
            )
    elif outcome.kind is reconcile_apply.ReconcileKind.DEFERRED:
        typer.secho(
            f"warning: {record.sub_dst}: merge conflict kept live, base not "
            f"advanced — conflict re-surfaces next install (re-run "
            f"interactively to resolve)",
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
    mcp_delta: transitions.MCPDelta | None = None,
    file_modes: Mapping[Path, int] | None = None,
) -> Path:
    """Write the install transition record; return the target directory path.

    ``state_snapshots`` carries the pre-install store state captured at
    the pass-2 barrier (:func:`_capture_store_snapshots`); ``revert``
    restores those entries in lockstep with the ``changes.patch`` reverse.

    ``file_modes`` is the per-path pre-install permission map (paths whose
    MODE this install changed); ``revert`` chmods each reverted path back to
    its recorded value in lockstep with the patch reverse, so a mode-only
    install (empty content patch) is still a faithful inverse.

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
        mcp_delta=mcp_delta,
        file_modes=file_modes,
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


def _run_predeploy_gates(
    *,
    drift_report: compare_mod.CompareReport,
    ctx: ProfileContext,
    auto_accept_tracked: bool,
    auto_accept_live: bool,
    yes: bool,
) -> None:
    """Run the pre-deploy confirm/reject gates in their fixed order.

    Bundles the duplicate-section-name refusal + the unexpected-drift
    confirm (``--auto-accept-{tracked,live}``) + the bare-install
    unexpected-drift reject into one orchestrator so :func:`install`
    reads as a high-level pipeline rather than three nearly-identical
    confirm shells. Each gate is independent and short-circuits when its
    triggering flag is unset; the order matches the pre-extraction body
    verbatim so flag interactions stay unchanged.

    A duplicate user-section name is structural corruption (NOT a
    migratable legacy-marker artifact, so install does not silently fix it
    the way it migrates pre-hash markers): refuse it FIRST, before any
    other gate, so a duplicate-bearing tracked/live file aborts the install
    with the actionable rename message rather than collapsing a section
    body during deploy.
    """
    _refuse_duplicate_section_names(ctx, command="install")
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
#   ``transitions.write_transition`` / ``secrets_mod.append_to_allowlist``
#   are all unreachable).
# - No new ``_simulate_*`` / ``_dry_*`` diff-or-merge function: every
#   compute step here delegates to the same shared helpers the real
#   pipeline uses, so a future change to the diff algorithm reflects
#   in dry-run output automatically.
# - WOULD only on mutating verbs (``deploy`` / ``inject`` / ``install`` /
#   ``uninstall`` / ``enable`` / ``disable``); section headers and read
#   counts go unprefixed.
# - No ``confirm_auto_operation`` call from the dry-run path: the call
#   site in :func:`_confirm_legacy_drift_or_exit` is inside
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
) -> None:
    """Simulate every install phase without mutating filesystem or state.

    Called from :func:`setforge.cli.install.install` when ``--dry-run``
    is set. Walks the install phases the real pipeline performs (profile
    resolve, host overlay, drift gate, file deploys, plugin reconcile,
    extension reconcile, transition record) and prints a ``WOULD
    ``-prefixed action line per mutating verb. Calls only read-only
    helpers; never writes files, never touches the transition state dir,
    never invokes the auto-confirm confirm wizard, never runs git fetch
    (the source-layer git check runs BEFORE this function but is itself
    read-only).
    """
    typer.echo(_DRY_RUN_HEADER)
    _dry_run_emit_profile_summary(ctx)
    # Thread the validated host_local_sections overlay into the drift
    # compare so a live file that already received its injected host-local
    # sections does NOT surface as spurious content drift — matching what
    # ``setforge compare`` reports (compare.py threads the same overlay).
    # Without this the dry-run report AND the fresh-host welcome preview
    # (which both route through this function) over-report drift on any
    # tracked_file using legacy host_local_sections marker injection.
    # Loaded here rather than carried on ``ctx`` so this read-only path
    # stays self-contained, mirroring ``_dry_run_emit_host_local_inject``.
    host_local_sections_map = _load_validated_host_local_sections(
        ctx.cfg, ctx.resolved, ctx.repo_root
    )
    drift_report = compare_mod.compare_profile(
        ctx.cfg,
        ctx.profile,
        ctx.repo_root,
        host_local_sections=host_local_sections_map,
    )
    _dry_run_emit_drift_gate(drift_report)
    _dry_run_emit_deploys(ctx, drift_report)
    _dry_run_emit_host_local_inject(ctx)
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
    typer.echo(f"  mcp_servers:    {len(ctx.resolved.mcp_servers)}")
    typer.echo(f"  cargo_binaries: {len(ctx.resolved.cargo_binaries)}")
    typer.echo(f"  bootstrap:      {len(ctx.resolved.bootstrap)}")
    typer.echo("  host overlay:   none (host-local layer not yet enabled)")


def _dry_run_emit_drift_gate(
    drift_report: compare_mod.CompareReport,
) -> None:
    """Emit the ``=== would-be drift gate ===`` block.

    The drift gate is a READ in the real pipeline too (it computes
    unexpected drift over the existing live tree) — counts stay
    unprefixed. The count reports files whose drift is CLASSIFIED
    unexpected or conflicted (the compare-level
    :class:`~setforge.compare.DriftClass`); the live install gate
    (:func:`_check_unexpected_drift`) trips only on permission-mode
    drift (``mode_drift``), so this count can include diff-only drift
    that a real install does not reject. The dry-run path never invokes
    the auto-confirm wizard (short-circuiting before the confirm is a
    hard requirement per spec).
    """
    typer.echo("=== would-be drift gate ===")
    unexpected = sum(
        1
        for e in drift_report.entries
        if e.drift_class in (DriftClass.UNEXPECTED, DriftClass.CONFLICTED)
    )
    typer.echo(f"unexpected drift in {unexpected} file(s)")


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
    a ``HOST_LOCAL_PROVENANCE_TAG`` so users can identify host-local
    injections in the dry-run output. No-op when local.yaml is absent or
    declares no host-local sections for tracked_files in this profile.
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
