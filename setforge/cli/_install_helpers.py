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
from datetime import UTC
from pathlib import Path
from typing import Final, assert_never

import typer

from setforge import (
    base_store,
    deploy,
    disposition_merge,
    scalar_base_store,
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
from setforge.compare import (
    CompareStatus,
    expand_tracked_file,
    resolve_dst,
    resolve_src,
)
from setforge.config import (
    Config,
    Disposition,
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
    for tracked_file, _sub_name, _src, _dst in _iter_all_tracked_files(ctx):
        saw_tracked_file = True
        if tracked_file.preserve_user_keys or tracked_file.preserve_user_keys_deep:
            return True
    if not saw_tracked_file:
        return None
    return False


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
    before threading the overlay through ``deploy.copy_atomic`` /
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


def _span_lockstep_paths(ctx: ProfileContext) -> list[Path]:
    """Return the spans sidecar + byte-base paths to snapshot for revert.

    For every span-bearing disposition tracked (sub-)file in the resolved
    profile, returns both its spans sidecar manifest path and its stored
    byte-base path so the install transition snapshots all three (live +
    base + sidecar) and ``patch -R`` reverts them in lockstep (Invariant
    I5). Scoped to span-bearing files so non-span disposition files keep
    today's re-seed-on-next-install base behaviour unchanged.
    """
    paths: list[Path] = []
    for tracked_file, sub_name, _, _ in _iter_all_tracked_files(ctx):
        if tracked_file.disposition is not None and tracked_file.spans:
            paths.append(spans_store.manifest_path(ctx.profile, sub_name))
            paths.append(base_store.base_path(ctx.profile, sub_name))
    return paths


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
        e.status == CompareStatus.DRIFTED and (e.unexpected_drift_keys or e.mode_drift)
        for e in drift_report.entries
    )
    if not has_real_unexpected:
        return

    unexpected_count = sum(
        1
        for e in drift_report.entries
        if e.status == CompareStatus.DRIFTED
        and (e.unexpected_drift_keys or e.mode_drift)
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
) -> None:
    """Deploy each tracked_file via :func:`deploy.copy_atomic` + stamp baselines.

    Echoes the per-file ``copy_atomic`` action to stdout (preserving the
    pre-refactor format) and stamps tracked-side embedded section hashes
    after each ``preserve_user_sections`` deploy so the three-way
    classifier has a baseline on the next install.

    For every regular-file sub-entry whose ``tracked_file.disposition`` is
    set, the per-host stored base (:mod:`setforge.base_store`) is woven into
    the deploy: the base is READ before the deploy (the merge ancestor for
    :func:`deploy.copy_atomic`'s 3-way driver), then — and ONLY after the
    live write returns successfully — ADVANCED to the merged bytes when the
    driver signals re-baselining. The ordering is load-bearing: a base that
    lags live is the safe failure direction (the next install re-merges
    against a stale-but-valid ancestor); a base written before or around
    the live write could end up ahead of live, which is corruption. A
    deferred conflict (``merge_conflicts`` non-empty, ``new_base is None``)
    keeps live and warns; its base stays put so the divergence re-surfaces
    next install. After the loop, every base under the profile whose
    file_id is NOT in this run's disposition keep-set is pruned, so bases
    for files that left the profile (or dropped their disposition) are
    cleaned up. The symlink branch never touches the base store.

    A SEPARATE, mutually-exclusive lifecycle covers the SHALLOW
    ``preserve_user_keys`` scalar overlay (:mod:`setforge.scalar_base_store`)
    for every regular-file sub-entry whose ``disposition is None`` and whose
    ``preserve_user_keys`` is non-empty. It mirrors the byte-base ordering:
    READ each shallow path's stored scalar base BEFORE the deploy (the
    ancestor :func:`deploy.copy_atomic`'s scalar driver resolves against),
    then — and ONLY after the live write returns cleanly — ADVANCE the bases
    the driver signalled via ``result.new_scalar_bases`` in ONE batched
    :func:`scalar_base_store.set_bases` call (deferred bare conflicts are
    already omitted from that map). A deferred scalar conflict
    (``result.scalar_conflicts`` non-empty with the path absent from
    ``new_scalar_bases``) keeps live and warns. After the per-file deploy,
    :func:`scalar_base_store.prune` drops any stored path no longer in the
    file's live shallow set. The two lifecycles never overlap on one file:
    ``disposition`` and ``preserve_user_keys`` are mutually exclusive in
    config.

    ``file_id`` is the ``expand_tracked_file`` synthetic ``sub_name``
    (``name`` for plain files, ``name/relpath`` for directory entries) —
    the same stable per-profile identifier the prune keep-set and
    transitions use. ``sub_name`` is always a relative path with no ``..``
    component (``name`` is a config key; ``relpath`` is taken
    ``relative_to`` the src dir), so it satisfies ``base_store``'s
    traversal guard (:func:`setforge.base_store._resolve_target`).

    ``conflict_resolver`` is the OPTIONAL interactive disposition conflict
    resolver (built by :func:`_build_conflict_resolver`), threaded into every
    disposition :func:`deploy.copy_atomic` call. ``None`` (non-interactive /
    non-tty / ``--auto``) leaves the bare warn-and-defer behavior unchanged.
    """
    profile = ctx.profile
    disposition_file_ids: set[str] = set()
    for name in ctx.resolved.tracked_files:
        tracked_file = ctx.cfg.tracked_files[name]
        host_local = host_local_sections_map.get(name) or None
        src = resolve_src(tracked_file, ctx.repo_root)
        dst = resolve_dst(tracked_file)
        for sub_name, sub_src, sub_dst in expand_tracked_file(name, src, dst):
            if tracked_file.symlink is not None:
                # Symlink-deployed: the link lands at ``sub_dst`` and
                # the tracked content lands at
                # ``Path(tracked_file.symlink).expanduser()``.
                # preserve_user_{sections,keys} still composes;
                # deploy.deploy_symlinked_file routes through the same
                # _compute_content path as copy_atomic. The stored base
                # lifecycle is regular-file-only — never wired here.
                result = deploy.deploy_symlinked_file(
                    sub_src, sub_dst, tracked_file, host_local_sections=host_local
                )
                typer.echo(
                    f"{result.action.value:>8}  {sub_dst} -> {tracked_file.symlink}"
                )
                _echo_preserve_user_keys_provenance(tracked_file)
                _echo_host_local_sections_provenance(host_local)
                if tracked_file.preserve_user_sections:
                    section_reconcile.stamp_tracked_baseline(sub_src)
                continue
            override = section_decisions.get(sub_dst)
            precomputed = live_sections_map.get(sub_dst)
            # Stored-base 3-way path is gated on a declared disposition.
            # READ the base BEFORE the deploy: it is the merge ancestor
            # copy_atomic's driver diffs live/tracked against.
            base_text: str | None = None
            if tracked_file.disposition is not None:
                disposition_file_ids.add(sub_name)
                base_text = _read_or_migrate_disposition_base(
                    profile, sub_name, sub_dst
                )
            # Span re-overlay path: READ the spans sidecar BEFORE the deploy
            # so the relocation ladder has its derived state. Spans ride the
            # disposition path (the 3-way merge is where the re-overlay +
            # re-baseline happen).
            file_spans = (
                tracked_file.spans if tracked_file.disposition is not None else []
            )
            span_states = (
                spans_store.get_states(profile, sub_name) if file_spans else {}
            )
            # Scalar-base path (mutually exclusive with disposition): READ the
            # stored base for every shallow preserve path BEFORE the deploy.
            shallow_preserve_paths = tracked_file.preserve_user_keys
            scalar_bases = _read_scalar_bases(
                profile, sub_name, tracked_file.disposition, shallow_preserve_paths
            )
            result = deploy.copy_atomic(
                sub_src,
                sub_dst,
                preserve_user_sections=tracked_file.preserve_user_sections,
                preserve_user_keys=tracked_file.preserve_user_keys or None,
                preserve_user_keys_deep=tracked_file.preserve_user_keys_deep or None,
                section_bodies_override=override,
                precomputed_live_sections=precomputed,
                host_local_sections=host_local,
                mode=tracked_file.mode,
                disposition=tracked_file.disposition,
                base_text=base_text,
                merge_auto=section_auto,
                scalar_bases=scalar_bases,
                conflict_resolver=conflict_resolver,
                spans=file_spans or None,
                span_states=span_states or None,
            )
            typer.echo(f"{result.action.value:>8}  {sub_dst}")
            _echo_preserve_user_keys_provenance(tracked_file)
            _echo_host_local_sections_provenance(host_local)
            if tracked_file.preserve_user_sections:
                section_reconcile.stamp_tracked_baseline(sub_src)
            # ADVANCE the disposition base only AFTER the live write.
            if tracked_file.disposition is not None:
                _advance_disposition_base(profile, sub_name, sub_dst, result)
            # ADVANCE the spans sidecar + warn on orphans AFTER the live
            # write, in lockstep with the byte base.
            if file_spans:
                _advance_span_states(
                    profile,
                    sub_name,
                    sub_dst,
                    result,
                    file_spans,
                    strict_spans=strict_spans,
                )
            # SCALAR-base lifecycle (mutually exclusive with disposition):
            # advance / prune / warn, ordered AFTER the live write.
            if scalar_bases is not None:
                _advance_scalar_bases(
                    profile, sub_name, sub_dst, result, shallow_preserve_paths
                )
    # PRUNE after the whole loop: bases whose file_id is not in this run's
    # disposition keep-set (file left the profile, or lost its disposition)
    # are removed. Non-disposition files never have a base, so an empty
    # keep-set still correctly clears any stale bases under the profile.
    base_store.prune(profile, disposition_file_ids)


def _read_or_migrate_disposition_base(
    profile: str,
    file_id: str,
    sub_dst: Path,
) -> str | None:
    """Return the disposition merge-ancestor base text, migrating if needed.

    Reads the stored base for ``file_id`` under ``profile``. When a base
    already exists it is returned verbatim (steady state). When NO base
    exists (a file entering the disposition world for the first time) the
    SHARED-section strip + base-seed migration runs via
    :func:`_migrate_shared_markers_for_base`: if the live file still carries
    legacy SHARED markers they are stripped in place and the base is seeded
    from the stripped bytes, and those stripped bytes are returned so the
    first 3-way merge has base == live (no spurious delta, zero data loss).
    Returns ``None`` — the ordinary base-absent (deploy-tracked-verbatim)
    path — when no base exists and the live file has no SHARED markers.
    """
    raw = base_store.read_base(profile, file_id)
    if raw is not None:
        return raw.decode("utf-8")
    return _migrate_shared_markers_for_base(profile, file_id, sub_dst)


def _migrate_shared_markers_for_base(
    profile: str,
    file_id: str,
    sub_dst: Path,
) -> str | None:
    """Strip live SHARED markers in place + seed base; return the seeded base text.

    The EXPAND half of the section→disposition migration, called
    only when ``sub_dst``'s stored base is ABSENT (a file entering the
    disposition world for the first time). When the live file exists AND
    still carries legacy ``shared`` user-section markers, this:

    1. Computes the stripped-live bytes IN MEMORY via
       :func:`setforge.sections.strip_shared_markers` (which parses the WHOLE
       file via the marker state machine first, so a malformed file raises
       :class:`~setforge.errors.MarkerError` BEFORE any write — no partial
       output, no half-migrated file).
    2. Rewrites the live file to those exact stripped bytes, preserving the
       file's EXISTING mode (0600 stays 0600) via the
       fchmod-before-replace pattern (no symlink-follow, no mode widening).
    3. Seeds the stored base to the SAME in-memory stripped bytes.

    Returns the stripped text so the caller threads it as ``base_text`` into
    :func:`deploy.copy_atomic`: base == stripped-live == what now sits live,
    so the first 3-way merge has zero spurious delta (the data-loss
    invariant). Returns ``None`` — leaving the caller's ``base_text`` at
    ``None`` for the ordinary base-absent (deploy-tracked-verbatim) path —
    when the live file is absent OR carries no SHARED markers. Host-local
    markers and tracked-side markers are NOT touched here.

    The gate is strict on the ``(base absent, shared markers present)`` pair
    (base-absence is the caller's precondition; shared-marker presence is
    ``stripped != live_text``), so a second install — where the base now
    exists OR the markers are already stripped — never re-seeds or
    double-strips.
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
    # Rewrite live to the stripped bytes, preserving the EXISTING live mode.
    existing_mode = stat.S_IMODE(sub_dst.stat().st_mode)
    _atomic_rewrite_preserving_mode(sub_dst, stripped, existing_mode)
    # Seed the base from a post-rewrite read_text — the EXACT text copy_atomic
    # re-reads as `ours`. read_text applies universal-newline translation
    # (CRLF -> LF), so seeding from the in-memory `stripped` (which keeps CRLF)
    # would leave base != ours on every line of a CRLF file and manufacture a
    # spurious first-merge delta. Re-reading the file we just wrote keeps
    # base == ours == landed-live. (The post-deploy advance re-seeds the
    # durable base; this write is the merge ancestor + a crash mitigation so a
    # kill before the advance still finds a seeded base, not base-absent.)
    seeded = sub_dst.read_text(encoding="utf-8")
    base_store.write_base(profile, file_id, seeded.encode("utf-8"))
    return seeded


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
            f"--auto=use-tracked or the merge wizard)",
            err=True,
            fg=typer.colors.YELLOW,
        )


def _advance_span_states(
    profile: str,
    file_id: str,
    sub_dst: Path,
    result: deploy.DeployResult,
    file_spans: list[SpanEntry],
    *,
    strict_spans: bool,
) -> None:
    """Advance the spans sidecar, prune left spans, and warn on orphans.

    Writes every recomputed :class:`~setforge.spans_store.SpanState` from
    the deploy AND prunes any anchor no longer in the file's intent — both
    in LOCKSTEP with the byte base just advanced (Invariant I5). Each
    orphan emits a loud per-span warning; an orphan NEVER aborts the
    default install (Invariant I6). When ``strict_spans`` is set a PINNED
    orphan escalates to a refuse-install :class:`SetforgeError`.
    """
    if result.new_span_states is not None:
        spans_store.set_states(profile, file_id, result.new_span_states)
    spans_store.prune(profile, file_id, {span.anchor for span in file_spans})
    pinned_orphans: list[str] = []
    for orphan in result.span_orphans:
        typer.secho(
            f"warning: {sub_dst}: span {orphan.anchor!r} ({orphan.kind.value}) "
            f"could not be relocated upstream — region preserved, not dropped",
            err=True,
            fg=typer.colors.YELLOW,
        )
        if orphan.kind is SpanKind.PINNED:
            pinned_orphans.append(orphan.anchor)
    if strict_spans and pinned_orphans:
        joined = ", ".join(repr(a) for a in pinned_orphans)
        raise SetforgeError(
            f"{sub_dst}: --strict-spans: pinned span(s) {joined} orphaned "
            "(anchor gone upstream); refusing install"
        )


def _read_scalar_bases(
    profile: str,
    file_id: str,
    disposition: Disposition | None,
    shallow_preserve_paths: list[str],
) -> dict[str, object] | None:
    """Read the stored scalar base for every shallow preserve path, or None.

    Returns ``None`` (the scalar overlay is off for this file) when a
    ``disposition`` is declared — the two models are mutually exclusive — or
    when the file declares no shallow ``preserve_user_keys``. Otherwise maps
    each shallow path to its stored base (a typed scalar, ``None`` for a
    stored ``null``, or :data:`setforge.scalar_merge.ABSENT` for no base).
    The whole shallow list is passed to the driver, which skips any
    non-scalar path; reading the base for all of them is harmless.
    """
    if disposition is not None or not shallow_preserve_paths:
        return None
    return {
        path: scalar_base_store.get_base(profile, file_id, path)
        for path in shallow_preserve_paths
    }


def _advance_scalar_bases(
    profile: str,
    file_id: str,
    sub_dst: Path,
    result: deploy.DeployResult,
    shallow_preserve_paths: list[str],
) -> None:
    """Advance / prune / warn the scalar bases AFTER a clean live write.

    ADVANCE every path the driver signalled (``result.new_scalar_bases``,
    deferred bare conflicts already omitted) in ONE batched
    :func:`scalar_base_store.set_bases`; a failure PROPAGATES (base lagging
    live is the safe failure direction). PRUNE any stored path no longer in
    the file's live shallow set. WARN on each deferred scalar conflict (a
    path in ``scalar_conflicts`` but absent from ``new_scalar_bases``) so the
    user knows the divergence re-surfaces next install.
    """
    if result.new_scalar_bases:
        scalar_base_store.set_bases(profile, file_id, result.new_scalar_bases)
    advanced_paths = result.new_scalar_bases or {}
    deferred_scalar = [
        path for path in result.scalar_conflicts if path not in advanced_paths
    ]
    if deferred_scalar:
        joined = ", ".join(deferred_scalar)
        typer.secho(
            f"warning: {sub_dst}: scalar conflict kept live for {joined}, "
            f"base not advanced — conflict re-surfaces next install "
            f"(re-run with --auto=use-tracked or the merge wizard)",
            err=True,
            fg=typer.colors.YELLOW,
        )
    scalar_base_store.prune(profile, file_id, set(shallow_preserve_paths))


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


def _echo_preserve_user_keys_provenance(tracked_file: TrackedFile) -> None:
    """Emit mockup-B install-output provenance lines for a tracked_file.

    Suppressed when no resolved key originated in local.yaml (i.e.
    today's setforge.yaml-only behavior). Otherwise prints one
    ``preserved keys (N effective):`` header + one ``• key  [tag]``
    line per FROM_PROFILE / FROM_LOCAL_YAML entry, plus a ``✗ key
    [removed via local.yaml — overwritten with tracked value]`` line
    for each REMOVED_VIA_LOCAL entry (the auditable
    would-have-been-preserved row mockup B requires).
    """
    from setforge.preserved_keys import KeyOrigin, display_tag, has_local_yaml_overlay

    resolved_list = tracked_file.preserve_user_keys_resolved
    if not has_local_yaml_overlay(resolved_list):
        return
    effective = [k for k in resolved_list if k.origin != KeyOrigin.REMOVED_VIA_LOCAL]
    typer.echo(f"    preserved keys ({len(effective)} effective):")
    for key in resolved_list:
        if key.origin == KeyOrigin.REMOVED_VIA_LOCAL:
            typer.echo(
                f"      ✗ {key.key}  "
                f"[removed via local.yaml — overwritten with tracked value]"
            )
        else:
            typer.echo(f"      • {key.key}  {display_tag(key)}")


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
    (when set and pointing at a git repo,
    :func:`transitions.make_meta` records HEAD's sha so ``setforge
    status`` can compute commits-since-last-install) and
    ``reconcile_outcomes`` (defaults to empty so
    pre-bump callers keep working; when non-empty, serialized to
    ``reconcile_outcomes.json`` alongside ``extensions.json`` /
    ``plugins.json`` so ``install --retry-failed`` can rebuild the
    skipped-ids set on the next invocation).

    ``preserve_user_keys_applied`` is the schema-bump
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
    unprefixed. When unexpected drift IS present, surface the count so
    users can see what the real install would gate on, but do NOT
    invoke the auto-confirm confirm wizard (the dry-run path is the preview;
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
