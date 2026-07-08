"""install subcommand — orchestrates tracked-file deploy + extension/plugin reconcile.

Wires deploy.copy_atomic, extension/plugin reconcile, and the transition
snapshot. Imports ``app`` from
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
    _iter_all_tracked_files,
    _parse_section_auto,
)
from setforge.cli._install_helpers import (
    _deploy_all_tracked_files,
    _dry_run_pipeline,
    _install_recorded_nothing,
    _load_validated_host_local_sections,
    _run_predeploy_gates,
    _want_interactive_reconcile,
    _write_install_transition,
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
    apply_host_local_tracked_file_overrides,
    apply_local_overlay,
    load_config,
    refuse_unmigrated_host_local_leak,
    resolve_profile,
)
from setforge.errors import SetforgeError
from setforge.locking import profile_lock
from setforge.reconcile import host_local_record
from setforge.secrets import SecretAction, SecretFinding, SecretsScanResult
from setforge.transitions import (
    ReconcileStatus,
    load_latest,
    load_reconcile_outcomes,
)


def _fetch_upstream(
    install_source: source_mod.Source, *, no_fetch: bool, dry_run: bool
) -> None:
    """Fetch the git config source before deploy (the A0 fetch-upstream step).

    A :class:`~setforge.source.PathSource` no-ops inside ``fetch_source``;
    ``--no-fetch`` skips the pull entirely for offline / CI runs (a missing
    GitSource clone then surfaces a clean ``SourceNotCloned`` downstream
    rather than a silent network touch). On ``--dry-run`` the pull is only
    announced (WOULD-prefixed), never performed. A ``GitOpError`` /
    ``DirtySourceCheckout`` propagates as a ``SetforgeError`` and aborts the
    install before any tracked file is written. Only a real GitSource pull
    echoes a status line, so a PathSource install stays quiet.
    """
    if no_fetch:
        return
    is_git = isinstance(install_source, source_mod.GitSource)
    if dry_run:
        if is_git:
            typer.echo("WOULD fetch upstream config source")
        return
    fetch_message = source_mod.fetch_source(install_source)
    if is_git:
        typer.echo(fetch_message)


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
    no_fetch: bool = typer.Option(
        False,
        "--no-fetch",
        help=(
            "Skip the pre-deploy upstream fetch of a git config source "
            "(offline / air-gapped / CI). The install reconciles against the "
            "already-checked-out clone; nothing is pulled. A path source "
            "never fetches, so this flag is a no-op there."
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
    # Refuse before mutation: unmigrated host-local content could leak.
    refuse_unmigrated_host_local_leak(cfg, verb="install", profile=profile)
    repo_root = config.resolve().parent
    resolved = resolve_profile(cfg, profile)
    # Both overlays below must apply AFTER profile resolution.
    apply_host_local_tracked_file_overrides(cfg)
    # STAGE B: sections live in the reconcile store now, threaded read-only.
    host_local_sections_map = _load_validated_host_local_sections(
        cfg, resolved, repo_root, profile
    )
    apply_local_overlay(cfg, resolved, profile)
    ctx = ProfileContext(
        cfg=cfg, resolved=resolved, repo_root=repo_root, profile=profile
    )

    # Fires BEFORE git-check so a dirty fresh-host source can't raise first.
    fresh = is_fresh_host()
    if fresh and not dry_run:
        reject_auto_on_fresh_host(auto=auto)
        inventory = build_welcome_inventory(ctx)

        def _welcome_dry_run() -> None:
            _dry_run_pipeline(ctx=ctx)

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
    # A0 fetch-upstream. Pull the git config source FIRST so the freshly
    # checked-out content is what gets reconciled below (and what the
    # git-check then judges for staleness).
    install_source = resolve_source_for_git_check(repo_root)
    _fetch_upstream(install_source, no_fetch=no_fetch, dry_run=dry_run)

    run_git_check_or_raise(
        source=install_source,
        no_git_check=no_git_check,
    )

    # Boundary-not-leaf dispatch. When `--dry-run` is set,
    # route through `_dry_run_pipeline` which calls only the read-only
    # shared helpers (compare_profile, vscode_extensions.reconcile(dry_run=True),
    # claude_plugins.reconcile(dry_run=True)). The real pipeline below is
    # provably unreachable: zero mutating subprocess calls, zero file
    # writes, zero transition record. The boolean is NOT threaded into
    # deploy / transitions / compare / merge — those modules stay
    # leaf-pure and the dry-run path bypasses them entirely.
    if dry_run:
        _dry_run_pipeline(ctx=ctx)
        return

    with profile_lock(profile):
        if not no_transition:
            transitions.ensure_state_dir_writable()
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
        # Thread the validated host-local sections overlay (computed at
        # line 246) so a live file that already received its injected
        # host-local sections does NOT surface as spurious drift in the
        # report feeding the section-reconcile gate — matching what the
        # standalone `setforge compare` reports.
        drift_report = compare_mod.compare_profile(
            cfg, profile, repo_root, host_local_sections=host_local_sections_map
        )

        _run_predeploy_gates(
            drift_report=drift_report,
            ctx=ctx,
            auto_accept_tracked=auto_accept_tracked,
            auto_accept_live=auto_accept_live,
            yes=yes,
        )

        # Refuse-before-write: pre-flight the symlink-dst clobber refusal.
        # deploy_symlinked_file() raises when a regular file or directory
        # already sits at a symlink tracked_file's dst, but that check only
        # fires at pass-2 WRITE time — a symlink ordered after regular files
        # would let those earlier writes land, then abort with no transition
        # recorded (un-revertable partial install). Surfacing the same
        # condition HERE, before any mutation (seed commit, overlay migration,
        # or deploy), keeps the install all-or-nothing.
        _refuse_on_symlink_dst_conflicts(ctx)

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

        # Interactive reconcile: resolve conflicts through the reconcile
        # engine's per-region wizard ONLY when this install is in
        # interactive-reconcile mode AND stdout is a tty (the same gate the
        # shared user-section wizard uses). Non-tty / --auto ⇒ False, so the
        # driver keeps the bare warn-and-defer / auto behavior.
        interactive = _want_interactive_reconcile(
            reconcile_user_sections=reconcile_user_sections,
            section_auto=section_auto,
        )

        deploy_outcome = _deploy_all_tracked_files(
            ctx,
            host_local_sections_map=host_local_sections_map,
            section_auto=section_auto,
            interactive=interactive,
        )

        # Seed AFTER deploy so its pre-install snapshot is the revert baseline.
        seeded = host_local_record.seed_section_slots_to_store(
            cfg, resolved, repo_root, profile
        )
        if seeded:
            typer.secho(
                f"seeded host-local section template(s): {', '.join(sorted(seeded))}",
                err=True,
                fg=typer.colors.GREEN,
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

        # INV-4: an idempotent re-install (empty content patch AND no store /
        # delta / mode / seed change) writes NO transition dir — skip the churn
        # of an empty, indefinitely-kept record. Any store mutation with an
        # empty patch (e.g. honoring a deletion) still records its transition so
        # revert can restore the store.
        if not no_transition and not _install_recorded_nothing(
            file_pre=file_pre,
            file_post=file_post,
            deploy_outcome=deploy_outcome,
            ext_delta=ext_delta,
            plugin_delta=plugin_delta,
            mcp_delta=mcp_delta,
            reconcile_outcomes=plugin_outcomes + ext_outcomes,
            seeded=bool(seeded),
        ):
            target = _write_install_transition(
                profile,
                file_pre,
                file_post,
                ext_delta,
                plugin_delta,
                source_dir=ctx.repo_root,
                reconcile_outcomes=plugin_outcomes + ext_outcomes,
                state_snapshots=deploy_outcome.state_snapshots,
                mcp_delta=mcp_delta,
                file_modes=deploy_outcome.prior_modes,
            )
            typer.echo(f"transition: {target}")
            typer.echo(f"↩  revert with: setforge revert --profile={profile}")

        _gate_on_mcp_failures(mcp_failed)
        _gate_on_deferred_reconcile(deploy_outcome.deferred_reconcile, interactive)


def _gate_on_deferred_reconcile(
    deferred: tuple[Path, ...],
    interactive: bool,
) -> None:
    """Exit non-zero when a non-interactive install left reconcile conflicts.

    A plain file whose conflict DEFERRED non-interactively (no TTY, no
    ``--auto``) keeps live but leaves the upstream change unresolved. The
    transition is already written (the partial install stays revertable); this
    gate signals the unresolved set so CI / cron fails loudly instead of
    silently passing over a conflict. An interactive run (``interactive`` True)
    already let the user choose Skip per region, so it does NOT gate — those
    defers warned per file during the deploy.

    Count-only: each deferred file was ALREADY echoed per-file (the yellow
    ``merge conflict kept live`` warn during deploy), so this gate reports just
    the count + the actionable resolution — no second per-file list.
    """
    if not deferred or interactive:
        return
    count = len(deferred)
    typer.secho(
        f"error: {count} file{'s' if count != 1 else ''} deferred with "
        "unresolved conflicts (see the per-file warnings above) — re-run "
        "`setforge install` interactively, or pass --auto=keep-live / "
        "--auto=use-tracked to resolve non-interactively.",
        err=True,
        fg=typer.colors.RED,
    )
    raise typer.Exit(code=1)


def _refuse_on_symlink_dst_conflicts(ctx: ProfileContext) -> None:
    """Refuse the install when a symlink tracked_file's dst is already occupied.

    Mirrors the refusal in :func:`deploy.deploy_symlinked_file` (a regular file
    or a directory — but NOT a pre-existing symlink — sitting at the link's
    ``dst``), but runs as a pass-1 refuse-before-write gate so the abort fires
    BEFORE any file is written or any store / local.yaml is mutated. Without
    this pre-flight the same condition only surfaces at pass-2 write time, where
    a symlink ordered after regular-file tracked_files would let those earlier
    writes land and then raise with no transition recorded — an un-revertable
    partial install. Every conflicting dst is collected so the user sees the
    complete set in one aggregated error rather than one failure per attempt.
    """
    failures: list[str] = []
    for tracked_file, _sub_name, _sub_src, sub_dst in _iter_all_tracked_files(ctx):
        if tracked_file.symlink is None:
            continue
        if sub_dst.is_symlink() or not sub_dst.exists():
            continue
        kind = "directory" if sub_dst.is_dir() else "regular file"
        failures.append(
            f"refusing to deploy symlink at {sub_dst}: a {kind} is already "
            f"present. Move it aside or remove it before deploying "
            f"tracked_file with symlink: {tracked_file.symlink!r}."
        )
    if failures:
        raise SetforgeError("\n".join(failures))


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
