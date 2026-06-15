"""``cleanup-orphans`` subcommand — review and remove tracked-file orphans.

An orphan is a live path setforge previously deployed (per a
``transitions/*/meta.json`` ``paths`` field) that is no longer listed
in any resolved tracked_files entry. The subcommand has three modes:

- default (no ``--apply``) — dry-run; print ``WOULD remove`` lines.
- ``--apply`` + TTY — arrow-key wizard with three choices: abort /
  delete only (irreversible) / delete + write transition (revert-able).
- ``--apply`` + non-TTY + no ``--yes`` — raises
  :class:`OrphanCleanupRequiresInteractive` (mutate-gate pattern).
- ``--apply --yes`` — defaults to the safe revert-able branch.

``--ignore <id>`` appends a tracked_file identifier to
``~/.config/setforge/local.yaml``'s ``orphan_ignore`` list so future
runs skip the corresponding orphan. The tracked ``setforge.yaml`` is
NEVER mutated — orphan-ignore is strictly a host-local decision.
"""

from __future__ import annotations

import os
import stat as stat_mod
import sys
from enum import StrEnum
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from ruamel.yaml import YAML

from setforge import compare as compare_mod
from setforge import transitions
from setforge.binaries import LOCAL_CONFIG_PATH
from setforge.cli import (
    _CONFIG_OPTION,
    _PROFILE_OPTION,
    _resolve_config_arg,
    app,
)
from setforge.cli._help_examples import CLEANUP_ORPHANS_EXAMPLES
from setforge.compare import OrphanDetection, OrphanEntry, load_ignored_orphans
from setforge.config import load_config
from setforge.errors import OrphanCleanupRequiresInteractive

__all__ = [
    "ApplyChoice",
    "cleanup_orphans",
]

# ``prompt_toolkit.shortcuts.radiolist_dialog`` is imported lazily via the
# module-level ``__getattr__`` below so non-interactive callers never pay
# the ~140ms cost. The TUI fires only when ``apply=True`` and stdin is a
# TTY. Module-level ``__getattr__`` keeps the attribute-on-module path
# that tests' ``monkeypatch.setattr("setforge.cli.orphans.radiolist_dialog", ...)``
# relies on (same pattern as :mod:`setforge.cli._confirm`).


def __getattr__(name: str) -> Any:  # noqa: ANN401 — PEP 562 module hook returns Any
    if name == "radiolist_dialog":
        from prompt_toolkit.shortcuts import radiolist_dialog

        return radiolist_dialog
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class ApplyChoice(StrEnum):
    """User's choice from the ``--apply`` arrow-key wizard.

    - ``ABORT`` — default safe; no mutations.
    - ``DELETE_ONLY`` — unlink each orphan; NO transition record;
      irreversible.
    - ``DELETE_AND_TRANSITION`` — write a transition record FIRST
      (capturing pre-delete content), then unlink; recoverable via
      ``setforge revert``.
    """

    ABORT = "abort"
    DELETE_ONLY = "delete-only"
    DELETE_AND_TRANSITION = "delete-and-transition"


def _append_ignored_orphan(ignore_id: str) -> None:
    """Append ``ignore_id`` to ``orphan_ignore:`` in :data:`LOCAL_CONFIG_PATH`.

    Uses ruamel.yaml's round-trip loader so existing comments and key
    ordering survive. Creates the file (with parent dirs) when absent.
    Idempotent — re-adding an existing id is a no-op.
    """
    yaml = YAML(typ="rt")
    LOCAL_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LOCAL_CONFIG_PATH.exists():
        data = yaml.load(LOCAL_CONFIG_PATH.read_text(encoding="utf-8"))
    else:
        data = None
    if not isinstance(data, dict):
        data = {}
    raw = data.get("orphan_ignore")
    if not isinstance(raw, list):
        raw = []
    if ignore_id in raw:
        return
    raw.append(ignore_id)
    data["orphan_ignore"] = raw
    with LOCAL_CONFIG_PATH.open("w", encoding="utf-8") as fh:
        yaml.dump(data, fh)


