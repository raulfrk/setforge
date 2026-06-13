"""install subcommand — orchestrates tracked-file deploy + extension/plugin reconcile.

Wires section-marker reconcile, deploy.copy_atomic, extension/plugin
reconcile, and the transition snapshot. Imports ``app`` from
:mod:`setforge.cli` so the ``@app.command()`` registration fires at
module import time; ``setforge/cli/__init__.py`` imports this module at
the bottom for the side effect.
"""

from pathlib import Path
from typing import assert_never

import typer

from setforge import (
    cargo as cargo_mod,
)
from setforge import (
    compare as compare_mod,
)
from setforge import (
    deploy,
    transitions,
)
from setforge import secrets as secrets_mod
from setforge import section_templates as section_templates_mod
from setforge import source as source_mod
from setforge.cli import (
    _CONFIG_OPTION,
    _PROFILE_OPTION,
    _resolve_config_arg,
    app,
)
from setforge.cli._git_check import (
    resolve_source_for_git_check,
    run_git_check_or_raise,
)
from setforge.cli._help_examples import INSTALL_EXAMPLES
from setforge.cli._helpers import (
    ProfileContext,
    _extract_live_sections_map,
    _iter_all_tracked_files,
    _parse_section_auto,
    _resolve_section_decisions,
)
from setforge.cli._install_helpers import (
    _build_conflict_resolver,
    _deploy_all_tracked_files,
    _dry_run_pipeline,
    _load_validated_host_local_sections,
    _reconcile_shared_spans,
    _run_predeploy_gates,
    _validate_span_file_types,
    _write_install_transition,
    migrate_local_overlay_spans_on_install,
    seed_overlay_migration_snapshot,
)
from setforge.cli._mcp_helpers import reconcile_mcp_servers
from setforge.cli._plugin_helpers import (
    _emit_reconcile_summary,
    _reconcile_extensions,
    _reconcile_plugins,
)
from setforge.cli._secrets_confirm import prompt_secret_action
from setforge.cli._welcome import (
    WelcomeChoice,
    build_welcome_inventory,
    is_fresh_host,
    prompt_welcome,
    reject_auto_on_fresh_host,
)
from setforge.config import (
    Config,
    ResolvedProfile,
    apply_host_local_tracked_file_overrides,
    apply_local_overlay,
    load_config,
    resolve_profile,
)
from setforge.locking import profile_lock
from setforge.secrets import SecretAction, SecretFinding, SecretsScanResult
from setforge.transitions import (
    ReconcileStatus,
    load_latest,
    load_reconcile_outcomes,
)


