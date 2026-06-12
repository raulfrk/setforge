"""Section-marker helpers shared by install / compare / sync subcommands.

No ``app`` import and no ``@app.command()`` decorator registrations.
Two flavors of I/O run through this module: (1) the live-file reads
the legacy-marker-refuse / section-decisions / live-sections-extract
flow performs, and (2) the directory walks ``expand_tracked_file`` runs
for tracked entries whose ``src`` is a directory — both helpers below
that delegate to ``expand_tracked_file`` (``_iter_section_tracked_files``,
``_iter_all_tracked_files``) inherit that walk cost.
"""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import typer

from setforge import section_reconcile, section_wizard
from setforge import sections as sections_mod
from setforge.capture import CaptureAuto
from setforge.compare import (
    CompareReport,
    CompareStatus,
    FileCompare,
    expand_tracked_file,
    resolve_dst,
    resolve_src,
)
from setforge.config import Config, ResolvedProfile, TrackedFile
from setforge.errors import SetforgeError
from setforge.section_reconcile import SectionDrift, SectionDriftState
from setforge.section_wizard import ReconcileAuto, SectionAction
from setforge.sections import (
    SectionSemantics,
    detect_legacy_markers,
    detect_legacy_namespace_markers,
)


@dataclass(slots=True, frozen=True)
class ProfileContext:
    """Bundle the ``(cfg, resolved, repo_root, profile)`` data clump.

    Every subcommand's helper chain in ``install`` / ``sync`` / ``compare``
    needs the parsed :class:`Config`, the resolved profile, the absolute
    config-repo root, and the profile name; threading them as four
    positional arguments across 8+ signatures was the canonical data
    clump. Callers build a single :class:`ProfileContext` once at the
    command entry point and pass it through every subsequent helper.

    The dataclass is frozen + slotted so it stays a cheap value object;
    helpers that need only a subset of fields still receive the same
    context and reach for the field they need (``ctx.cfg``,
    ``ctx.resolved``, etc.).
    """

    cfg: Config
    resolved: ResolvedProfile
    repo_root: Path
    profile: str


