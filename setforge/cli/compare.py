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
from setforge.cli._install_helpers import _load_validated_host_local_sections
from setforge.cli._output import render
from setforge.compare import CompareStatus, load_ignored_orphans, resolve_dst
from setforge.config import (
    Config,
    apply_host_local_tracked_file_overrides,
    apply_local_overlay,
    load_config,
    resolve_profile,
)
from setforge.host_local_inject import HOST_LOCAL_PROVENANCE_TAG
from setforge.locking import profile_lock
from setforge.section_reconcile import SectionDriftState
from setforge.sections import SectionSemantics, extract_sections
from setforge.source import HostLocalSection, HostLocalSectionName


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
    if strict and not check:
        raise typer.BadParameter("--strict requires --check")
    config = _resolve_config_arg(config)
    cfg = load_config(config)
    repo_root = config.resolve().parent
    resolved = resolve_profile(cfg, profile)
    # Apply local.yaml host-local mode/dst/symlink_target overlay.
    # Captures the per-tracked_file override mapping
    # so the renderer can emit ``[host-local mode=...]`` /
    # ``[host-local dst=...]`` / ``[host-local symlink → ...]``
    # provenance tags next to each affected entry.
    host_local_overrides = apply_host_local_tracked_file_overrides(cfg)
    # Apply local.yaml plugin/extension/marketplace overlay (SPEC 2).
    # Mutates resolved and cfg in place; the resolved
    # provenance lists drive the host-overlay block printed below the
    # drift report (cf. render_local_overlay_block in setforge.compare).
    overlay_resolution = apply_local_overlay(cfg, resolved, profile)
    profile_ctx = ProfileContext(
        cfg=cfg, resolved=resolved, repo_root=repo_root, profile=profile
    )
    _refuse_legacy_live_markers(profile_ctx, command="compare")

    with profile_lock(profile):
        # Load + validate the local.yaml host_local_sections
        # overlay so ``compare_profile`` threads it into ``diff_file`` —
        # a live file that already received its host-local sections must
        # not surface as drift. Same validator install uses (anchors
        # resolved at deploy time; this layer only sniffs file-type).
        host_local_sections_map = _load_validated_host_local_sections(
            cfg, resolved, repo_root
        )
        report = compare_mod.compare_profile(
            cfg,
            profile,
            repo_root,
            transitions_dir=transitions.transitions_root(),
            ignored=load_ignored_orphans(),
            host_local_sections=host_local_sections_map,
        )

    console = Console()

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
        _render_compare_report(report, console, full_diff=full_diff)
        # SPEC 1 mockup: surface every host-local section
        # the install would inject, tagged with the canonical provenance
        # marker (HOST_LOCAL_PROVENANCE_TAG). Lives BELOW the drift
        # summary so the diff body and per-status counts stay grouped,
        # mirroring the mockup's ordering ("✓ no drift ... + <tag> X").
        _render_host_local_preview(host_local_sections_map, cfg, console)
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
    (sorted), ``disposition`` (string or null), ``drift_is_expected``
    (bool). Orphans surface as a list of strings. No diff bodies in
    JSON mode — they belong to the human view; ``compare --full-diff``
    is a human-oriented surface.
    """
    entries = [
        {
            "name": entry.name,
            "status": entry.status.value,
            "unexpected_drift_keys": sorted(entry.unexpected_drift_keys),
            "expected_drift_keys": sorted(entry.expected_drift_keys),
            "disposition": entry.disposition.value
            if entry.disposition is not None
            else None,
            "drift_is_expected": entry.drift_is_expected,
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


def _classify_section_state(section_name: str, live_names: set[str]) -> tuple[str, str]:
    """Return ``(sigil, suffix)`` describing whether ``section_name`` is injected.

    ``"="`` + ``"already injected"`` when the section already appears in
    the live file's marker set (the previous install landed); ``"+"`` +
    ``"would be injected"`` otherwise. The arms collapse the two
    ``console.print`` branches in :func:`_render_host_local_preview` to
    a single formatted line.
    """
    if section_name in live_names:
        return "=", "already injected"
    return "+", "would be injected"


def _render_host_local_preview(
    host_local_sections_map: dict[str, dict[HostLocalSectionName, HostLocalSection]],
    cfg: Config,
    console: Console,
) -> None:
    """Emit the SPEC 1 host-local would-be-injected preview block.

    Per the mockup: ``+ <HOST_LOCAL_PROVENANCE_TAG> X ← would be injected``.
    One indented block per tracked_file with at least one host-local
    section declared in local.yaml. For each section, classifies
    "would be injected" (section name not present in live file) vs
    "already injected" (already on disk from a prior install) by
    re-extracting marker names from the live file. The compare command
    is read-only — this is the user's preview of what install would do
    without running it, mirroring the dry-run install output.

    No-op when ``host_local_sections_map`` is empty.
    ``host_local_sections_map`` is the output of
    :func:`_load_validated_host_local_sections`, which already filters
    by the resolved profile's tracked_files — no further profile-membership
    check is needed here.
    """
    if not host_local_sections_map:
        return
    rendered_any = False
    for tf_id, sections_map in host_local_sections_map.items():
        tracked_file = cfg.tracked_files[tf_id]
        dst = resolve_dst(tracked_file)
        # Existing live-section names — used to classify already-injected
        # vs would-be-injected per section. allow_legacy=True so a pre-hash
        # live file does not crash the preview.
        try:
            live_text = dst.read_text(encoding="utf-8")
        except FileNotFoundError:
            live_names: set[str] = set()
        else:
            live_names = set(extract_sections(live_text, allow_legacy=True))
        if not rendered_any:
            console.print("")
            rendered_any = True
        console.print(f"{dst}  ({tf_id})", markup=False)
        for section_name in sections_map:
            sigil, suffix = _classify_section_state(section_name, live_names)
            console.print(
                f"  {sigil} {HOST_LOCAL_PROVENANCE_TAG} {section_name}     ← {suffix}",
                markup=False,
            )


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
