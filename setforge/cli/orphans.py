"""``cleanup-orphans`` subcommand — review and remove tracked-file orphans.

An orphan is a live path setforge previously deployed (per a
``transitions/*/meta.json`` ``paths`` field) that is no longer listed
in any resolved tracked_files entry. ``--scan`` is a separate explicit mode
for unrecorded leaves inside bounded currently-managed trees. The legacy
subcommand has four modes:

- default (no ``--apply``) — dry-run; print ``WOULD delete`` lines.
- ``--apply`` + TTY — arrow-key wizard with three choices: abort /
  delete only (irreversible) / delete + write transition (revert-able).
- ``--apply`` + non-TTY + no ``--yes`` — raises
  :class:`OrphanCleanupRequiresInteractive` (mutate-gate pattern).
- ``--apply --yes`` — defaults to the safe revert-able branch.

``--ignore <id>`` appends a tracked_file identifier to
``~/.config/setforge/local.yaml``'s ``orphan_ignore`` list so future
runs skip the corresponding orphan. The tracked ``setforge.yaml`` is
NEVER mutated — orphan-ignore is strictly a host-local decision.

``--scan --apply`` requires a TTY and asks separately for each candidate,
defaulting to keep. It rejects ``--yes`` and never removes parent directories.
"""

from __future__ import annotations

import os
import sys
from enum import StrEnum
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from ruamel.yaml import YAML

from setforge import compare as compare_mod
from setforge import operations, orphan_scan, transitions
from setforge.binaries import LOCAL_CONFIG_PATH
from setforge.cli import (
    _CONFIG_OPTION,
    _PROFILE_OPTION,
    _resolve_config_arg,
    app,
)
from setforge.cli._help_examples import CLEANUP_ORPHANS_EXAMPLES
from setforge.compare import OrphanDetection, OrphanEntry, load_ignored_orphans
from setforge.config import load_config, resolve_effective_profile
from setforge.errors import OrphanCleanupRequiresInteractive, SetforgeError
from setforge.locking import mutation_locks

__all__ = [
    "ApplyChoice",
    "cleanup_orphans",
]


def __getattr__(name: str) -> Any:  # noqa: ANN401 — PEP 562 module hook returns Any
    if name == "button_bar":
        from setforge.ui.widgets import button_bar

        return button_bar
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
    skipped_host_local: int = 0,
) -> None:
    """Print a one-line note when the detection guards filtered candidates.

    Suppressed when nothing was filtered. For a destructive tool the
    count is a trust signal — it explains why a previously-touched path
    is absent from the WOULD-delete list (gone from disk, a tracked
    source that can never be an orphan, a path outside every
    currently-managed destination root, or a setforge-written host-local
    file that must never be reaped).
    """
    total = skipped_absent + skipped_source + skipped_unmanaged + skipped_host_local
    if total == 0:
        return
    console.print(
        f"note: skipped {total} previously-touched path(s) — "
        f"{skipped_absent} no longer on disk, {skipped_source} tracked source, "
        f"{skipped_unmanaged} unmanaged, {skipped_host_local} host-local"
    )


def _print_dry_run(
    orphans: list[OrphanEntry],
    console: Console,
    *,
    skipped_absent: int = 0,
    skipped_source: int = 0,
    skipped_unmanaged: int = 0,
    skipped_host_local: int = 0,
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
        skipped_host_local=skipped_host_local,
    )


def _print_scan_dry_run(result: orphan_scan.ScanResult, console: Console) -> None:
    """Render unrecorded scan candidates without implying attribution."""
    if not result.entries:
        console.print("=== no unrecorded managed-tree candidates ===")
    else:
        console.print(
            "=== unrecorded managed-tree candidates — nothing will be deleted ==="
        )
        for entry in result.entries:
            suffix = (
                f" -> {entry.link_target}"
                if entry.kind is orphan_scan.ScanEntryKind.SYMLINK
                else ""
            )
            console.print(f"REVIEW  {entry.kind.value:<7}  {entry.path}{suffix}")
        console.print("=== rerun with --scan --apply to review each path ===")
    skipped = result.skipped_unsupported + result.skipped_mounts
    if skipped:
        console.print(
            f"note: skipped {skipped} path(s) — "
            f"{result.skipped_unsupported} unsupported type, "
            f"{result.skipped_mounts} mounted subtree"
        )