def _print_skip_note(
    console: Console,
    *,
    skipped_absent: int,
    skipped_source: int,
    skipped_unmanaged: int,
) -> None:
    """Print a one-line note when the detection guards filtered candidates.

    Suppressed when nothing was filtered. For a destructive tool the
    count is a trust signal — it explains why a previously-touched path
    is absent from the WOULD-delete list (gone from disk, a tracked
    source that can never be an orphan, or a path outside every
    currently-managed destination root).
    """
    total = skipped_absent + skipped_source + skipped_unmanaged
    if total == 0:
        return
    console.print(
        f"note: skipped {total} previously-touched path(s) — "
        f"{skipped_absent} no longer on disk, {skipped_source} tracked source, "
        f"{skipped_unmanaged} unmanaged"
    )


def _print_dry_run(
    orphans: list[OrphanEntry],
    console: Console,
    *,
    skipped_absent: int = 0,
    skipped_source: int = 0,
    skipped_unmanaged: int = 0,
) -> None:
    """Print the default-mode dry-run output."""
    if not orphans:
        console.print("=== no orphans ===")
    else:
        console.print("=== DRY-RUN — nothing will be deleted ===")
        for orphan in orphans:
            console.print(f"WOULD delete  {orphan.path}")
        console.print("=== rerun with --apply to delete ===")
    _print_skip_note(
        console,
        skipped_absent=skipped_absent,
        skipped_source=skipped_source,
        skipped_unmanaged=skipped_unmanaged,
    )


def _detect_orphans_live(
    profile: str, config_path: Path
) -> tuple[Any, OrphanDetection]:
    """Re-detect orphans live for the apply path.

    Returns ``(cfg, detection)`` — ``detection`` carries the kept
    orphans plus the guard skip tallies that feed the dry-run
    transparency note. The cfg is re-loaded inside the call so callers
    cannot accidentally pass a stale snapshot from a prior ``compare``
    invocation. Catches the "stale snapshot deletes re-added file" race
    called out in the SPEC 2 anti-pattern checks.
    """
    cfg = load_config(config_path)
    report = compare_mod.compare_profile(
        cfg,
        profile,
        config_path.resolve().parent,
        transitions_dir=transitions.transitions_root(),
        ignored=load_ignored_orphans(),
    )
    detection = OrphanDetection(
        orphans=report.orphans,
        skipped_absent=report.orphan_skipped_absent,
        skipped_source=report.orphan_skipped_source,
        skipped_unmanaged=report.orphan_skipped_unmanaged,
    )
    return cfg, detection


def _pick_cleanup_branch(*, yes: bool) -> ApplyChoice:
    """Pick the cleanup branch under ``--apply``.

    - ``yes=True`` → :attr:`ApplyChoice.DELETE_AND_TRANSITION` (safe
      revert-able default per SPEC 2).
    - non-TTY + ``yes=False`` → raise
      :class:`OrphanCleanupRequiresInteractive` (mutate-gate).
    - TTY + ``yes=False`` → arrow-key wizard; Esc → ABORT.
    """
    if yes:
        return ApplyChoice.DELETE_AND_TRANSITION
    if not sys.stdin.isatty():
        raise OrphanCleanupRequiresInteractive(
            "setforge cleanup-orphans --apply requires --yes when stdin is not a TTY"
        )
    # Lazy import resolves via module-level ``__getattr__`` (tests
    # monkeypatch the same attribute path).
    from setforge.cli import orphans as _self

    choice = _self.radiolist_dialog(
        title="setforge cleanup-orphans",
        text="What would you like to do?",
        values=[
            (ApplyChoice.ABORT, "no, abort (default)"),
            (ApplyChoice.DELETE_ONLY, "yes, delete the listed paths (NOT revert-able)"),
            (
                ApplyChoice.DELETE_AND_TRANSITION,
                "yes + write transition for revert",
            ),
        ],
        default=ApplyChoice.ABORT,
    ).run()
    if choice is None:
        return ApplyChoice.ABORT
    return choice