@app.command(epilog=INSTALL_EXAMPLES)
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
        help=(
            "Resolve permission-mode drift non-interactively by reapplying "
            "the tracked mode."
        ),
    ),
    auto_accept_live: bool = typer.Option(
        False,
        "--auto-accept-live",
        help=(
            "Proceed past permission-mode drift non-interactively; install "
            "still reapplies the tracked mode (live permission bits are not kept)."
        ),
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
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the --auto* confirmation prompt (for non-interactive use).",
    ),
    no_secrets_scan: bool = typer.Option(
        False,
        "--no-secrets-scan",
        help="Skip pre-deploy secrets scan (gitleaks) for automation.",
    ),
    retry_failed: bool = typer.Option(
        False,
        "--retry-failed",
        help=(
            "Re-attempt only the items skipped during the previous install's "
            "reconcile (per the prior transition's reconcile_outcomes). "
            "Other reconcile work is suppressed for this run."
        ),
    ),
    no_git_check: bool = typer.Option(
        False,
        "--no-git-check",
        help=(
            "Skip the pre-deploy git-status check on the config source. "
            "Intended for CI / cron — bypasses the dirty-tree / "
            "cache-lag warning on path / git sources respectively."
        ),
    ),
    strict_spans: bool = typer.Option(
        False,
        "--strict-spans",
        help=(
            "Escalate an orphaned PINNED span (its anchor went missing "
            "upstream) from a warning to a refuse-install. The refusal "
            "fires before any tracked file is written — no tracked file "
            "deploys, no transition lands (bootstrap stubs are created "
            "earlier in the pipeline). Forked-span and non-strict orphans "
            "always warn and continue."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Simulate every install phase without mutating the filesystem, "
            "transition state, or extension/plugin reconcilers. Output is "
            "WOULD-prefixed for mutating verbs; the final line is "
            "'=== rerun without --dry-run to apply for real ==='."
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
    # Seed-once host-local section templates: PLAN now (pure, in-memory),
    # COMMIT later under the lock AFTER consent. The plan reads the
    # host's current overlay + the template bodies but writes NOTHING; the
    # injected_seed window (below) makes the planned bodies visible to the
    # overlay readers AND the fresh-host welcome preview — exactly as if the
    # block were already committed — while the disk write waits behind the
    # welcome / git-check gates. Empty plan on --dry-run (the read-only path
    # must not seed) so both the injection and the under-lock COMMIT no-op.
    seed_plan = (
        [] if dry_run else _plan_section_templates_for_install(cfg, resolved, repo_root)
    )
    # Pre-consent seed-injection window. The planned bodies are visible to
    # the three overlay readers below AND the fresh-host welcome preview
    # (all funnel through source._load_local_source_config), with NO disk
    # write. The window closes — clearing the module state — before the
    # git-check + lock, so a welcome decline or a git abort leaves local.yaml
    # untouched. On --dry-run seed_plan is empty, so the context
    # manager is a no-op.
    with source_mod.injected_seed(seed_plan):
        # Resolve host-local↔shared span intent collisions BEFORE the overlay
        # fold so the chosen winner per collided anchor flows into the fold.
        # Bare install stays silent host-local-wins; --auto routes the
        # adopt-shared / keep-host-local decision; a non-tty
        # --reconcile-user-sections raises rather than burying the collision.
        # Returns the (tf_id, anchor) pairs whose SHARED span should
        # win the fold.
        prefer_shared_anchors = _reconcile_shared_spans(
            cfg,
            profile=profile,
            reconcile_user_sections=reconcile_user_sections,
            section_auto=section_auto,
        )
        # Apply local.yaml host-local mode/dst/symlink_target overlay
        # — also AFTER profile resolution. Rebuilds each TrackedFile with the
        # overlay-fields overrides applied so downstream resolve_dst / deploy /
        # deploy_symlinked_file consume the override transparently.
        apply_host_local_tracked_file_overrides(
            cfg, prefer_shared_anchors=prefer_shared_anchors
        )
        # Load + validate the local.yaml host_local_sections overlay
        # (host-local). Validation is file-type only at this layer: anchors /
        # bodies are resolved during deploy._compute_content. Empty mapping
        # when local.yaml is absent or declares no host-local sections.
        host_local_sections_map = _load_validated_host_local_sections(
            cfg, resolved, repo_root
        )
        # Reject spans on non-markdown tracked_files BEFORE any file is
        # written (the host-local overlay has already folded host-local spans
        # into each TrackedFile.spans above).
        _validate_span_file_types(cfg, resolved, repo_root)
        # Apply local.yaml plugin/extension/marketplace overlay (SPEC 2)
        # — also AFTER profile resolution. Mutates resolved
        # and cfg in place so the existing reconcile path consumes the
        # merged sets transparently. Raises LocalOverlayError (a
        # ConfigError) on collision / unknown-remove, surfaced via the
        # standard SetforgeError handler. The cross-ref check fires
        # defensively here even when validate ran first (Q8).
        apply_local_overlay(cfg, resolved, profile)
        ctx = ProfileContext(
            cfg=cfg, resolved=resolved, repo_root=repo_root, profile=profile
        )

        # Fresh-host welcome gate. Fires BEFORE every other
        # phase (git-check, dry-run dispatch, state-dir probe, bootstrap,
        # deploy) so a brand-new host can preview what the install will do
        # and consent before any mutation OR diagnostic that depends on a
        # specific source-tree state (the git-check on a dirty fresh-host
        # source would otherwise raise before the user ever sees the
        # welcome). ``--yes`` skips the welcome (the caller has consented
        # out-of-band); ``--auto=*`` is rejected on a fresh host because
        # there is no drift yet for the auto-resolver to act on.
        fresh = is_fresh_host()
        if fresh and not dry_run:
            reject_auto_on_fresh_host(auto=auto)
            inventory = build_welcome_inventory(ctx)

            def _welcome_dry_run() -> None:
                _dry_run_pipeline(ctx=ctx, section_auto=section_auto)

            welcome_choice = prompt_welcome(
                inventory=inventory,
                yes=yes,
                run_dry_run=_welcome_dry_run,
            )
            if welcome_choice is not WelcomeChoice.PROCEED:
                return

    # Pre-deploy git-status check. Fires BEFORE the drift
    # gate so a dirty / stale source is surfaced before any other slow
    # work (compare, secrets-scan, deploy). When the source-layer is
    # configured (--source / SETFORGE_SOURCE / local.yaml), use it so a
    # git-source's CACHE dir is inspected for staleness; otherwise fall
    # back to ``repo_root`` (the dir holding the resolved setforge.yaml)
    # which is the right answer for the legacy explicit-``--config``
    # invocations the test suite relies on.
    run_git_check_or_raise(
        source=resolve_source_for_git_check(repo_root),
        no_git_check=no_git_check,
    )

    # Boundary-not-leaf dispatch. When `--dry-run` is set,
    # route through `_dry_run_pipeline` which calls only the read-only
    # shared helpers (compare_profile, _extract_live_sections_map,
    # _resolve_section_decisions, vscode_extensions.reconcile(dry_run=True),
    # claude_plugins.reconcile(dry_run=True)). The real pipeline below is
    # provably unreachable: zero mutating subprocess calls, zero file
    # writes, zero transition record. The boolean is NOT threaded into
    # deploy / transitions / compare / merge — those modules stay
    # leaf-pure and the dry-run path bypasses them entirely.
    if dry_run:
        _dry_run_pipeline(ctx=ctx, section_auto=section_auto)
        return

    with profile_lock(profile):
        if not no_transition:
            transitions.ensure_state_dir_writable()
        # COMMIT the seed-once plan now — UNDER the lock, AFTER the welcome /
        # git-check consent gates. Captures local.yaml's pre-seed
        # bytes FIRST so the install transition's file_pre baseline is the
        # genuine unseeded content (recorded below), making revert restore an
        # unseeded local.yaml. Runs before the overlay-span migration so the
        # just-written legacy block is folded into a unified span exactly as
        # a pre-existing block would be.
        seeded, local_pre_seed = _commit_seed_under_lock(seed_plan)
        # Transparent, idempotent local.yaml rewrite: retire any legacy
        # host_local_sections block into unified `spans` OVERLAY entries so
        # the on-disk representation matches the new model. Runs under the
        # lock (after the state-dir probe) and BEFORE the deploy snapshot so
        # the pre-migration text can seed the transition's file_pre for a
        # byte-exact revert. This install still deploys from the already-loaded
        # legacy host_local_sections_map (representation changed, not behavior).
        overlay_migration = migrate_local_overlay_spans_on_install(profile)
        deploy.validate_srcs_exist(cfg, resolved, repo_root)
        deploy.bootstrap_local(resolved.bootstrap)
        # Cargo binaries install during install, BEFORE deploy. A missing
        # cargo toolchain warns once and continues (soft); per-crate
        # build failures warn (yellow) but do NOT gate the exit code — a
        # crate that won't build is a host-specific outcome, not a config
        # error. No revert tracking — cargo binaries are not cleanly
        # reversible.
        cargo_mod.install_cargo_binaries(resolved.cargo_binaries)

        # P4.3: check for unexpected drift before deploying.
        # Only DRIFTED entries (existing live files that diverge from tracked
        # in unexpected ways) gate install. MISSING entries are expected on
        # first install and are handled by deploy below.
        drift_report = compare_mod.compare_profile(cfg, profile, repo_root)

        _run_predeploy_gates(
            drift_report=drift_report,
            ctx=ctx,
            auto_accept_tracked=auto_accept_tracked,
            auto_accept_live=auto_accept_live,
            section_auto=section_auto,
            yes=yes,
        )

        tracked_root = config.resolve().parent / "tracked"
        scan_result = secrets_mod.run_pre_deploy_scan(
            tracked_root=tracked_root,
            skip=no_secrets_scan,
        )
        if scan_result.findings and not _handle_secret_findings(scan_result, yes=yes):
            typer.secho(
                "install aborted by secrets scan", err=True, fg=typer.colors.RED
            )
            raise typer.Exit(code=1)

        # Resolve user-section drift (shared sections) into per-tracked_file
        # decisions BEFORE the deploy loop so wizard prompts and the
        # bare-install warning fire once, deterministically.
        section_decisions = _resolve_section_decisions(
            ctx,
            section_auto=section_auto,
            interactive=reconcile_user_sections,
        )

        # Pre-extract live user-sections for every section-bearing tracked_file
        # so deploy.copy_atomic can skip its own re-read + re-parse pass.
        # See `precomputed_live_sections` on copy_atomic.
        live_sections_map = _extract_live_sections_map(ctx)

        # For symlink-deployed tracked_files the recorded "touched path" is
        # the symlink's TARGET (where bytes actually land), not the link
        # path itself: GNU patch refuses to patch a symlink as a regular
        # file, so a transition recording the link path would brick revert.
        dst_paths: list[Path] = [
            Path(tf.symlink).expanduser() if tf.symlink is not None else sub_dst
            for tf, _, _, sub_dst in _iter_all_tracked_files(ctx)
        ]
        dst_paths.extend(Path(str(p)).expanduser() for p in resolved.bootstrap)
        # Store files (byte bases, spans sidecars, scalar-base manifests) do
        # NOT ride this patch snapshot: their pre-install state is captured
        # at the pass-2 barrier (state_snapshots below) and revert restores
        # them through that mechanism — recording them here too would
        # double-restore (Invariant I5 now lives in the snapshot path).
        file_pre = transitions.snapshot_paths(dst_paths)
        # When the overlay-span rewrite moved a legacy block, record local.yaml
        # in the transition (append to dst_paths so file_post captures its
        # post-migration content; seed file_pre with the genuine pre-migration
        # text) so revert restores it byte-exact in LOCKSTEP with live.
        seed_overlay_migration_snapshot(overlay_migration, dst_paths, file_pre)
        _record_seed_pre_seed_baseline(
            seeded=seeded,
            local_pre_seed=local_pre_seed,
            dst_paths=dst_paths,
            file_pre=file_pre,
        )

        # Interactive disposition conflict wizard: built ONLY when this install
        # is in interactive-reconcile mode AND stdout is a tty (the same gate
        # the shared user-section wizard uses). Non-tty / --auto ⇒ None, so the
        # driver keeps the bare warn-and-defer / auto behavior.
        conflict_resolver = _build_conflict_resolver(
            reconcile_user_sections=reconcile_user_sections,
            section_auto=section_auto,
        )

        state_snapshots = _deploy_all_tracked_files(
            ctx,
            section_decisions=section_decisions,
            live_sections_map=live_sections_map,
            host_local_sections_map=host_local_sections_map,
            section_auto=section_auto,
            conflict_resolver=conflict_resolver,
            strict_spans=strict_spans,
        )

        retry_failed_ids = (
            _collect_retry_failed_ids(profile) if retry_failed else frozenset()
        )
        ext_delta, ext_outcomes = _reconcile_extensions(
            resolved, retry_failed_ids=retry_failed_ids, yes=yes
        )
        plugin_delta, plugin_outcomes = _reconcile_plugins(
            cfg, resolved, retry_failed_ids=retry_failed_ids, yes=yes
        )
        mcp_delta, mcp_failed = reconcile_mcp_servers(cfg, resolved)

        file_post = transitions.snapshot_paths(dst_paths)

        _emit_reconcile_summary(plugin_outcomes, ext_outcomes)

        if not no_transition:
            target = _write_install_transition(
                profile,
                file_pre,
                file_post,
                ext_delta,
                plugin_delta,
                source_dir=ctx.repo_root,
                reconcile_outcomes=plugin_outcomes + ext_outcomes,
                state_snapshots=state_snapshots,
                mcp_delta=mcp_delta,
            )
            typer.echo(f"transition: {target}")
            typer.echo(f"↩  revert with: setforge revert --profile={profile}")

        _gate_on_mcp_failures(mcp_failed)


def _gate_on_mcp_failures(mcp_failed: list[tuple[str, str]]) -> None:
    """Exit non-zero when any declared MCP server failed to register.

    Cargo failures do NOT gate (a crate that won't build is a soft,
    host-specific outcome — the warning already surfaced), but a declared
    MCP server that could not be registered is a hard reconcile failure.
    """
    if not mcp_failed:
        return
    names = ", ".join(name for name, _err in mcp_failed)
    typer.secho(
        f"install completed with MCP server failures: {names}",
        err=True,
        fg=typer.colors.RED,
    )
    raise typer.Exit(code=1)


def _plan_section_templates_for_install(
    cfg: Config, resolved: ResolvedProfile, repo_root: Path
) -> list[section_templates_mod.SeedPlanEntry]:
    """PLAN seed-once host-local section templates — PURE, no disk write.

    Reads the host's current host-local overlay (legacy block + migrated
    spans) and the template bodies, and returns the seed plan for slots
    whose section is absent. An empty list when the profile declares no
    slots or every slotted section is already populated on the host (the
    seed-once gate). The bodies are committed to ``local.yaml`` later by
    :func:`_commit_section_template_seed`, under the install lock and
    after consent; meanwhile :func:`setforge.source.injected_seed` makes
    the planned bodies visible to the overlay readers + welcome preview
    without a write.
    """
    if not resolved.section_slots:
        return []
    overlay = source_mod.load_local_host_local_sections(source_mod.LOCAL_CONFIG_PATH)
    # Project the provenance-marked HostLocalSectionName keys to plain str
    # for the seed planner's presence set.
    existing: dict[str, set[str]] = {
        tf_id: {str(name) for name in sections} for tf_id, sections in overlay.items()
    }
    return section_templates_mod.plan_section_seeds(
        cfg, resolved, repo_root, existing_overlay=existing
    )


def _commit_section_template_seed(
    seed_plan: list[section_templates_mod.SeedPlanEntry],
) -> bool:
    """COMMIT the seed plan to ``local.yaml`` — the disk write, post-consent.

    Writes the planned template bodies into ``local.yaml`` as
    ``host_local_sections`` blocks and emits the one-time green seeded
    message. Returns ``True`` when at least one section was written,
    ``False`` on the empty-plan / already-populated no-op. The caller
    runs this under ``profile_lock`` after the welcome + git-check gates,
    so a declined or aborted install never reaches it.
    """
    seeded = section_templates_mod.seed_section_templates(
        seed_plan, source_mod.LOCAL_CONFIG_PATH
    )
    if seeded:
        names = ", ".join(sorted(e.section_name for e in seed_plan))
        typer.secho(
            f"seeded host-local section template(s): {names}",
            err=True,
            fg=typer.colors.GREEN,
        )
    return seeded


def _read_local_yaml_pre_seed() -> str | None:
    """Read ``local.yaml``'s current bytes (``None`` when absent).

    Captured BEFORE :func:`_commit_section_template_seed` so the install
    transition's ``file_pre`` baseline is the genuine pre-seed content and
    revert restores an unseeded ``local.yaml``.
    """
    try:
        return source_mod.LOCAL_CONFIG_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _commit_seed_under_lock(
    seed_plan: list[section_templates_mod.SeedPlanEntry],
) -> tuple[bool, str | None]:
    """Capture local.yaml's pre-seed bytes, then COMMIT the seed plan.

    Returns ``(seeded, local_pre_seed)``: whether a section was written
    and the genuine pre-seed bytes (``None`` when nothing was planned).
    The pre-seed snapshot is taken BEFORE the write so the caller can
    record it as the transition's ``file_pre`` baseline (revert restores
    an unseeded ``local.yaml``).
    """
    local_pre_seed = _read_local_yaml_pre_seed() if seed_plan else None
    seeded = _commit_section_template_seed(seed_plan)
    return seeded, local_pre_seed


def _record_seed_pre_seed_baseline(
    *,
    seeded: bool,
    local_pre_seed: str | None,
    dst_paths: list[Path],
    file_pre: dict[Path, str | None],
) -> None:
    """Override the local.yaml ``file_pre`` baseline with pre-seed bytes.

    The overlay-span migration captured a POST-seed ``pre_text`` (the seed
    wrote just before it), so when THIS install committed a seed, force the
    transition baseline back to the genuine pre-seed content. ``file_post``
    still captures the final migrated text, so revert restores an UNSEEDED
    ``local.yaml``. A run that only migrated a pre-existing block (no new
    seed) keeps the migration's own baseline untouched. Mutates
    ``dst_paths`` and ``file_pre`` in place.
    """
    if not seeded:
        return
    if source_mod.LOCAL_CONFIG_PATH not in dst_paths:
        dst_paths.append(source_mod.LOCAL_CONFIG_PATH)
    file_pre[source_mod.LOCAL_CONFIG_PATH] = local_pre_seed


def _handle_secret_findings(
    scan_result: SecretsScanResult,
    *,
    yes: bool,
    allowlist_path: Path | None = None,
) -> bool:
    """Prompt the user once per unique snippet-hash; return ``True`` to proceed.

    Returns ``False`` as soon as any finding resolves to
    :data:`SecretAction.ABORT` so the install loop short-circuits before
    mutating live state. :data:`SecretAction.ALLOWLIST` appends the
    finding's ``snippet_hash`` to the allowlist file via
    :func:`secrets_mod.append_to_allowlist`;
    :data:`SecretAction.SILENCE_ONE_SHOT` skips this finding for the
    current install only.
    """
    if allowlist_path is None:
        allowlist_path = Path.home() / ".config" / "setforge" / "secrets-allowlist"
    seen_hashes: set[str] = set()
    for finding in scan_result.findings:
        if finding.snippet_hash in seen_hashes:
            continue
        seen_hashes.add(finding.snippet_hash)
        if not _resolve_one_finding(finding, yes=yes, allowlist_path=allowlist_path):
            return False
    return True


def _resolve_one_finding(
    finding: SecretFinding,
    *,
    yes: bool,
    allowlist_path: Path,
) -> bool:
    """Prompt for one finding's action; return ``False`` on ABORT."""
    action = prompt_secret_action(finding, yes=yes)
    match action:
        case SecretAction.ABORT:
            return False
        case SecretAction.ALLOWLIST:
            secrets_mod.append_to_allowlist(
                snippet_hash=finding.snippet_hash,
                allowlist_path=allowlist_path,
            )
            return True
        case SecretAction.SILENCE_ONE_SHOT:
            return True
        case _:
            assert_never(action)


def _collect_retry_failed_ids(profile: str) -> frozenset[str]:
    """Read the previous transition's ``reconcile_outcomes`` and return
    the set of items whose status was ``"skipped"``.

    Returns an empty :class:`frozenset` when there's no prior transition
    or the previous transition has no ``reconcile_outcomes.json`` file
    (backward-compat path for transitions written before the schema bump).
    Used by ``setforge install --retry-failed`` to filter the reconcile
    work list to only those previously-failed ids.
    """
    prev = load_latest(profile)
    if prev is None:
        return frozenset()
    outcomes = load_reconcile_outcomes(prev)
    return frozenset(o.item_id for o in outcomes if o.status is ReconcileStatus.SKIPPED)