def _detect_orphans_live(
    profile: str, config_path: Path
) -> tuple[Any, OrphanDetection]:
    """Resolve the effective profile and re-detect orphans from live state.

    Returns ``(cfg, detection)`` — ``detection`` carries the kept
    orphans plus the guard skip tallies that feed the dry-run
    transparency note. Shared config and host-local overlays are re-loaded
    inside the call so dry-run and apply cannot reuse a stale or shared-only
    snapshot.
    """
    cfg = load_config(config_path)
    repo_root = config_path.resolve().parent
    resolved = resolve_effective_profile(cfg, profile, repo_root).resolved
    # Orphan detection consumes only the report's orphan projection; staged
    # drift classification is irrelevant here and must not acquire the
    # checkout-identity lock from inside cleanup's mutation lock scope.
    ownership_authorized = {
        sub_name: True
        for name in resolved.tracked_files
        for sub_name, _src, _dst in compare_mod.expand_tracked_file(
            name,
            compare_mod.resolve_src(cfg.tracked_files[name], repo_root),
            compare_mod.resolve_dst(cfg.tracked_files[name]),
        )
    }
    report = compare_mod.compare_profile(
        cfg,
        profile,
        repo_root,
        transitions_dir=transitions.transitions_root(),
        ignored=load_ignored_orphans(),
        ownership_authorized=ownership_authorized,
    )
    detection = OrphanDetection(
        orphans=report.orphans,
        skipped_absent=report.orphan_skipped_absent,
        skipped_source=report.orphan_skipped_source,
        skipped_unmanaged=report.orphan_skipped_unmanaged,
        skipped_host_local=report.orphan_skipped_host_local,
    )
    return cfg, detection


def _detect_scan_live(
    profile: str, config_path: Path
) -> tuple[Any, orphan_scan.ScanResult]:
    """Reload config and scan every effective profile from current disk state."""
    cfg = load_config(config_path)
    repo_root = config_path.resolve().parent
    resolve_effective_profile(cfg, profile, repo_root)
    result = orphan_scan.scan_unrecorded_managed_tree(
        cfg,
        repo_root,
        config_path=config_path.resolve(),
        transitions_dir=transitions.transitions_root(),
    )
    return cfg, result


def _confirm_scan_entries(
    entries: tuple[orphan_scan.ScanEntry, ...], console: Console
) -> tuple[orphan_scan.ScanEntry, ...]:
    """Ask for separate, default-keep consent for every scan candidate."""
    if not sys.stdin.isatty():
        raise OrphanCleanupRequiresInteractive(
            "setforge cleanup-orphans --scan --apply requires a TTY for "
            "per-file confirmation"
        )
    approved: list[orphan_scan.ScanEntry] = []
    for entry in entries:
        suffix = (
            f" -> {entry.link_target}"
            if entry.kind is orphan_scan.ScanEntryKind.SYMLINK
            else ""
        )
        if typer.confirm(
            f"delete {entry.kind.value} {entry.path}{suffix}?",
            default=False,
        ):
            approved.append(entry)
        else:
            console.print(f"  kept     {entry.path}")
    return tuple(approved)


def _scan_path_guards(
    entries: tuple[orphan_scan.ScanEntry, ...],
) -> tuple[operations.PathGuard, ...]:
    return orphan_scan.capture_parent_path_guards(
        tuple(entry.path for entry in entries)
    )


