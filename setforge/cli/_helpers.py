"""Section-marker helpers shared by install / compare / sync subcommands.

No ``app`` import and no ``@app.command()`` decorator registrations.
The directory walks ``expand_tracked_file`` runs for tracked entries whose
``src`` is a directory feed ``_iter_all_tracked_files`` below, which inherits
that walk cost.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import typer

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

if TYPE_CHECKING:
    from setforge.reconcile_apply import ReconcileAuto


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
    file_selection: frozenset[str] | None = None

    @property
    def file_profile(self) -> ResolvedProfile:
        if self.file_selection is None:
            return self.resolved
        return self.resolved.model_copy(
            update={
                "tracked_files": [
                    name
                    for name in self.resolved.tracked_files
                    if name in self.file_selection
                ]
            }
        )


def _parse_capture_auto(auto: str | None) -> CaptureAuto | None:
    """Validate and parse ``--auto=`` for the capture-side flow.

    Raises :class:`typer.Exit(2)` with a user-visible error if ``auto``
    is neither ``"use-live"`` nor ``"keep-tracked"``.
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
    from setforge.reconcile_apply import ReconcileAuto

    try:
        return ReconcileAuto(auto_value)
    except ValueError:
        typer.secho(
            f"error: --auto must be 'use-tracked' or 'keep-live' (got {auto_value!r})",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(2) from None


def _iter_all_tracked_files(
    ctx: ProfileContext,
) -> Iterator[tuple[TrackedFile, str, Path, Path]]:
    """Yield ``(tracked_file, sub_name, sub_src, sub_dst)`` per resolved entry.

    Consolidates the unfiltered
    resolve_src / resolve_dst / expand_tracked_file walks that ``install``
    (transition snapshot + deploy loop) and ``sync`` (transition
    snapshot) all duplicate today. Yields ``tracked_file`` alongside the
    ``expand_tracked_file`` synthetic ``sub_name`` (``name`` for plain
    files, ``name/relpath`` for directory entries) and the path pair
    because the install deploy caller needs per-tracked_file
    ``preserve_user_*`` attributes; callers that only need a path
    destructure as ``_, _, _, sub_dst`` or ``_, _, sub_src, _``.
    """
    for name in ctx.file_profile.tracked_files:
        tracked_file = ctx.cfg.tracked_files[name]
        if tracked_file.tree is not None:
            continue
        src = resolve_src(tracked_file, ctx.repo_root)
        dst = resolve_dst(tracked_file)
        for sub_name, sub_src, sub_dst in expand_tracked_file(name, src, dst):
            yield tracked_file, sub_name, sub_src, sub_dst


def _iter_all_trees(
    ctx: ProfileContext,
) -> Iterator[tuple[TrackedFile, str, Path, Path]]:
    """Yield one unexpanded root tuple for every explicit managed tree."""
    for name in ctx.file_profile.tracked_files:
        tracked_file = ctx.cfg.tracked_files[name]
        if tracked_file.tree is None:
            continue
        yield (
            tracked_file,
            name,
            resolve_src(tracked_file, ctx.repo_root),
            resolve_dst(tracked_file),
        )


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
