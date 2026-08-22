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
from setforge.errors import SetforgeError
from setforge.user_section_markers import detect_duplicate_section_names

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
    for name in ctx.resolved.tracked_files:
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
    for name in ctx.resolved.tracked_files:
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


def _refuse_duplicate_section_names(ctx: ProfileContext, *, command: str) -> None:
    """Raise :class:`SetforgeError` when a tracked/live markdown file repeats
    a user-section name across two start markers.

    Two ``<!-- setforge:user-section start ... NAME -->`` regions sharing one
    NAME used to collapse silently in the dict-keyed section primitives: only
    the last body survived, ``merge_sections`` spliced it into BOTH regions
    (the first region's distinct content permanently lost), and
    ``set_marker_hashes`` stamped one hash onto both end markers (corrupting
    the first region's ``hash=`` segment). The core parse/merge/hash
    primitives now raise :class:`~setforge.errors.MarkerError` on the second
    pair, but that surfaces partway through a strict parse as an opaque
    ``line N: duplicate user-section name 'NAME'`` message.

    This pre-check runs
    :func:`setforge.user_section_markers.detect_duplicate_section_names`
    (regex-only; no strict parse) on every line-based tracked_file's tracked
    SRC and live DST, raising a single user-actionable error naming the
    duplicated section BEFORE any strict parse happens. Structural files
    (JSON / JSONC / YAML) carry no inline markers and are skipped.

    ``command`` is the user-facing command name (``compare`` / ``sync`` /
    ``install``) used in the error message so the user sees which entry point
    refused.
    """
    from setforge import structural_merge

    for _tracked_file, _sub_name, sub_src, sub_dst in _iter_all_tracked_files(ctx):
        if structural_merge.is_structural(sub_dst):
            continue
        for role, path in (("tracked", sub_src), ("live", sub_dst)):
            try:
                text = path.read_text(encoding="utf-8")
            except (FileNotFoundError, IsADirectoryError):
                continue
            duplicate = detect_duplicate_section_names(text)
            if duplicate is None:
                continue
            raise SetforgeError(
                f"{path}: duplicate user-section name {duplicate!r} on the "
                f"{role} side. Two '<!-- setforge:user-section start ... "
                f"{duplicate} -->' regions share one name, so 'setforge "
                f"{command}' would silently collapse the first region's body "
                f"and corrupt its end-marker hash. Rename one of the two "
                f"sections so every user-section name is unique."
            )