def _execute_scan_cleanup(
    profile: str,
    config_path: Path,
    *,
    console: Console,
) -> None:
    """Apply only individually approved candidates surviving a locked re-scan."""
    _, initial = _detect_scan_live(profile, config_path)
    if not initial.entries:
        console.print("=== no unrecorded managed-tree candidates ===")
        return
    approved = _confirm_scan_entries(initial.entries, console)
    if not approved:
        console.print("=== no scan candidates approved ===")
        return
    approved_by_path = {entry.path: entry for entry in approved}
    with (
        mutation_locks(config_dir=config_path.resolve().parent, profile=profile),
        operations.recover_on_error(profile, "cleanup-orphans"),
    ):
        operations.refuse_active(profile)
        _, refreshed = _detect_scan_live(profile, config_path)
        selected = tuple(
            entry
            for entry in refreshed.entries
            if (approved_entry := approved_by_path.get(entry.path)) is not None
            and orphan_scan.approval_matches(approved_entry, entry)
        )
        if not selected:
            console.print("=== no approved scan candidates remain ===")
            return
        journal = operations.prepare(
            command="cleanup-orphans",
            profile=profile,
            config_dir=config_path.resolve().parent,
            resources_lock=False,
            command_line=("cleanup-orphans", "--scan", "--apply"),
            paths=tuple(entry.path for entry in selected),
            path_guards=_scan_path_guards(selected),
        )
        try:
            for entry in selected:
                orphan_scan.validate_approved_entry(entry)
        except BaseException as primary:
            try:
                operations.complete(journal)
            except BaseException as cleanup_error:
                primary.add_note(
                    "cleanup-orphans journal cleanup failed after a no-effect "
                    f"preflight refusal: {cleanup_error}"
                )
            raise
        deltas = transitions.filesystem_deletion_deltas(
            entry.path for entry in selected
        )
        for entry in selected:
            orphan_scan.validate_approved_entry(entry)
        console.print("=== unrecorded orphan cleanup ===")
        for index, entry in enumerate(selected, start=1):
            journal = operations.begin_checkpoint(
                journal,
                name=f"delete-unrecorded-path-{index}",
                kind=operations.CheckpointKind.REVERSIBLE,
                recovery=f"restore approved managed-tree candidate {entry.path}",
                paths=(entry.path,),
                restore_state=False,
                adapters=(),
            )
            if index == 1:
                transition_dir = _write_scan_transition(profile, deltas)
                console.print(f"  transition: {transition_dir}")
            orphan_scan.unlink_approved_entry(entry)
            console.print(f"  deleted  {entry.path}")
            journal = operations.finish_checkpoint(journal)
        operations.complete(journal)
        console.print(f"  to undo: setforge revert --profile={profile}")


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
    from setforge.ui.widgets import CANCEL, Button

    choice = _self.button_bar(
        [
            Button("no, abort (default)", ApplyChoice.ABORT),
            Button(
                "yes, delete the listed paths (NOT revert-able)",
                ApplyChoice.DELETE_ONLY,
            ),
            Button(
                "yes + write transition for revert",
                ApplyChoice.DELETE_AND_TRANSITION,
            ),
        ],
        title="setforge cleanup-orphans",
        body="What would you like to do?",
        initial=0,
    )
    if choice is CANCEL:
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


def _write_orphan_transition(
    profile: str, deltas: tuple[transitions.FilesystemDelta, ...]
) -> Path:
    """Write an arbitrary-byte, type-preserving deletion transition."""
    meta = transitions.make_meta(transitions.TransitionCommand.CLEANUP_ORPHANS, profile)
    return transitions.write_transition(
        meta,
        {},
        {},
        ext_delta=None,
        filesystem_deltas=deltas,
    )


