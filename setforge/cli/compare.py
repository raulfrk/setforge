"""compare subcommand — read-only drift report (live vs tracked) for a profile."""

from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.syntax import Syntax

from setforge import codex_lifecycle, transitions
from setforge import compare as compare_mod
from setforge.cli import (
    _CONFIG_OPTION,
    _PROFILE_OPTION,
    _resolve_config_arg,
    app,
)
from setforge.cli._help_examples import COMPARE_EXAMPLES
from setforge.cli._helpers import (
    ProfileContext,
    _refuse_duplicate_section_names,
)
from setforge.cli._output import make_console, render
from setforge.compare import CompareStatus, load_ignored_orphans
from setforge.config import (
    OrphanOverlay,
    collect_orphan_overlays,
    load_config,
    resolve_effective_profile,
)
from setforge.locking import profile_lock


@app.command(epilog=COMPARE_EXAMPLES)
def compare(
    ctx: typer.Context,
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    full_diff: bool = typer.Option(
        False,
        "--full-diff",
        "--full",
        help="Append unified diff body below the summary table.",
    ),
    check: bool = typer.Option(
        False, "--check", help="Exit non-zero on unexpected drift (for CI)."
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help="With --check: exit 1 on any drift (expected or unexpected).",
    ),
) -> None:
    """Report drift between tracked and live for every tracked_file in the profile."""
    if strict and not check:
        raise typer.BadParameter("--strict requires --check")
    config = _resolve_config_arg(config)
    cfg = load_config(config)
    repo_root = config.resolve().parent
    effective = resolve_effective_profile(cfg, profile, repo_root)
    resolved = effective.resolved
    host_local_overrides = effective.tracked_file_overrides
    # Surface local.yaml overlay entries the apply site silently skipped
    # (unknown id / off-profile id). Read-only diagnosis — collected from
    # the same overlay block, classified against cfg.tracked_files and the
    # resolved profile's tracked_files list.
    orphan_overlays = collect_orphan_overlays(cfg, resolved)
    overlay_resolution = effective.local_overlay
    profile_ctx = ProfileContext(
        cfg=cfg, resolved=resolved, repo_root=repo_root, profile=profile
    )
    _refuse_duplicate_section_names(profile_ctx, command="compare")
    ownership_authorized = compare_mod.file_authorization_map(cfg, resolved, repo_root)

    with profile_lock(profile):
        report = compare_mod.compare_profile(
            cfg,
            profile,
            repo_root,
            transitions_dir=transitions.transitions_root(),
            ignored=load_ignored_orphans(),
            ownership_authorized=ownership_authorized,
            resolved=resolved,
        )
        report = codex_lifecycle.append_projection(
            report, cfg, resolved, repo_root, profile=profile
        )

    console = make_console()

    def _human() -> None:
        # Render host-local mode/dst/symlink_target
        # override provenance tags. Same markup=False discipline as
        # the preserve_user_keys block — the tags carry square brackets.
        for line in compare_mod.render_host_local_tracked_file_overrides_block(
            host_local_overrides
        ):
            console.print(line, markup=False)
        # SPEC 2 — emit the per-axis effective-set block (plugins /
        # extensions / marketplaces) with [from local.yaml] / SPEC-2
        # remove tags inline, plus the footer summary line. soft_wrap
        # so the footer-summary line (~80+ cols) does not break mid-
        # phrase under Rich's auto-wrap (would corrupt grep-based
        # assertions in the e2e suite).
        for line in compare_mod.render_local_overlay_block(cfg, overlay_resolution):
            console.print(line, markup=False, soft_wrap=True)
        _render_compare_report(
            report, console, full_diff=full_diff, orphan_overlays=orphan_overlays
        )

    render(
        ctx.obj,
        "compare",
        _compare_json_data(report, orphan_overlays),
        human_fn=_human,
    )

    if check:
        if strict:
            if any(e.status != CompareStatus.UNCHANGED for e in report.entries):
                raise typer.Exit(code=1)
        elif report.has_unexpected_drift:
            raise typer.Exit(code=1)


def _compare_json_data(
    report: compare_mod.CompareReport,
    orphan_overlays: tuple[OrphanOverlay, ...] | list[OrphanOverlay] = (),
) -> dict[str, Any]:
    """Build the JSON-mode payload for ``setforge compare``.

    Renders the same report the human view shows, projected as plain
    dict/list/string shapes so ``json.dumps`` can serialise without
    custom encoders. Per-entry fields: ``name``, ``status`` (StrEnum
    value), ``drift_class`` (string or null — null unless DRIFTED),
    ``reason`` (string or null), and ``span_only_drift`` / ``drift_is_expected``
    (always ``false`` since the spans retirement; kept for consumers). No
    diff bodies in JSON mode — they belong to the human view;
    ``compare --full-diff`` is a human-oriented surface.

    Top-level keys: ``entries``, ``orphans``, ``has_unexpected_drift``,
    and ``orphan_overlay_entries`` — a list of ``{"id", "class"}`` objects
    (``class`` is ``"unknown"`` or ``"off_profile"``) for each ``local.yaml``
    overlay entry the apply site silently skipped. Additive — existing keys
    are untouched.
    """
    entries = [
        {
            "name": entry.name,
            "status": entry.status.value,
            "drift_class": entry.drift_class.value
            if entry.drift_class is not None
            else None,
            "reason": entry.reason,
            "span_only_drift": False,
            "drift_is_expected": False,
        }
        for entry in report.entries
    ]
    return {
        "entries": entries,
        "orphans": [str(orphan.path) for orphan in report.orphans],
        "has_unexpected_drift": report.has_unexpected_drift,
        "orphan_overlay_entries": [
            {"id": o.id, "class": o.class_.value} for o in orphan_overlays
        ],
    }


def _render_compare_report(
    report: compare_mod.CompareReport,
    console: Console,
    *,
    full_diff: bool,
    orphan_overlays: list[OrphanOverlay],
) -> None:
    """Print the summary table, per-status counts, optional unified diffs,
    the orphans block (when any), and the skipped-overlay-entries block
    (when any local.yaml overlay entry was silently skipped by the apply
    site)."""
    table = compare_mod.compare_summary_table(report)
    console.print(table)

    unchanged_count = sum(
        1 for e in report.entries if e.status == CompareStatus.UNCHANGED
    )
    missing_count = sum(1 for e in report.entries if e.status == CompareStatus.MISSING)
    if unchanged_count:
        console.print(f"UNCHANGED: {unchanged_count} files")
    if missing_count:
        console.print(f"MISSING: {missing_count} files")

    if report.orphans:
        console.print(f"\nOrphans ({len(report.orphans)}):")
        for orphan in report.orphans:
            console.print(f"  {orphan.path}")
        console.print(
            "[dim]run `setforge cleanup-orphans --profile=<name>` "
            "to review and remove.[/dim]"
        )

    if orphan_overlays:
        console.print(f"\nSkipped overlay entries ({len(orphan_overlays)}):")
        for overlay_orphan in orphan_overlays:
            console.print(
                f"  {overlay_orphan.id} [{overlay_orphan.class_.value}]",
                markup=False,
            )
        console.print(
            "[dim]run `setforge validate --profile=<name>` to diagnose "
            "(unknown id → error; off_profile → note).[/dim]"
        )

    if full_diff:
        _print_full_diffs(report, console)


def _print_full_diffs(report: compare_mod.CompareReport, console: Console) -> None:
    for entry in report.entries:
        if not entry.diff:
            continue
        if console.is_terminal:
            console.print(Syntax(entry.diff, "diff", word_wrap=True))
        else:
            console.print(entry.diff, markup=False, highlight=False)
