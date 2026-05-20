"""compare subcommand — read-only drift report (live vs tracked) for a profile.

Includes two compare-private helpers (``_print_section_reconcile_dry_run``,
``_render_drift_file``) that surface section-marker dry-run output and
per-file rich diff bodies.
"""

from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.syntax import Syntax

from setforge import compare as compare_mod
from setforge import section_reconcile, section_wizard, transitions
from setforge.cli import (
    _CONFIG_OPTION,
    _PROFILE_OPTION,
    _resolve_config_arg,
    app,
)
from setforge.cli._help_examples import COMPARE_EXAMPLES
from setforge.cli._helpers import (
    ProfileContext,
    _iter_section_tracked_files,
    _refuse_legacy_live_markers,
)
from setforge.cli._output import render
from setforge.compare import CompareStatus, load_ignored_orphans
from setforge.config import apply_preserve_user_keys_overlay, load_config, resolve_profile
from setforge.section_reconcile import SectionDriftState
from setforge.sections import SectionSemantics


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
    reconcile_user_sections: bool = typer.Option(
        False,
        "--reconcile-user-sections",
        help=(
            "Dry-run: print what 'install --reconcile-user-sections' "
            "would prompt about. Read-only — no live mutation, no prompts."
        ),
    ),
) -> None:
    """Report drift between tracked and live for every tracked_file in the profile."""
    config = _resolve_config_arg(config)
    cfg = load_config(config)
    repo_root = config.resolve().parent
    resolved = resolve_profile(cfg, profile)
    # Apply local.yaml preserve_user_keys overlay (mockup B / SPEC 8).
    # See setforge/cli/install.py for the rationale; both commands must
    # apply the overlay so the renderer can read preserve_user_keys_resolved
    # for the mockup-B provenance display.
    apply_preserve_user_keys_overlay(cfg, profile)
    profile_ctx = ProfileContext(
        cfg=cfg, resolved=resolved, repo_root=repo_root, profile=profile
    )
    _refuse_legacy_live_markers(profile_ctx, command="compare")
    report = compare_mod.compare_profile(
        cfg,
        profile,
        repo_root,
        transitions_dir=transitions.transitions_root(),
        ignored=load_ignored_orphans(),
    )

    console = Console()

    def _human() -> None:
        # Mockup B / SPEC 8 — emit the host-overlay block BEFORE the
        # drift summary so its provenance display sits adjacent to the
        # configured-keys-on-this-host context, not buried below the
        # per-file diff bodies.
        for line in compare_mod.render_preserve_user_keys_overlay_block(
            cfg, resolved
        ):
            # markup=False — mockup-B provenance tags use square brackets
            # (e.g. ``[from local.yaml]``) which Rich would otherwise
            # interpret as markup spans and silently strip.
            console.print(line, markup=False)
        _render_compare_report(report, console, full_diff=full_diff)
        if reconcile_user_sections:
            _print_section_reconcile_dry_run(profile_ctx, console)

    render(ctx.obj, "compare", _compare_json_data(report), human_fn=_human)

    if check:
        if strict:
            if any(e.status == CompareStatus.DRIFTED for e in report.entries):
                raise typer.Exit(code=1)
        elif report.has_unexpected_drift:
            raise typer.Exit(code=1)


def _compare_json_data(report: compare_mod.CompareReport) -> dict[str, Any]:
    """Build the JSON-mode payload for ``setforge compare``.

    Renders the same report the human view shows, projected as plain
    dict/list/string shapes so ``json.dumps`` can serialise without
    custom encoders. Per-entry fields: ``name``, ``status`` (StrEnum
    value), ``unexpected_drift_keys`` (sorted), ``expected_drift_keys``
    (sorted). Orphans surface as a list of strings. No diff bodies in
    JSON mode — they belong to the human view; ``compare --full-diff``
    is a human-oriented surface.
    """
    entries = [
        {
            "name": entry.name,
            "status": entry.status.value,
            "unexpected_drift_keys": sorted(entry.unexpected_drift_keys),
            "expected_drift_keys": sorted(entry.expected_drift_keys),
        }
        for entry in report.entries
    ]
    return {
        "entries": entries,
        "orphans": [str(orphan.path) for orphan in report.orphans],
        "has_unexpected_drift": report.has_unexpected_drift,
    }


def _render_compare_report(
    report: compare_mod.CompareReport,
    console: Console,
    *,
    full_diff: bool,
) -> None:
    """Print the summary table, per-status counts, optional unified diffs,
    and the orphans block (when any)."""
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

    if full_diff:
        for entry in report.entries:
            if entry.diff:
                console.print(Syntax(entry.diff, "diff"))


def _print_section_reconcile_dry_run(ctx: ProfileContext, console: Console) -> None:
    """Render the ``compare --reconcile-user-sections`` dry-run output.

    For every tracked_file with ``preserve_user_sections=True`` that exists
    on both sides, walks the section classifier and prints one line per
    drifted shared section with its three-way state label, plus a
    one-line aggregate per file. No prompts, no live mutation.

    The output is structured for grep-based assertions in the Docker
    e2e suite (variant 18) — each drifted-section line includes the
    file path, section name, and state label.
    """
    any_emitted = False
    for sub_src, sub_dst in _iter_section_tracked_files(ctx):
        if not sub_dst.exists() or not sub_src.exists():
            continue
        if _render_drift_file(sub_src, sub_dst, console):
            any_emitted = True
    if not any_emitted:
        console.print("\nno shared user-section drift to reconcile.")


def _render_drift_file(sub_src: Path, sub_dst: Path, console: Console) -> bool:
    """Render the dry-run drift block for one (tracked, live) file pair.

    Returns ``True`` when at least one drifted-section line was printed
    for this file (i.e. the file contributed to the overall
    ``any_emitted`` flag in :func:`_print_section_reconcile_dry_run`).
    """
    tracked_text = sub_src.read_text(encoding="utf-8")
    live_text = sub_dst.read_text(encoding="utf-8")
    drifts = section_reconcile.classify_section_drift(tracked_text, live_text)
    summary = section_wizard.format_drift_summary(drifts.values())
    if not summary:
        return False
    console.print(f"\n[bold]{sub_dst}[/bold]: {summary}")
    for sec_name, drift in drifts.items():
        if drift.semantics is not SectionSemantics.SHARED:
            continue
        if drift.state is SectionDriftState.NO_DRIFT:
            continue
        label = section_wizard.state_label(drift.state)
        console.print(f"  three-way {label} [cyan]{sec_name}[/cyan]")
    return True