def _write_scan_transition(
    profile: str, deltas: tuple[transitions.FilesystemDelta, ...]
) -> Path:
    """Write the committed undo record for approved scan candidates."""
    meta = transitions.make_meta(transitions.TransitionCommand.CLEANUP_ORPHANS, profile)
    return transitions.write_transition(
        meta,
        {},
        {},
        ext_delta=None,
        filesystem_deltas=deltas,
    )


def _unlink_orphan_path(path: Path, console: Console) -> None:
    """Freeze and descriptor-unlink one legacy orphan (file or symlink).

    Symlinks: ``unlink()`` removes the link, never the target. NEVER
    ``resolve()`` before unlink — that would point at the user's data.
    Regular files: ``unlink()`` straight. Directories and parent
    directories are never removed: parent mutation is outside the
    frozen, journalled cleanup effect.

    Missing path → log warning + return (a race between detection and
    apply; user re-added the file, removed it manually, or the
    meta.json snapshot was stale). NEVER use ``unlink(missing_ok=True)``
    — swallowing the race is the bug.
    """
    if _lstat_safe(path) is None:
        console.print(
            f"[yellow]warning:[/yellow] orphan vanished before delete: {path}"
        )
        return
    entry = orphan_scan.freeze_candidate(path)
    orphan_scan.unlink_approved_entry(entry)
    console.print(f"  deleted  {path}")


def _execute_cleanup_locked(
    profile: str,
    orphans: list[OrphanEntry],
    choice: ApplyChoice,
    console: Console,
    *,
    entries: tuple[orphan_scan.ScanEntry, ...] | None = None,
    deltas: tuple[transitions.FilesystemDelta, ...] | None = None,
    journal: operations.OperationJournal | None = None,
) -> operations.OperationJournal | None:
    """Execute the chosen cleanup branch over a locked, refreshed list.

    For :attr:`ApplyChoice.DELETE_AND_TRANSITION` the transition record
    is written FIRST (before any unlink), so a crash between leaves a
    recoverable state. For :attr:`ApplyChoice.DELETE_ONLY` no
    transition is written and the deletes are irreversible. The
    :attr:`ApplyChoice.ABORT` branch is handled by the caller (no
    mutation, no console line beyond the abort marker). The caller owns the
    profile lock and re-detects immediately before invoking this helper.
    """
    frozen = (
        entries
        if entries is not None
        else tuple(orphan_scan.freeze_candidate(orphan.path) for orphan in orphans)
    )
    filesystem_deltas = (
        deltas
        if deltas is not None
        else transitions.filesystem_deletion_deltas(entry.path for entry in frozen)
    )
    for entry in frozen:
        orphan_scan.validate_approved_entry(entry)
    transitions.ensure_state_dir_writable()
    wrote_transition = False
    if choice is ApplyChoice.DELETE_AND_TRANSITION:
        wrote_transition = True

    console.print("=== orphan cleanup ===")
    for index, (orphan, entry) in enumerate(zip(orphans, frozen, strict=True), start=1):
        if journal is not None:
            journal = operations.begin_checkpoint(
                journal,
                name=f"delete-transition-orphan-{index}",
                kind=operations.CheckpointKind.REVERSIBLE,
                recovery=f"restore transition-attributed orphan {entry.path}",
                paths=(entry.path,),
                restore_state=False,
                adapters=(),
            )
        if index == 1 and wrote_transition:
            transition_dir = _write_orphan_transition(profile, filesystem_deltas)
            console.print(f"  transition: {transition_dir}")
        orphan_scan.unlink_approved_entry(entry)
        console.print(f"  deleted  {orphan.path}")
        if journal is not None:
            journal = operations.finish_checkpoint(journal)
    if wrote_transition:
        console.print(f"  to undo: setforge revert --profile={profile}")
    return journal


def _orphan_path_identity(path: Path) -> str:
    """Return a lexical identity without following a potentially orphaned symlink."""
    return os.path.normcase(os.fspath(path.expanduser().absolute()))


