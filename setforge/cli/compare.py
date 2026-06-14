"""compare subcommand — read-only drift report (live vs tracked) for a profile."""

from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.syntax import Syntax

from setforge import compare as compare_mod
from setforge import transitions
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
    _refuse_legacy_live_markers,
)
from setforge.cli._install_helpers import _load_validated_host_local_sections
from setforge.cli._output import render
from setforge.compare import CompareStatus, load_ignored_orphans, resolve_dst
from setforge.config import (
    Config,
    OrphanOverlay,
    apply_host_local_tracked_file_overrides,
    apply_local_overlay,
    collect_orphan_overlays,
    load_config,
    resolve_profile,
)
from setforge.host_local_inject import HOST_LOCAL_PROVENANCE_TAG
from setforge.locking import profile_lock
from setforge.sections import extract_sections
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
    # Surface local.yaml overlay entries the apply site silently skipped
    # (unknown id / off-profile id). Read-only diagnosis — collected from
    # the same overlay block, classified against cfg.tracked_files and the
    # resolved profile's tracked_files list.
    orphan_overlays = collect_orphan_overlays(cfg, resolved)
    # Apply local.yaml plugin/extension/marketplace overlay (SPEC 2).
    # Mutates resolved and cfg in place; the resolved
    # provenance lists drive the host-overlay block printed below the
    # drift report (cf. render_local_overlay_block in setforge.compare).
    overlay_resolution = apply_local_overlay(cfg, resolved, profile)
    profile_ctx = ProfileContext(
        cfg=cfg, resolved=resolved, repo_root=repo_root, profile=profile
    )
    _refuse_legacy_live_markers(profile_ctx, command="compare")
    _refuse_duplicate_section_names(profile_ctx, command="compare")

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
        _render_compare_report(
            report, console, full_diff=full_diff, orphan_overlays=orphan_overlays
        )
        # SPEC 1 mockup: surface every host-local section
        # the install would inject, tagged with the canonical provenance
        # marker (HOST_LOCAL_PROVENANCE_TAG). Lives BELOW the drift
        # summary so the diff body and per-status counts stay grouped,
        # mirroring the mockup's ordering ("✓ no drift ... + <tag> X").
        _render_host_local_preview(host_local_sections_map, cfg, console)

    render(
        ctx.obj,
        "compare",
        _compare_json_data(report, orphan_overlays),
        human_fn=_human,
    )

    if check:
        if strict:
            if any(e.status == CompareStatus.DRIFTED for e in report.entries):
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
    value), ``disposition`` (string or null), ``drift_class`` (string or
    null — null unless DRIFTED), ``reason`` (string or null),
    ``span_only_drift`` (bool), ``forked_scalar_conflicts`` (list of
    pre-rendered ``path: base → tracked | live`` strings, non-empty iff
    ``drift_class`` is ``conflicted``), ``drift_is_expected`` (bool,
    derived). No
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
            "disposition": entry.disposition.value
            if entry.disposition is not None
            else None,
            "drift_class": entry.drift_class.value
            if entry.drift_class is not None
            else None,
            "reason": entry.reason,
            "span_only_drift": entry.span_only_drift,
            "forked_scalar_conflicts": list(entry.forked_scalar_conflicts),
            "drift_is_expected": entry.drift_is_expected,
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