def _parse_capture_auto(auto: str | None) -> CaptureAuto | None:
    """Validate and parse ``--auto=`` for the capture-side flow.

    Raises :class:`typer.Exit(2)` with a user-visible error if ``auto``
    is neither ``"use-live"`` nor ``"keep-tracked"``. Shared by
    ``capture`` and ``sync`` so the parse-and-validate pattern stays in
    one place.
    """
    if auto is None:
        return None
    try:
        return CaptureAuto(auto)
    except ValueError:
        typer.secho(
            f"error: --auto must be 'use-live' or 'keep-tracked' (got {auto!r})",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(2) from None


def _parse_section_auto(
    auto_value: str | None, reconcile_user_sections: bool
) -> ReconcileAuto | None:
    """Validate and parse ``--auto=`` against ``--reconcile-user-sections``.

    Raises :class:`typer.Exit(2)` for the mutual-exclusivity violation
    and for unknown ``--auto`` values, matching the existing
    ``sync --auto`` error pattern.
    """
    if reconcile_user_sections and auto_value is not None:
        typer.secho(
            "error: --reconcile-user-sections and --auto are mutually exclusive",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(2)
    if auto_value is None:
        return None
    try:
        return ReconcileAuto(auto_value)
    except ValueError:
        typer.secho(
            f"error: --auto must be 'use-tracked' or 'keep-live' (got {auto_value!r})",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(2) from None


def _iter_section_tracked_files(
    ctx: ProfileContext,
) -> Iterator[tuple[Path, Path]]:
    """Yield ``(sub_src, sub_dst)`` for every section-bearing tracked_file.

    Encapsulates the resolve_src / resolve_dst / expand_tracked_file /
    ``preserve_user_sections`` filter chain that
    :func:`_resolve_section_decisions`, :func:`_refuse_legacy_live_markers`,
    and :func:`_extract_live_sections_map` all duplicate today.
    ``expand_tracked_file`` runs ``Path.rglob`` for directory-shaped
    tracked entries (a per-call filesystem walk); for plain-file entries
    the helper is allocation-only. Callers that need the live or tracked
    text read it themselves so the generator's iteration cost stays
    O(N) in the tracked-file count regardless of file sizes.

    Callers that only need ``sub_dst`` destructure as ``_, sub_dst``.
    """
    # The legacy preserve_user_sections section-reconcile model was retired at
    # schema 2.0 (shared sections now ride disposition: shared + section spans).
    # No tracked_file carries the legacy flag any more, so this iterator yields
    # nothing — the section-reconcile callers become inert. The bare ``yield``
    # after ``return`` keeps the function a generator (so callers can iterate it)
    # while emitting no items.
    return
    yield  # type: ignore[unreachable]


def _iter_all_tracked_files(
    ctx: ProfileContext,
) -> Iterator[tuple[TrackedFile, str, Path, Path]]:
    """Yield ``(tracked_file, sub_name, sub_src, sub_dst)`` per resolved entry.

    Sibling of :func:`_iter_section_tracked_files` without the
    ``preserve_user_sections`` filter; consolidates the unfiltered
    resolve_src / resolve_dst / expand_tracked_file walks that ``install``
    (transition snapshot + copy_atomic loop) and ``sync`` (transition
    snapshot) all duplicate today. Yields ``tracked_file`` alongside the
    ``expand_tracked_file`` synthetic ``sub_name`` (``name`` for plain
    files, ``name/relpath`` for directory entries) and the path pair
    because the install copy_atomic caller needs per-tracked_file
    ``preserve_user_*`` attributes; callers that only need a path
    destructure as ``_, _, _, sub_dst`` or ``_, _, sub_src, _``.
    """
    for name in ctx.resolved.tracked_files:
        tracked_file = ctx.cfg.tracked_files[name]
        src = resolve_src(tracked_file, ctx.repo_root)
        dst = resolve_dst(tracked_file)
        for sub_name, sub_src, sub_dst in expand_tracked_file(name, src, dst):
            yield tracked_file, sub_name, sub_src, sub_dst


def _resolve_drift_paths(
    drift_report: CompareReport,
    ctx: ProfileContext,
) -> list[tuple[FileCompare, Path, Path]]:
    """Join ``drift_report.entries`` to tracked-file ``(sub_src, sub_dst)`` paths.

    Both ``install._build_unexpected_drift_plan`` and
    ``sync._build_capture_plan`` need the same ``name → (sub_src, sub_dst)``
    map keyed by the ``expand_tracked_file`` synthetic ``sub_name`` — the
    exact string that becomes ``FileCompare.name`` — so directory sub-files
    (``name/relpath``) do not collide on a bare basename. Returns one
    ``(entry, sub_src, sub_dst)`` tuple per DRIFTED
    entry with drift content (``diff`` or ``mode_drift`` non-empty).
    Entries with no path match fall back to the entry name in both
    positions, preserving the pre-extraction behavior.
    """
    paths_by_name: dict[str, tuple[Path, Path]] = {}
    for _tracked_file, sub_name, sub_src, sub_dst in _iter_all_tracked_files(ctx):
        # ``sub_name`` is expand_tracked_file's synthetic name — ``name``
        # for plain files, ``name/relpath`` for directory entries — and is
        # exactly what compare_profile stores in ``FileCompare.name``. Keying
        # by it gives one unique entry per sub-file, so directory sub-files no
        # longer overwrite each other on a shared basename.
        paths_by_name[sub_name] = (sub_src, sub_dst)
    resolved_entries: list[tuple[FileCompare, Path, Path]] = []
    for entry in drift_report.entries:
        if entry.status is not CompareStatus.DRIFTED:
            continue
        if not (entry.diff or entry.mode_drift):
            continue
        paths = paths_by_name.get(entry.name)
        if paths is None:
            sub_src = Path(entry.name)
            sub_dst = Path(entry.name)
        else:
            sub_src, sub_dst = paths
        resolved_entries.append((entry, sub_src, sub_dst))
    return resolved_entries


def _refuse_legacy_live_markers(ctx: ProfileContext, *, command: str) -> None:
    """Raise :class:`SetforgeError` if any live ``preserve_user_sections``
    file carries pre-hash markers.

    Walks every tracked_file in ``resolved`` whose tracked entry has
    ``preserve_user_sections=True`` and runs
    :func:`setforge.sections.detect_legacy_markers` on the live file (when
    it exists). The strict parser would otherwise raise
    :class:`setforge.errors.MarkerError` partway through the read-only /
    capture flow with an opaque ``line N: missing required keyword``
    message; this surfaces a single actionable error before any strict
    parse happens, pointing the user at ``setforge install`` to migrate.

    ``command`` is the user-facing command name (``compare`` / ``sync`` /
    ``merge``) used in the error message so the user sees which entry
    point refused. Install must NOT call this — install's job is to
    migrate.
    """
    for _, sub_dst in _iter_section_tracked_files(ctx):
        try:
            live_text = sub_dst.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        if detect_legacy_namespace_markers(live_text):
            raise SetforgeError(
                f"{sub_dst}: legacy 'my-setup:user-section' marker namespace "
                f"detected (pre-rename). The post-rename "
                f"parser does not recognize these markers, so 'setforge "
                f"{command}' would silently drop host-local section bodies. "
                f"Migrate the file in place with:\n"
                f"  sed -i 's/my-setup:user-section/setforge:user-section/g' "
                f"{sub_dst}"
            )
        if detect_legacy_markers(live_text):
            raise SetforgeError(
                f"{sub_dst}: legacy user-section marker format detected "
                f"(pre-hash markers without 'host-local'/'shared' keyword "
                f"or 'hash=<sha256>' segment). 'setforge {command}' is "
                f"strict on live-side markers. Run "
                f"'uv run setforge install --profile=<name>' first to "
                f"migrate the file in place."
            )


def _warn_shared_drift(sub_dst: Path, drifts: Mapping[str, SectionDrift]) -> None:
    """Emit the bare-install drift warning for one tracked_file.

    Routes by worst state present across the file's ``shared`` sections:

    - any :attr:`SectionDriftState.CONFLICT` present → loud
      ``WARNING: ... CONFLICT — ...`` line in RED + bold (genuine three-way
      divergence; user attention warranted before the next install).
    - otherwise, at least one non-:attr:`NO_DRIFT` shared section
      (``PENDING_TRACKED`` / ``LIVE_EDITED`` / ``LEGACY`` / ``INCONSISTENT``)
      → regular ``warning: ...`` line in YELLOW (today's behaviour, kept
      for the non-CONFLICT states).
    - no shared drift → silent (host-local-only drift never warns).
    """
    shared_drifts = [
        d
        for d in drifts.values()
        if d.semantics is SectionSemantics.SHARED
        and d.state is not SectionDriftState.NO_DRIFT
    ]
    if not shared_drifts:
        return
    tail = "(re-run with --reconcile-user-sections or --auto=use-tracked)"
    conflict_drifts = [
        d for d in shared_drifts if d.state is SectionDriftState.CONFLICT
    ]
    if conflict_drifts:
        summary = section_wizard.format_drift_summary(conflict_drifts)
        typer.secho(
            f"WARNING: {sub_dst}: CONFLICT — {summary} {tail}",
            err=True,
            fg=typer.colors.RED,
            bold=True,
        )
    else:
        non_conflict_drifts = [
            d for d in shared_drifts if d.state is not SectionDriftState.CONFLICT
        ]
        summary = section_wizard.format_drift_summary(non_conflict_drifts)
        typer.secho(
            f"warning: {sub_dst}: {summary} {tail}",
            err=True,
            fg=typer.colors.YELLOW,
        )


def _resolve_section_decisions(
    ctx: ProfileContext,
    *,
    section_auto: ReconcileAuto | None,
    interactive: bool,
) -> dict[Path, dict[str, str]]:
    """Walk every tracked_file with ``preserve_user_sections=True`` and run the
    section reconcile wizard, returning a ``{dst_path: {section_name: body}}``
    map the install loop forwards to :func:`deploy.copy_atomic`.

    Renders one bare-install warning per tracked_file that has any shared
    drift; surfacing the warnings here keeps the deploy loop a thin
    orchestrator. Tracked files without ``preserve_user_sections`` are
    silently skipped; their copy_atomic call gets an empty override.
    """
    decisions: dict[Path, dict[str, str]] = {}
    for sub_src, sub_dst in _iter_section_tracked_files(ctx):
        try:
            live_text = sub_dst.read_text(encoding="utf-8")
        except FileNotFoundError:
            # First install for this file — no live to reconcile.
            continue
        tracked_text = sub_src.read_text(encoding="utf-8")
        drifts = section_reconcile.classify_section_drift(tracked_text, live_text)
        if not drifts:
            continue
        if section_auto is None and not interactive:
            _warn_shared_drift(sub_dst, drifts)
        outcomes = section_wizard.reconcile_sections(
            drifts, auto=section_auto, interactive=interactive
        )
        sparse = {
            n: d.body
            for n, d in outcomes.items()
            if d.action in (SectionAction.TAKE_TRACKED, SectionAction.EDIT)
        }
        if sparse:
            decisions[sub_dst] = sparse
    return decisions


def _extract_live_sections_map(
    ctx: ProfileContext,
) -> dict[Path, sections_mod.LiveSections]:
    """Pre-extract live user-section bodies for every section-bearing tracked_file.

    Walks ``resolved.tracked_files``, and for each entry whose tracked_file has
    ``preserve_user_sections=True`` AND whose live file already exists,
    reads the live file once and stores the
    :class:`~sections_mod.LiveSections` produced by
    :func:`sections_mod.extract_live_sections` keyed by the live
    ``sub_dst`` path.

    The install loop passes the matching entry to ``deploy.copy_atomic``
    via ``precomputed_live_sections`` so ``_compute_content`` does not
    re-read + re-parse the same live file a second time. The factory
    routes through ``allow_legacy=True`` so pre-hash live files (untagged
    markers, no end-marker hash) flow through install's migration path;
    install is the verb that re-tags + stamps. Compare / sync use the
    strict parser and refuse legacy via :func:`_refuse_legacy_live_markers`.
    """
    live_sections: dict[Path, sections_mod.LiveSections] = {}
    for _, sub_dst in _iter_section_tracked_files(ctx):
        try:
            live_text = sub_dst.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        live_sections[sub_dst] = sections_mod.extract_live_sections(live_text)
    return live_sections