def _lstat_safe(path: Path) -> os.stat_result | None:
    """Return ``path.lstat()`` or ``None`` if missing.

    Uses ``lstat`` (not ``stat``) so symlink orphans are detected as
    symlinks without dereferencing — never call ``resolve()`` before
    unlinking (would torch the user's pointed-to file or directory).
    """
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _read_orphan_content(path: Path) -> str | None:
    """Snapshot ``path``'s content for the transition record.

    Symlinks record as ``None`` (no body for the transition patch —
    revert recreates the link by re-deploying from tracked). Regular
    files read as UTF-8; binary content not supported (matches
    :func:`setforge.transitions.snapshot_paths`).
    """
    info = _lstat_safe(path)
    if info is None:
        return None
    if stat_mod.S_ISLNK(info.st_mode):
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _write_orphan_transition(profile: str, orphans: list[OrphanEntry]) -> Path:
    """Write a transition record capturing pre-delete content for revert.

    Builds ``file_pre`` from each orphan's current content (or ``None``
    for symlinks / unreadable files) and ``file_post`` mapping every
    path to ``None`` — the existing convention for deletions in
    :func:`setforge.transitions.write_transition`. Writes BEFORE any
    unlink so a crash mid-cleanup still leaves the user a recoverable
    state.
    """
    meta = transitions.make_meta(transitions.TransitionCommand.CLEANUP_ORPHANS, profile)
    file_pre: dict[Path, str | None] = {
        orphan.path: _read_orphan_content(orphan.path) for orphan in orphans
    }
    file_post: dict[Path, str | None] = {orphan.path: None for orphan in orphans}
    return transitions.write_transition(
        meta,
        file_pre,
        file_post,
        ext_delta=None,
    )


def _unlink_orphan_path(path: Path, console: Console) -> None:
    """Remove one orphan (file or symlink).

    Symlinks: ``unlink()`` removes the link, never the target. NEVER
    ``resolve()`` before unlink — that would point at the user's data.
    Regular files: ``unlink()`` straight. Directories: NOT handled
    here (cleanup walks empty parents via :func:`_rmdir_empty_parents`
    AFTER all file deletes).

    Missing path → log warning + return (a race between detection and
    apply; user re-added the file, removed it manually, or the
    meta.json snapshot was stale). NEVER use ``unlink(missing_ok=True)``
    — swallowing the race is the bug.
    """
    info = _lstat_safe(path)
    if info is None:
        console.print(
            f"[yellow]warning:[/yellow] orphan vanished before delete: {path}"
        )
        return
    if stat_mod.S_ISDIR(info.st_mode):
        # Directories are deleted post-file by _rmdir_empty_parents.
        return
    path.unlink()
    console.print(f"  deleted  {path}")


def _rmdir_empty_parents(parents: list[Path], console: Console) -> None:
    """Remove now-empty parent dirs, single-level only.

    For every parent, check ``not any(p.iterdir())`` and call
    ``Path.rmdir()`` once. NEVER ``os.removedirs`` (walks up, can nuke
    shared parents) and NEVER ``shutil.rmtree`` (recursive delete, would
    torch unrelated files). Each unique parent is attempted exactly
    once.
    """
    seen: set[Path] = set()
    for parent in parents:
        if parent in seen:
            continue
        seen.add(parent)
        if not parent.exists():
            continue
        try:
            if not any(parent.iterdir()):
                parent.rmdir()
                console.print(f"  deleted  {parent}/   (empty)")
        except OSError as exc:
            console.print(
                f"[yellow]warning:[/yellow] could not remove empty dir {parent}: {exc}"
            )