def _apply_orphan_cleanup(
    profile: str,
    config_path: Path,
    *,
    yes: bool,
    console: Console,
) -> None:
    """Entry-point for the ``--apply`` code path.

    Runs a pre-prompt live scan for the user's decision, then re-runs the shared
    effective-profile detector *inside* ``profile_lock`` before transition or
    unlink. A concurrent install/sync can therefore make a candidate active
    while cleanup waits, and the authoritative locked scan will retain it.
    Conversely, a newly discovered orphan is excluded because the user did not
    confirm it in the pre-prompt list.
    """
    _, detection = _detect_orphans_live(profile, config_path)
    orphans = detection.orphans
    if not orphans:
        console.print("=== no orphans ===")
        return

    choice = _pick_cleanup_branch(yes=yes)
    if choice is ApplyChoice.ABORT:
        console.print("[red]✗ aborted[/red] — no orphans deleted")
        return

    confirmed = {_orphan_path_identity(orphan.path) for orphan in orphans}
    with (
        mutation_locks(config_dir=config_path.resolve().parent, profile=profile),
        operations.recover_on_error(profile, "cleanup-orphans"),
    ):
        operations.refuse_active(profile)
        _, refreshed = _detect_orphans_live(profile, config_path)
        approved_still_orphaned = [
            orphan
            for orphan in refreshed.orphans
            if _orphan_path_identity(orphan.path) in confirmed
        ]
        if not approved_still_orphaned:
            console.print("=== no orphans ===")
            return
        paths = tuple(orphan.path for orphan in approved_still_orphaned)
        entries = tuple(orphan_scan.freeze_candidate(path) for path in paths)
        deltas = transitions.filesystem_deletion_deltas(entry.path for entry in entries)
        for entry in entries:
            orphan_scan.validate_approved_entry(entry)
        journal = operations.prepare(
            command="cleanup-orphans",
            profile=profile,
            config_dir=config_path.resolve().parent,
            resources_lock=False,
            command_line=("cleanup-orphans", "--apply"),
            paths=paths,
            path_guards=orphan_scan.capture_parent_path_guards(paths),
        )
        updated = _execute_cleanup_locked(
            profile,
            approved_still_orphaned,
            choice,
            console,
            entries=entries,
            deltas=deltas,
            journal=journal,
        )
        assert updated is not None
        journal = updated
        operations.complete(journal)


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
    scan: bool = typer.Option(
        False,
        "--scan",
        help=(
            "Discover unrecorded files and symlinks inside bounded managed "
            "trees. Apply requires separate TTY confirmation for each path."
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

    ``--scan`` discovers only unrecorded leaves under bounded managed roots.
    Its apply path requires per-file TTY consent and rejects blanket ``--yes``.
    """
    if scan and yes:
        raise SetforgeError(
            "--scan rejects --yes; unrecorded paths require per-file confirmation"
        )
    if scan and ignore is not None:
        raise SetforgeError("--scan and --ignore cannot be combined")

    resolved_config = _resolve_config_arg(config)
    console = Console(stderr=True)

    if ignore is not None:
        _append_ignored_orphan(ignore)
        console.print(
            f"added [cyan]{ignore}[/cyan] to orphan_ignore in {LOCAL_CONFIG_PATH}"
        )
        return

    if scan:
        if apply:
            _execute_scan_cleanup(profile, resolved_config, console=console)
        else:
            _, result = _detect_scan_live(profile, resolved_config)
            _print_scan_dry_run(result, console)
        return

    if not apply:
        _, detection = _detect_orphans_live(profile, resolved_config)
        _print_dry_run(
            detection.orphans,
            console,
            skipped_absent=detection.skipped_absent,
            skipped_source=detection.skipped_source,
            skipped_unmanaged=detection.skipped_unmanaged,
            skipped_host_local=detection.skipped_host_local,
        )
        return

    _apply_orphan_cleanup(profile, resolved_config, yes=yes, console=console)
