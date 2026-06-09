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
    compare as compare_mod,
)
from setforge import (
    deploy,
    transitions,
)
from setforge import secrets as secrets_mod
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
    _revert_lockstep_paths,
    _run_predeploy_gates,
    _validate_span_file_types,
    _write_install_transition,
    migrate_local_overlay_spans_on_install,
    seed_overlay_migration_snapshot,
)
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
            "upstream) from a warning to a refuse-install. Forked-span and "
            "non-strict orphans always warn and continue."
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
    # Load + validate the local.yaml host_local_sections overlay (host-local).
    # Validation is file-type only at this layer: anchors / bodies are
    # resolved during deploy._compute_content. Empty mapping when local.yaml
    # is absent or declares no host-local sections.
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

        # P4.3: check for unexpected drift before deploying.
        # Only DRIFTED entries (existing live files that diverge from tracked
        # in unexpected ways) gate install. MISSING entries are expected on
        # first install and are handled by deploy below.
        drift_report = compare_mod.compare_profile(cfg, profile, repo_root)

        _run_predeploy_gates(
            drift_report=drift_report,
            ctx=ctx,
            config=config,
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
        # Roll every disposition file's stored byte base (and, for span-bearing
        # files, its spans sidecar) in LOCKSTEP with live on revert (Invariant
        # I5): snapshot them into the transition so the patch -R mechanism
        # reverts live + base (+ sidecar) together. Capturing the base for
        # PLAIN disposition files too is the data-loss fix — a first-install
        # seeded base must be deleted on revert, not stranded.
        dst_paths.extend(_revert_lockstep_paths(ctx))

        file_pre = transitions.snapshot_paths(dst_paths)
        # When the overlay-span rewrite moved a legacy block, record local.yaml
        # in the transition (append to dst_paths so file_post captures its
        # post-migration content; seed file_pre with the genuine pre-migration
        # text) so revert restores it byte-exact in LOCKSTEP with live.
        seed_overlay_migration_snapshot(overlay_migration, dst_paths, file_pre)

        # Interactive disposition conflict wizard: built ONLY when this install
        # is in interactive-reconcile mode AND stdout is a tty (the same gate
        # the shared user-section wizard uses). Non-tty / --auto ⇒ None, so the
        # driver keeps the bare warn-and-defer / auto behavior.
        conflict_resolver = _build_conflict_resolver(
            reconcile_user_sections=reconcile_user_sections,
            section_auto=section_auto,
        )

        _deploy_all_tracked_files(
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
            )
            typer.echo(f"transition: {target}")
            typer.echo(f"↩  revert with: setforge revert --profile={profile}")


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