def _execute_cleanup(
    profile: str,
    orphans: list[OrphanEntry],
    choice: ApplyChoice,
    console: Console,
) -> None:
    """Execute the chosen cleanup branch over a pre-detected ``orphans`` list.

    For :attr:`ApplyChoice.DELETE_AND_TRANSITION` the transition record
    is written FIRST (before any unlink), so a crash between leaves a
    recoverable state. For :attr:`ApplyChoice.DELETE_ONLY` no
    transition is written and the deletes are irreversible. The
    :attr:`ApplyChoice.ABORT` branch is handled by the caller (no
    mutation, no console line beyond the abort marker).
    """
    transitions.ensure_state_dir_writable()
    wrote_transition = False
    if choice is ApplyChoice.DELETE_AND_TRANSITION:
        transition_dir = _write_orphan_transition(profile, orphans)
        console.print(f"  transition: {transition_dir}")
        wrote_transition = True

    console.print("=== orphan cleanup ===")
    for orphan in orphans:
        _unlink_orphan_path(orphan.path, console)
    _rmdir_empty_parents([o.path.parent for o in orphans], console)
    if wrote_transition:
        console.print(f"  to undo: setforge revert --profile={profile}")


def _apply_orphan_cleanup(
    profile: str,
    config_path: Path,
    *,
    yes: bool,
    console: Console,
) -> None:
    """Entry-point for the ``--apply`` code path.

    RE-COMPUTES orphans live via ``compare_mod.compare_profile`` (which
    dispatches to ``compare_mod.detect_orphans``) on every invocation —
    NEVER reuses a cached snapshot from a prior ``compare`` call.
    Catches the "stale snapshot deletes re-added file" race called out
    in the SPEC 2 anti-pattern checks. The literal AST acceptance
    command in the SPEC walks this function for a ``compare_profile``
    OR ``detect_orphans`` attribute-style call site; both are wired
    here.
    """
    cfg = load_config(config_path)
    report = compare_mod.compare_profile(
        cfg,
        profile,
        config_path.resolve().parent,
        transitions_dir=transitions.transitions_root(),
        ignored=load_ignored_orphans(),
    )
    orphans = report.orphans
    if not orphans:
        console.print("=== no orphans ===")
        return

    choice = _pick_cleanup_branch(yes=yes)
    if choice is ApplyChoice.ABORT:
        console.print("[red]✗ aborted[/red] — no orphans deleted")
        return

    _execute_cleanup(profile, orphans, choice, console)


@app.command("cleanup-orphans", epilog=CLEANUP_ORPHANS_EXAMPLES)
def cleanup_orphans(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Actually delete orphans. Without this, the command is a dry-run.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help=(
            "Skip the arrow-key wizard and default to the safe "
            "revert-able branch (delete + write transition). Required "
            "for non-interactive contexts when --apply is set."
        ),
    ),
    ignore: str | None = typer.Option(
        None,
        "--ignore",
        help=(
            "Tracked_file id to add to ~/.config/setforge/local.yaml "
            "'orphan_ignore:' so its destination is excluded from "
            "future orphan detection. Mutates host-local config only; "
            "the tracked setforge.yaml is never touched."
        ),
    ),
) -> None:
    """Review and remove tracked-file orphans for ``profile``.

    Default is dry-run; pass ``--apply`` to mutate. ``--apply`` + TTY
    fires an arrow-key wizard (abort / delete-only / delete + write
    transition). ``--apply`` + non-TTY + no ``--yes`` raises
    :class:`OrphanCleanupRequiresInteractive`. ``--apply --yes``
    short-circuits to the safe revert-able branch.

    ``--ignore <id>`` appends to the host-local ignore list and
    returns without scanning — useful for one-shot manual exclusion
    without scanning the transitions dir.
    """
    resolved_config = _resolve_config_arg(config)
    console = Console(stderr=True)

    if ignore is not None:
        _append_ignored_orphan(ignore)
        console.print(
            f"added [cyan]{ignore}[/cyan] to orphan_ignore in {LOCAL_CONFIG_PATH}"
        )
        return

    if not apply:
        _, detection = _detect_orphans_live(profile, resolved_config)
        _print_dry_run(
            detection.orphans,
            console,
            skipped_absent=detection.skipped_absent,
            skipped_source=detection.skipped_source,
            skipped_unmanaged=detection.skipped_unmanaged,
        )
        return

    _apply_orphan_cleanup(profile, resolved_config, yes=yes, console=console)
