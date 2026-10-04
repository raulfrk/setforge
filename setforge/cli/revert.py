"""revert subcommand + transitions inspection subgroup.

``revert`` replays the most recent transition for a profile in reverse
and records its own reverse transition (so a second ``revert`` acts as
redo). ``transitions list`` / ``transitions show`` inspect the recorded
history.

Per mockup A: revert is gated by a confirm-explain-redo
wizard that shows the full diff, RISKS, and REDO instructions before
applying. ``--yes`` short-circuits the wizard for non-interactive use.
"""

import json
import os
import stat
import sys
import tempfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from setforge import operations, orphan_scan, transitions
from setforge._editor import run_editor
from setforge._redact import redact_argv
from setforge.cli import (
    _CONFIG_OPTION,
    _PROFILE_OPTION,
    _require_output_path,
    _resolve_config_arg,
    app,
)
from setforge.cli._help_examples import (
    REVERT_EXAMPLES,
    TRANSITIONS_LIST_EXAMPLES,
    TRANSITIONS_SHOW_EXAMPLES,
)
from setforge.cli._helpers import ProfileContext, _iter_all_tracked_files
from setforge.cli._output import make_console, render
from setforge.cli._plugin_helpers import _write_reverse_transition
from setforge.cli._revert_confirm import (
    ExtensionOperation,
    ExtensionReconcile,
    FileMutation,
    MultiStepRevertPlan,
    PluginOperation,
    PluginReconcile,
    RevertChoice,
    RevertPlan,
    confirm_multi_step_revert_operation,
    confirm_revert_operation,
)
from setforge.config import (
    load_config,
    resolve_effective_profile,
    resolve_symlink_target,
)
from setforge.errors import (
    NoTransitionFound,
    ProfileNotFound,
    RevertFailed,
    SetforgeError,
)
from setforge.ownership import (
    OwnershipStore,
    read_owner_id_locked,
    resolve_owner_common_dir,
)
from setforge.snapshots import format_age


def _human_age(timestamp: datetime, now: datetime) -> str:
    """Return a coarse human-readable age string ("11 minutes ago")."""
    delta = now - timestamp
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds} seconds ago"
    minutes = seconds // 60
    if minutes < 60:
        unit = "minute" if minutes == 1 else "minutes"
        return f"{minutes} {unit} ago"
    hours = minutes // 60
    if hours < 24:
        unit = "hour" if hours == 1 else "hours"
        return f"{hours} {unit} ago"
    days = hours // 24
    unit = "day" if days == 1 else "days"
    return f"{days} {unit} ago"


def _compact_age(timestamp: datetime, now: datetime) -> str:
    """Return the mockup's compact age form ("2h ago", "3d ago", "5m ago", "<1m ago").

    Distinct from :func:`_human_age` (used by the confirm wizard's
    long-form panel) — the listing column needs a fixed-narrow string
    so the table aligns. Uses UTC arithmetic via the caller-supplied
    ``now``; both ``timestamp`` and ``now`` must be tz-aware.
    """
    if (now - timestamp).total_seconds() < 60:
        return "<1m ago"
    return format_age(now, timestamp)


def _diff_summaries_from_patch(patch_text: str) -> dict[str, str]:
    """Parse a unified diff and return ``{abs_path: "+N -M"}`` per file.

    Counts hunk-body ``+``/``-`` lines (skipping ``+++`` / ``---``
    headers). Paths are rebuilt from the ``+++`` line per
    :func:`transitions._diff_path` (root-relative; prepend ``/``), reversing
    any C-style quoting via :func:`transitions._cunquote_path` so this stays
    symmetric with :func:`transitions.summarize_transition`.
    ``/dev/null`` paths use the corresponding ``--- a/<x>`` for deletions.
    """
    summaries: dict[str, str] = {}
    current_path: str | None = None
    plus = 0
    minus = 0
    # Only ``--- ``/``+++ `` lines in a FILE-HEADER region are path headers;
    # inside a hunk body a deleted line whose content starts with ``-- ``
    # renders as ``--- foo`` and must be counted as a deletion, not mistaken
    # for a header (else the real file's counts reset and a phantom entry
    # appears). ``in_hunk`` is True once ``@@`` opens a hunk body and stays
    # True until the next ``diff``/``index`` opens a fresh header region.
    in_hunk = False
    for line in patch_text.splitlines():
        if line.startswith(("diff ", "index ")):
            in_hunk = False
            continue
        if not in_hunk and line.startswith("--- "):
            from_path = transitions._cunquote_path(line[4:].split("\t", 1)[0])
            current_path = (
                _abs_diff_path(from_path) if from_path != "/dev/null" else None
            )
            continue
        if not in_hunk and line.startswith("+++ "):
            to_path = transitions._cunquote_path(line[4:].split("\t", 1)[0])
            if to_path != "/dev/null":
                current_path = _abs_diff_path(to_path)
            plus = 0
            minus = 0
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if current_path is None:
            continue
        if line.startswith("+"):
            plus += 1
        elif line.startswith("-"):
            minus += 1
        summaries[current_path] = f"+{plus} -{minus}"
    return summaries


def _abs_diff_path(path: str) -> str:
    """Prepend a single leading ``/`` to a diff-header path.

    setforge emits paths root-relative (no leading ``/``), so the preview
    re-anchors them absolute. A path that already carries a leading ``/``
    (e.g. a hand-authored or externally sourced patch) must not become
    ``//root/...`` — normalize so exactly one leading slash results.
    """
    return "/" + path.lstrip("/")


def _plugin_reconciles_from_transition(
    delta: transitions.PluginDelta | None,
) -> tuple[PluginReconcile, ...]:
    """Build :class:`PluginReconcile` tuple from a transition's plugin delta.

    Maps the forward ``PluginDelta`` to the post-revert state that the
    panel surfaces (matches the dispatch semantics in
    :data:`setforge.cli._plugin_helpers._REVERSE_PLUGIN_DISPATCH`):

    - forward ``installed`` → revert uninstalls → :attr:`PluginOperation.DISABLED`
      (panel marker ``-``).
    - forward ``enabled``   → revert disables   → :attr:`PluginOperation.DISABLED`.
    - forward ``disabled``  → revert re-enables → :attr:`PluginOperation.ENABLED`
      (panel marker ``+``).

    ``marketplaces_added`` / ``marketplaces_removed`` are intentionally
    NOT projected into the panel listing — the wizard's plugin section
    is per-plugin; marketplace ops are a separate axis and would need
    their own renderer.
    """
    if delta is None:
        return ()
    source = "[from transition record]"
    reconciles: list[PluginReconcile] = []
    for plugin_id in delta.installed:
        reconciles.append(
            PluginReconcile(
                plugin_id=plugin_id,
                operation=PluginOperation.DISABLED,
                source=source,
            )
        )
    for plugin_id in delta.enabled:
        reconciles.append(
            PluginReconcile(
                plugin_id=plugin_id,
                operation=PluginOperation.DISABLED,
                source=source,
            )
        )
    for plugin_id in delta.disabled:
        reconciles.append(
            PluginReconcile(
                plugin_id=plugin_id,
                operation=PluginOperation.ENABLED,
                source=source,
            )
        )
    return tuple(reconciles)


def _extension_reconciles_from_transition(
    delta: transitions.ExtensionDelta | None,
) -> tuple[ExtensionReconcile, ...]:
    """Build :class:`ExtensionReconcile` tuple from a transition's extension delta.

    Maps the forward ``ExtensionDelta`` to the post-revert state:

    - forward ``added``   → revert uninstalls → :attr:`ExtensionOperation.UNINSTALLED`
      (panel marker ``-``).
    - forward ``removed`` → revert reinstalls → :attr:`ExtensionOperation.INSTALLED`
      (panel marker ``+``).
    """
    if delta is None:
        return ()
    source = "[from transition record]"
    reconciles: list[ExtensionReconcile] = []
    for ext_id in delta.added:
        reconciles.append(
            ExtensionReconcile(
                extension_id=ext_id,
                operation=ExtensionOperation.UNINSTALLED,
                source=source,
            )
        )
    for ext_id in delta.removed:
        reconciles.append(
            ExtensionReconcile(
                extension_id=ext_id,
                operation=ExtensionOperation.INSTALLED,
                source=source,
            )
        )
    return tuple(reconciles)


def _build_revert_plan(
    record: transitions.TransitionRecord, profile: str
) -> RevertPlan:
    """Compute per-file diff summaries for ``record`` → RevertPlan.

    The plan reflects what the FORWARD transition did; revert will
    reverse each item. Plugin / extension reconciles are inferred from
    the transition's ``plugins.json`` / ``extensions.json`` payloads when
    present (via :func:`_plugin_reconciles_from_transition` and
    :func:`_extension_reconciles_from_transition`). Collision detection
    runs at apply time via ``patch --dry-run -R`` (see
    :func:`transitions.apply_patch_reverse`).
    """
    transition = record.directory
    meta = record.meta
    age = _human_age(meta.timestamp, datetime.now(UTC))

    patch_file = transition / "changes.patch"
    diff_summaries: dict[str, str] = {}
    if patch_file.exists():
        diff_summaries = _diff_summaries_from_patch(
            patch_file.read_text(encoding="utf-8", errors="surrogateescape")
        )

    # Per-path mode restore: when the forward transition recorded a
    # pre-install mode for a path, revert will chmod it back — surface that
    # in the preview so a mode-only revert (empty content patch) is not
    # silent. Missing file_modes.json (pre-bump) → empty map → no note.
    recorded_modes = record.file_modes
    touched = record.paths
    # A content-NOOP + mode-only install records the path in file_modes but
    # NOT in meta.json's ``paths`` (no content delta), so union the
    # mode-only paths in — preserving the touched-paths order first — so the
    # preview lists every file revert will mutate on EITHER axis.
    touched_set = set(touched)
    mode_only = [p for p in recorded_modes if p not in touched_set]
    file_mutations = tuple(
        FileMutation(
            path=p,
            diff_summary=diff_summaries.get(str(p), "+0 -0"),
            mode_restore=(
                f"mode → {recorded_modes[p]:#o}" if p in recorded_modes else None
            ),
        )
        for p in [*touched, *mode_only]
    )

    return RevertPlan(
        transition_id=transition.name,
        transition_type=str(meta.command),
        profile=profile,
        age_human=age,
        file_mutations=file_mutations,
        plugin_reconciles=_plugin_reconciles_from_transition(record.plugins),
        extension_reconciles=_extension_reconciles_from_transition(record.extensions),
        redo_command=f"setforge revert --profile={profile}",
    )


def _render_plan_to_editor(plan: RevertPlan) -> Path:
    """Write a human-readable rendering of ``plan`` to a tmp file → Path.

    Used by APPLY_WITH_EDITOR to let the user review the plan in their
    ``$EDITOR`` before re-prompting. The file is read-only-ish: we never
    parse the editor's output back; this is a review gesture, not a
    plan-editor.
    """
    fd, name = tempfile.mkstemp(prefix="setforge-revert-plan-", suffix=".txt")
    # Close the OS-level fd immediately; we'll use Path.write_text below.
    # Closing here (rather than after write_text) keeps the fd from
    # leaking on any exception that fires during line building.
    os.close(fd)
    target = Path(name)
    lines = [
        f"transition: {plan.transition_id}",
        f"  type:    {plan.transition_type}",
        f"  profile: {plan.profile}",
        f"  age:     {plan.age_human}",
        "",
        f"files affected ({len(plan.file_mutations)}):",
    ]
    for fm in plan.file_mutations:
        lines.append(f"  M  {fm.path}  (line-delta: {fm.diff_summary})")
    if plan.plugin_reconciles:
        lines.append("")
        lines.append(f"plugins reconciled ({len(plan.plugin_reconciles)}):")
        for pr in plan.plugin_reconciles:
            marker = "+" if pr.operation is PluginOperation.ENABLED else "-"
            lines.append(f"  {marker} {pr.plugin_id}  {pr.source}")
    if plan.extension_reconciles:
        lines.append("")
        lines.append(f"extensions reconciled ({len(plan.extension_reconciles)}):")
        for er in plan.extension_reconciles:
            marker = "+" if er.operation is ExtensionOperation.INSTALLED else "-"
            lines.append(f"  {marker} {er.extension_id}  {er.source}")
    lines.append("")
    lines.append(f"REDO: {plan.redo_command}")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def _apply_revert(
    record: transitions.TransitionRecord,
    profile: str,
    config: Path,
    *,
    path_guards: tuple[operations.PathGuard, ...] = (),
    config_identity_fd: int | None = None,
) -> Path:
    """Apply the reverse transition and return the record it wrote.

    The caller reports that record only once its journal has completed, so a
    step that is later rolled back is never announced as a recorded revert.

    Content patches reverse payload bytes; typed filesystem deltas restore
    symlink topology while retaining links that existed before the install.
    Legacy records lacking a touched link's preimage refuse before effects.

    Store-state restore (Invariant I5): when the transition carries
    ``state_snapshots/``, the byte bases / spans sidecars / scalar-base
    manifests it captured are restored after the patch reverse —
    was-absent entries deleted, present entries rewritten byte-exact.
    The CURRENT store state for the same (store, key) set is recaptured
    first and recorded on the reverse transition, so a second revert
    (redo) round-trips the stores too. A pre-snapshot transition (no
    ``state_snapshots/`` dir) skips store work entirely — its store
    deltas, if any, still ride its own ``changes.patch`` from the era
    when store files were patch-recorded. The reverse transition is
    written LAST (meta.json is its commit marker), so an interrupted
    revert is re-runnable: the idempotent restore simply re-applies.

    File-mode restore: when the transition carries a ``file_modes.json``
    (a forward command changed a file's permission bits), each recorded
    path is chmod-ed back to its pre-command mode AFTER the patch reverse —
    the content patch carries bytes only, so this is the only inverse of
    the install chmod (e.g. a 0600 secret retracked to 0644 is restored to
    0600). The CURRENT mode of each such path is recaptured FIRST and
    recorded on the reverse transition so a second revert (redo) restores
    the install-applied mode — mode redo symmetry mirrors the store-state
    recapture. A pre-bump transition (no ``file_modes.json``) reads back as
    an empty map and skips mode work entirely (backward-compat).
    """
    transitions.ensure_state_dir_writable()
    transition = record.directory
    typer.echo(f"reverting: {transition}")

    filesystem_deltas = record.filesystem_deltas
    ownership_transfers = record.ownership_transfers
    filesystem_paths = {item.path for item in filesystem_deltas}
    text_paths = [path for path in record.paths if path not in filesystem_paths]
    file_pre = transitions.snapshot_paths(text_paths)

    pre_store_state = record.state_snapshots
    reverse_store_state: tuple[transitions.StateSnapshotEntry, ...] = ()
    if pre_store_state is not None:
        # Recapture the SAME (store, key) set as it stands now — before
        # any mutation — so the reverse transition can redo this revert.
        reverse_store_state = tuple(
            transitions.snapshot_store_state(e.store, e.profile, e.key)
            for e in pre_store_state
        )

    # Recapture the CURRENT mode of every mode-recorded path BEFORE the
    # restore so the reverse transition can redo the install chmod. Empty
    # for a pre-bump transition (no file_modes.json → {}), which then
    # records no file_modes on the reverse record (omit-when-empty).
    pre_command_modes = record.file_modes
    reverse_modes = _recapture_modes(pre_command_modes)

    _refuse_legacy_symlink_record(record, config, profile)
    transitions.validate_filesystem_deltas_reverse(filesystem_deltas)
    ownership_store = OwnershipStore()
    _validate_ownership_transfer_reverse(
        ownership_transfers,
        config=config,
        profile=profile,
        store=ownership_store,
        config_identity_fd=config_identity_fd,
    )

    transitions.apply_patch_reverse(transition)
    operations.apply_filesystem_deltas_reverse_anchored(filesystem_deltas, path_guards)
    if pre_store_state is not None:
        transitions.restore_state_snapshots(pre_store_state)
    # Restore each path's pre-install mode AFTER the patch reverse rewrote
    # its bytes (the content patch never carries permission bits).
    _restore_modes(pre_command_modes)
    reverse_ownership: list[transitions.OwnershipTransferDelta] = []
    for item in reversed(ownership_transfers):
        restored = ownership_store.transfer_locked(
            item.after.resource_id,
            expected_owner=item.after.owner_id,
            new_owner=item.before.owner_id,
            expected_generation=item.after.generation,
            declaration_refs=item.before.declaration_refs,
        )
        reverse_ownership.append(
            transitions.OwnershipTransferDelta(item.after, restored)
        )

    target = _write_reverse_transition(
        transition,
        profile,
        text_paths,
        file_pre,
        state_snapshots=reverse_store_state,
        file_modes=reverse_modes,
        filesystem_deltas=transitions.reverse_filesystem_deltas(filesystem_deltas),
        ownership_transfers=tuple(reverse_ownership),
    )
    return target


def _report_recorded_revert(target: Path, profile: str) -> None:
    typer.echo(f"transition: {target}")
    typer.echo(f"to REDO this revert: setforge revert --profile={profile}")


def _validate_ownership_transfer_reverse(
    deltas: tuple[transitions.OwnershipTransferDelta, ...],
    *,
    config: Path,
    profile: str,
    store: OwnershipStore,
    config_identity_fd: int | None,
) -> None:
    """Reconcile exact claims with current declarations and live fingerprints."""
    if not deltas:
        return
    from setforge.cli.install import (
        _preview_file_declaration_refs,
        _preview_file_ownership,
        _preview_package_ownership,
    )
    from setforge.provision.ownership import observation_fingerprint

    if config_identity_fd is None:
        raise RevertFailed("ownership transfer lacks a config identity lock")
    owner_id = read_owner_id_locked(config.resolve().parent, config_identity_fd)
    file_decisions = _preview_file_ownership(
        config, profile, owner_id_override=owner_id
    )
    package_decisions = _preview_package_ownership(
        config, profile, locked=False, owner_id_override=owner_id
    )
    fingerprints = {
        decision.observation.resource_id: decision.observation.fingerprint
        for decision in file_decisions
    }
    fingerprints.update(
        {
            decision.resource_id: observation_fingerprint(decision.observation)
            for decision in package_decisions
            if decision.observation is not None
        }
    )
    declaration_refs = _preview_file_declaration_refs(config, profile)
    for decision in package_decisions:
        declaration_refs[decision.resource_id] = (
            f"packages.{decision.item.type}.{decision.item.identity.key}",
        )
    for item in deltas:
        current = store.read(item.after.resource_id)
        if current != item.after:
            raise RevertFailed(
                "ownership claim changed since transition: "
                f"{item.after.resource_id.canonical()}"
            )
        if owner_id != item.after.owner_id:
            raise RevertFailed(
                "only the current ownership recipient can reverse this transfer"
            )
        if declaration_refs.get(item.after.resource_id) != item.after.declaration_refs:
            raise RevertFailed(
                "ownership declaration changed since transition: "
                f"{item.after.resource_id.canonical()}"
            )
        if fingerprints.get(item.after.resource_id) != item.after.fingerprint:
            raise RevertFailed(
                f"ownership resource changed since transition: {item.after.locator}"
            )


def _ownership_transfer_lock_targets(
    records: tuple[transitions.TransitionRecord, ...],
) -> tuple[Path, ...]:
    """Freeze filesystem containers referenced by ownership sidecars."""
    locators = {
        Path(item.after.locator).absolute()
        for record in records
        for item in record.ownership_transfers
        if item.after.resource_id.kind == "file"
    }
    targets = {
        path if path.is_dir() and not path.is_symlink() else path.parent
        for path in locators
    }
    return tuple(sorted(targets, key=str))


def _ownership_transfer_identity_dir(
    records: tuple[transitions.TransitionRecord, ...], config: Path
) -> Path | None:
    if any(record.ownership_transfers for record in records):
        return resolve_owner_common_dir(config.resolve().parent)
    return None


def _recapture_modes(recorded: Mapping[Path, int]) -> dict[Path, int]:
    """Snapshot the CURRENT mode of every path in ``recorded`` (redo data).

    Returns ``{path: live_mode}`` for each path that still exists — the
    install-applied mode this revert is about to undo. The reverse
    transition records this so a second revert (redo) re-applies the
    install chmod, mirroring the store-state recapture. A path that no
    longer exists (deleted out-of-band) is dropped: there is nothing to
    redo a chmod onto. Must run BEFORE :func:`_restore_modes` mutates the
    live modes.
    """
    out: dict[Path, int] = {}
    for path in recorded:
        try:
            out[path] = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            continue
    return out


def _restore_modes(recorded: Mapping[Path, int]) -> None:
    """chmod each recorded path back to its pre-command mode.

    Runs AFTER the patch reverse restored the path's bytes, so the mode
    axis is reverted in lockstep with the content axis. A path that no
    longer exists is skipped (idempotent — an interrupted revert can
    re-run); no-op for the empty map (pre-bump transition).
    """
    for path, mode in recorded.items():
        try:
            path.chmod(mode)
        except FileNotFoundError:
            continue


_TO_BEFORE_OPTION = typer.Option(
    None,
    "--to-before",
    help=(
        "Revert the named transition AND every newer transition for the "
        "profile. The newest step is pre-flight dry-run-checked; if a "
        "later step fails, the steps already applied are rolled back and "
        "nothing is changed (exit 1)."
    ),
)


@app.command(epilog=REVERT_EXAMPLES)
def revert(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirm-explain-redo prompt (non-interactive use).",
    ),
    to_before: str | None = _TO_BEFORE_OPTION,
) -> None:
    """Revert the most recent transition for ``--profile=X``.

    With ``--to-before=<id>``: revert the named transition AND every
    newer transition for the profile (in reverse-chronological order).
    The newest step is pre-flight dry-run-checked before any live
    mutation. Subsequent steps each run their own internal
    dry-run-then-apply gate; when one of them fails, the steps already
    applied are rolled back, so the chain reverts completely or not at all.

    Opens the confirm-explain-redo wizard before applying (mockup A for
    single-step; mockup H summary panel for multi-step). Records its own
    reverse transition so a second revert acts as redo.
    """
    config = _resolve_config_arg(config)
    if to_before is not None:
        _revert_to_before(profile, to_before, config=config, yes=yes)
        return

    transition = transitions.load_latest(profile)
    if transition is None:
        raise NoTransitionFound(f"no transition history for profile {profile!r}")

    record = transitions.load_record(transition)
    plan = _build_revert_plan(record, profile)
    choice = confirm_revert_operation(plan=plan, yes=yes)
    if choice is RevertChoice.ABORT:
        return
    while choice is RevertChoice.APPLY_WITH_EDITOR:
        target = _render_plan_to_editor(plan)
        try:
            run_editor(target)
        finally:
            target.unlink(missing_ok=True)
        # Re-prompt after editor closes so the user can still abort or re-edit.
        choice = confirm_revert_operation(plan=plan, yes=yes)
    if choice is RevertChoice.ABORT:
        return

    _apply_confirmed_reverts(
        (record,),
        profile,
        config,
        chain=False,
        history_unchanged=lambda: transitions.load_latest(profile) == transition,
    )


def _apply_confirmed_reverts(
    records: tuple[transitions.TransitionRecord, ...],
    profile: str,
    config: Path,
    *,
    chain: bool,
    history_unchanged: Callable[[], bool],
) -> None:
    """Revert ``records`` in order under one lock set and one journal.

    Serializes the live mutation against concurrent install/sync/revert,
    matching install.py / sync.py: the deploy model relies on a
    single-serialized-process assumption, and each step's reverse patch is
    defined against the state the previous step produced. The locks are
    taken after the confirm wizard and before any patch-reverse or store
    restore, in the canonical order shared with install (global adapters,
    then profile files). ``history_unchanged`` re-checks the confirmed
    selection once they are held. A failure rolls every applied step back,
    and the recorded reverts are reported only after the journal completes.
    """
    recorded: list[Path] = []
    try:
        with operations.transaction(
            resources=True,
            config_identity_dir=_ownership_transfer_identity_dir(records, config),
            config_dir=config.resolve().parent,
            target_roots=_ownership_transfer_lock_targets(records),
            profiles=_revert_locked_profiles(records, profile),
            recover=(profile, "revert"),
        ) as mutation_guards:
            if not history_unchanged():
                raise SetforgeError(
                    "transition history changed after confirmation; retry revert"
                )
            journal = _prepare_revert_journal(records, profile, config)
            journal = operations.begin_checkpoint(
                journal,
                name="revert-chain",
                kind=operations.CheckpointKind.COMPENSATABLE,
            )
            identity_guard = (
                mutation_guards.config_identity if mutation_guards is not None else None
            )
            config_identity_fd = (
                identity_guard.directory_fd if identity_guard is not None else None
            )
            for record in records:
                recorded.append(
                    _apply_revert(
                        record,
                        profile,
                        config,
                        path_guards=journal.path_guards,
                        config_identity_fd=config_identity_fd,
                    )
                )
            journal = operations.finish_checkpoint(journal)
            operations.complete(journal)
    except BaseException as failure:
        # recover_on_error attaches a note whenever its rollback was incomplete.
        if chain and recorded and not getattr(failure, "__notes__", ()):
            typer.echo(
                f"rolled back {len(recorded)} already reverted step(s) of this "
                "chain; nothing was changed",
                err=True,
            )
        raise
    for target in recorded:
        _report_recorded_revert(target, profile)


def _resolve_to_before_chain(
    profile: str, to_before: str
) -> list[transitions.TransitionListing]:
    """Return the chain of transitions to revert (newest-first), inclusive of
    ``to_before``.

    Raises :class:`SetforgeError` if the prefix doesn't resolve, the
    resolved transition isn't for ``profile``, or no transitions exist
    for the profile. Newest-first order matches both the mockup-H
    listing and the dry-run / apply order — the most-recent transition
    reverts first so each step's reverse patch lines up against the
    live tree it was recorded from.
    """
    target_path = transitions.resolve_transition_prefix(to_before)
    target_profile = transitions.load_meta(target_path).profile
    if target_profile != profile:
        raise SetforgeError(
            f"transition {target_path.name!r} is for profile "
            f"{target_profile!r}, not {profile!r}"
        )
    all_for_profile = transitions.list_transitions(
        profile_filter=[profile], reverse=True
    )
    if not all_for_profile:
        raise NoTransitionFound(f"no transition history for profile {profile!r}")
    chain: list[transitions.TransitionListing] = []
    for entry in all_for_profile:
        chain.append(entry)
        if entry.directory == target_path:
            return chain
    raise SetforgeError(
        f"transition {target_path.name!r} not found in profile "
        f"{profile!r}'s recorded history"
    )


def _revert_to_before(profile: str, to_before: str, *, config: Path, yes: bool) -> None:
    """Multi-step revert: pre-flight first step, then sequential apply.

    Steps:
    1. Resolve the chain (target + every newer transition, newest-first).
    2. Pre-flight check the FIRST (newest) step via
       ``apply_patch_reverse(dry_run=True)``: this catches the
       most-likely failure mode — drift on the live tree since the
       most-recent transition was recorded. Surface failure and exit 1
       on drift; no live mutation has occurred.

       Note: we cannot pre-flight steps 2..N without applying 1..N-1
       first — each later step's reverse patch is defined against the
       state produced by reversing its successor. Steps 2..N still run
       their internal dry-run-then-apply (the existing
       ``dry_run=False`` mode) so they refuse cleanly mid-stream on
       unexpected drift.
    3. Show the multi-step confirm wizard (one prompt covering all N).
    4. On user confirm: apply each step's reverse via
       ``_apply_revert`` (which calls ``apply_patch_reverse(dry_run=False)``
       — i.e. dry-run-then-real-apply per step — plus plugin / extension
       reconcile and writes a reverse transition). One journal covers the
       whole chain: a mid-stream failure rolls the applied steps back, and
       the recorded reverts are reported only after the chain commits.
    """
    chain = _resolve_to_before_chain(profile, to_before)
    # Pre-flight the newest step. Only step 1 is checkable against live
    # state without applying prior steps; see docstring.
    try:
        transitions.apply_patch_reverse(chain[0].directory, dry_run=True)
    except RevertFailed as exc:
        raise SetforgeError(
            f"dry-run reversal of {chain[0].directory.name!r} failed; "
            f"no live changes made:\n{exc}"
        ) from exc

    records = tuple(transitions.load_record(entry.directory) for entry in chain)
    step_plans = tuple(_build_revert_plan(record, profile) for record in records)
    plan = MultiStepRevertPlan(profile=profile, steps=step_plans)
    choice = confirm_multi_step_revert_operation(plan=plan, yes=yes)
    if choice is RevertChoice.ABORT:
        return

    def chain_unchanged() -> bool:
        refreshed = _resolve_to_before_chain(profile, to_before)
        return [entry.directory for entry in refreshed] == [
            entry.directory for entry in chain
        ]

    _apply_confirmed_reverts(
        records, profile, config, chain=True, history_unchanged=chain_unchanged
    )


def _prepare_revert_journal(
    chain: tuple[transitions.TransitionRecord, ...], profile: str, config: Path
) -> operations.OperationJournal:
    """Capture the whole confirmed reverse chain before its first mutation."""
    from setforge import mcp_servers

    touched: dict[Path, None] = {}
    state_keys: dict[
        tuple[transitions.SnapshotStore, str, str],
        transitions.StateSnapshotEntry,
    ] = {}
    has_extensions = False
    has_plugins = False
    has_codex_plugins = False
    mcp_endpoints: dict[str, list[tuple[tuple[str, ...], str]]] = {}
    generic_paths: dict[Path, None] = {}
    for record in chain:
        transfer_claim_paths = {
            OwnershipStore().claim_path(item.after.resource_id): None
            for item in record.ownership_transfers
        }
        touched.update(transfer_claim_paths)
        generic_paths.update(transfer_claim_paths)
        touched.update(dict.fromkeys(record.paths))
        generic_paths.update(
            dict.fromkeys(item.path for item in record.filesystem_deltas)
        )
        for snapshot in record.state_snapshots or ():
            identity = (snapshot.store, snapshot.profile, snapshot.key)
            state_keys[identity] = transitions.snapshot_store_state(*identity)
        has_extensions |= record.extensions is not None
        has_plugins |= record.plugins is not None
        has_codex_plugins |= record.codex_plugins is not None
        if record.mcp is not None:
            delta = record.mcp
            if not delta.is_empty():
                mcp_servers.require_inventory_context(delta.context, delta.scopes)
            for name, command, scope in (*delta.added, *delta.updated):
                endpoints = mcp_endpoints.setdefault(name, [])
                if (command, scope) not in endpoints:
                    endpoints.append((command, scope))
        _refuse_legacy_symlink_record(record, config, record.meta.profile)
    return operations.prepare(
        command="revert",
        profile=profile,
        config_dir=config.resolve().parent,
        resources_lock=True,
        command_line=tuple(redact_argv(sys.argv[1:])),
        paths=tuple(touched),
        state_snapshots=tuple(state_keys.values()),
        adapters=_revert_adapter_snapshots(
            extensions=has_extensions,
            plugins=has_plugins,
            codex_plugins=has_codex_plugins,
            mcp_endpoints=mcp_endpoints,
        ),
        path_guards=orphan_scan.capture_parent_path_guards(tuple(generic_paths)),
    )


def _revert_locked_profiles(
    chain: tuple[transitions.TransitionRecord, ...], journal_profile: str
) -> tuple[str, ...]:
    """Return the sorted profile lock envelope for a reverse chain."""
    profiles = {journal_profile}
    for record in chain:
        profiles.update(snapshot.profile for snapshot in record.state_snapshots or ())
    return tuple(sorted(profiles))


def _refuse_legacy_symlink_record(
    record: transitions.TransitionRecord, config: Path, profile: str
) -> None:
    """Refuse uncertain legacy link inverses; typed images handle current links."""
    covered = {delta.path for delta in record.filesystem_deltas}
    touched = frozenset(record.paths)
    cfg = load_config(config)
    repo_root = config.resolve().parent
    try:
        resolved = resolve_effective_profile(cfg, profile, repo_root).resolved
    except ProfileNotFound:
        if profile == transitions.MIGRATE_TRANSITION_PROFILE:
            return
        raise
    ctx = ProfileContext(
        cfg=cfg, resolved=resolved, repo_root=repo_root, profile=profile
    )
    for tracked, _name, _source, destination in _iter_all_tracked_files(ctx):
        if tracked.symlink is None or destination in covered:
            continue
        target = resolve_symlink_target(destination, tracked.symlink)
        attributed = any(
            destination in paths and touched.intersection(paths)
            for paths in record.tracked_file_destinations.values()
        )
        if destination in touched or target in touched or attributed:
            raise SetforgeError(
                f"legacy transition lacks symlink preimage for {destination}; "
                "revert cannot determine whether to retain or remove this link. "
                "Preserve the current files and reconcile the desired tracked source "
                "with install --file instead."
            )


def _revert_adapter_snapshots(
    *,
    extensions: bool,
    plugins: bool,
    codex_plugins: bool,
    mcp_endpoints: dict[str, list[tuple[tuple[str, ...], str]]],
) -> tuple[operations.AdapterSnapshot, ...]:
    from setforge import claude_plugins, mcp_servers, vscode_extensions
    from setforge import codex_plugins as codex_plugins_mod

    snapshots: list[operations.AdapterSnapshot] = []
    if extensions:
        snapshots.append(
            operations.AdapterSnapshot(
                operations.AdapterKind.EXTENSIONS,
                json.dumps(sorted(vscode_extensions.list_installed())),
            )
        )
    if plugins:
        snapshots.append(
            operations.AdapterSnapshot(
                operations.AdapterKind.PLUGINS,
                json.dumps(
                    {
                        "plugins": claude_plugins.list_installed(),
                        "marketplaces": claude_plugins.list_marketplaces(),
                    },
                    sort_keys=True,
                ),
            )
        )
    if codex_plugins:
        snapshots.append(
            operations.AdapterSnapshot(
                operations.AdapterKind.CODEX_PLUGINS,
                json.dumps(
                    {
                        "plugins": sorted(codex_plugins_mod.list_installed()),
                        "marketplaces": sorted(
                            [
                                name,
                                (
                                    item.source
                                    or codex_plugins_mod._source_from_root(item.root)
                                ).model_dump_json(exclude_none=True),
                            ]
                            for name, item in (
                                codex_plugins_mod.list_marketplaces().items()
                            )
                        ),
                    },
                    sort_keys=True,
                ),
            )
        )
    if mcp_endpoints:
        snapshots.append(
            operations.AdapterSnapshot(
                operations.AdapterKind.MCP,
                json.dumps(
                    [
                        {
                            "name": name,
                            "prior": mcp_servers.mcp_get_command(name),
                            "planned": endpoints,
                            "context": mcp_servers.inventory_context(),
                        }
                        for name, endpoints in mcp_endpoints.items()
                    ],
                    sort_keys=True,
                ),
            )
        )
    return tuple(snapshots)


transitions_app: typer.Typer = typer.Typer(
    help="Inspect transition history for install/sync/revert.",
    no_args_is_help=True,
    rich_markup_mode=None,
)
app.add_typer(transitions_app, name="transitions")


@transitions_app.callback()
def _transitions_output_contract(ctx: typer.Context) -> None:
    """Enforce the global output contract before a transitions leaf runs."""
    if ctx.invoked_subcommand is not None:
        _require_output_path(ctx.obj, ("transitions", ctx.invoked_subcommand))


_TRANSITIONS_LIST_PROFILE_OPTION = typer.Option(
    None,
    "--profile",
    "-p",
    help="Filter to specified profile(s). Repeatable; OR-filter.",
)
_TRANSITIONS_LIST_OLDEST_FIRST_OPTION = typer.Option(
    False,
    "--oldest-first",
    help="Reverse the default newest-first order (oldest first).",
)


@transitions_app.command("list", epilog=TRANSITIONS_LIST_EXAMPLES)
def transitions_list(
    ctx: typer.Context,
    profile: list[str] | None = _TRANSITIONS_LIST_PROFILE_OPTION,
    oldest_first: bool = _TRANSITIONS_LIST_OLDEST_FIRST_OPTION,
) -> None:
    """List recorded transitions for one or more profiles (newest-first).

    Columns per mockup H: ``id`` / ``type`` / ``age`` / ``files`` /
    ``plugins`` / ``ext``. Use ``--oldest-first`` to reverse to
    chronological order.
    """
    listings = transitions.list_transitions(
        profile_filter=list(profile) if profile else None,
        reverse=not oldest_first,
    )
    profile_filter = list(profile) if profile else None

    def _human() -> None:
        if not listings:
            typer.echo("(no transitions)")
            return
        _render_transitions_table(listings, profile_filter=profile_filter)

    data = _transitions_list_json_data(listings, profile_filter=profile_filter)
    render(ctx.obj, "transitions list", data, human_fn=_human)


def _transitions_list_json_data(
    listings: list[transitions.TransitionListing],
    *,
    profile_filter: list[str] | None,
) -> dict[str, Any]:
    """Build the JSON-mode payload for ``setforge transitions list``.

    Each transition surfaces as ``{id, type, profile, timestamp, files,
    plugins, ext}``; ``timestamp`` is ISO-8601 UTC. The ``profile_filter``
    is echoed back so downstream tooling can confirm which filter was
    applied (mirrors the human header's ``=== transitions for profile X ===``).
    """
    entries = [
        {
            "id": entry.directory.name,
            "type": entry.command,
            "profile": entry.profile,
            "timestamp": entry.timestamp.astimezone(UTC).isoformat(),
            "files": entry.file_count,
            "plugins": entry.plugin_count,
            "codex_plugins": entry.codex_plugin_count,
            "ownership_transfers": entry.ownership_transfer_count,
            "ext": entry.ext_count,
        }
        for entry in listings
    ]
    return {
        "profile_filter": list(profile_filter) if profile_filter else None,
        "transitions": entries,
    }


def _wide_console() -> Console:
    """Build a Console wide enough that mockup-H rows never wrap.

    Rich defaults to 80-col width when stdout is not a TTY (the case
    under CliRunner) — which truncates / wraps transition dirnames
    mid-line and breaks both the mockup parity and the
    ``--to-before=<id>`` copy-paste suggestion. A fixed 200-col width
    fits the widest realistic row (dirname + suffix < ~180 chars).

    ``highlight=False`` disables Rich's auto-highlighter — the
    transitions list embeds numbers and dirnames that Rich would
    otherwise wrap in ANSI sequences, breaking substring assertions
    in tests and copy-pasteability of the suggested commands.
    """
    return make_console(width=200, highlight=False)


def _render_transitions_table(
    listings: list[transitions.TransitionListing],
    *,
    profile_filter: list[str] | None,
) -> None:
    """Render the polished newest-first columnar listing per mockup H.

    Trailing hint lines ("to view details", "to revert to BEFORE …")
    surface only when at least one entry exists. The hint uses the
    newest entry's id as the example and the first profile from the
    filter (or the newest entry's profile when no filter is set) so
    the suggested command is always copy-pasteable.
    """
    console = _wide_console()
    now = datetime.now(UTC)
    table = Table(show_header=True, box=None, pad_edge=False, padding=(0, 2))
    table.add_column("id", no_wrap=True, style="cyan")
    table.add_column("type", no_wrap=True)
    table.add_column("age", no_wrap=True)
    table.add_column("files", no_wrap=True, justify="right")
    table.add_column("plugins", no_wrap=True, justify="right")
    table.add_column("codex", no_wrap=True, justify="right")
    table.add_column("ext", no_wrap=True, justify="right")
    table.add_column("ownership", no_wrap=True, justify="right")
    for entry in listings:
        table.add_row(
            entry.directory.name,
            entry.command,
            _compact_age(entry.timestamp, now),
            str(entry.file_count),
            str(entry.plugin_count),
            str(entry.codex_plugin_count),
            str(entry.ext_count),
            str(entry.ownership_transfer_count),
        )
    if profile_filter:
        header = f"=== transitions for profile {', '.join(profile_filter)} ==="
    else:
        header = "=== transitions (all profiles) ==="
    console.print(header)
    console.print(table)
    sample = listings[0]
    sample_profile = profile_filter[0] if profile_filter else sample.profile
    console.print("=== to view details ===")
    console.print(f"  setforge transitions show {sample.directory.name}")
    console.print("=== to revert to BEFORE a specific transition ===")
    console.print(
        f"  setforge revert --profile={sample_profile} "
        f"--to-before={sample.directory.name}"
    )


@transitions_app.command("show", epilog=TRANSITIONS_SHOW_EXAMPLES)
def transitions_show(
    prefix: str = typer.Argument(..., help="Dirname or unique-prefix match."),
) -> None:
    """Show the full audit-detail panel for one transition (mockup H)."""
    target = transitions.resolve_transition_prefix(prefix)
    meta = transitions.load_meta(target)
    console = _wide_console()
    profile = meta.profile
    console.print(f"=== transition {target.name} ===")
    console.print(f"  type:    {meta.command}")
    console.print(f"  profile: {profile}")
    # Render in the user's local timezone per mockup H
    # (e.g. "2026-05-17 18:47:33 +0100"); insert a colon into the
    # %z offset for readability.
    local_strftime = meta.timestamp.astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    if len(local_strftime) >= 5 and local_strftime[-5] in "+-":
        local_strftime = local_strftime[:-2] + ":" + local_strftime[-2:]
    console.print(f"  start:   {local_strftime}")
    console.print(f"  host:    {meta.host}")
    console.print(f"  version: {meta.version}")
    # Render the forward-compat fields populated by
    # install/sync/revert/wizard make_meta call sites. Each field is
    # omit-when-None in the on-disk shape (and thus None on the loaded
    # dataclass), so a None here is the pre-bump backward-compat path —
    # silently skip.
    if meta.end_timestamp is not None:
        end_ts = datetime.fromisoformat(meta.end_timestamp)
        end_local = end_ts.astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
        if len(end_local) >= 5 and end_local[-5] in "+-":
            end_local = end_local[:-2] + ":" + end_local[-2:]
        console.print(f"  end:     {end_local}")
    if meta.command_line is not None:
        console.print(f"  argv:    {' '.join(meta.command_line)}")

    _render_files_section_show(target, console)
    _render_plugins_section_show(target, console)
    _render_codex_plugins_section_show(target, console)
    _render_extensions_section_show(target, console)
    _render_ownership_transfers_show(target, console)

    console.print("=== reverse this transition ===")
    console.print(f"  setforge revert --profile={profile} --to-before={target.name}")
    console.print(
        "    (will undo this transition AND every newer transition for this profile)"
    )


def _render_ownership_transfers_show(
    target: transitions.TransitionDir, console: Console
) -> None:
    deltas = transitions.load_ownership_transfers(target)
    if not deltas:
        return
    console.print("=== ownership transfers ===")
    for item in deltas:
        console.print(f"  resource: {item.after.resource_id.canonical()}")
        console.print(f"    from: {item.before.owner_id}")
        console.print(f"    to:   {item.after.owner_id}")
        console.print(
            f"    generation: {item.before.generation} -> {item.after.generation}"
        )


def _render_files_section_show(
    target: transitions.TransitionDir, console: Console
) -> None:
    """Render the ``files mutated (N):`` block with per-file diff stats."""
    file_actions = transitions.summarize_transition(target)
    if not file_actions:
        return
    patch_file = target / "changes.patch"
    diff_summaries: dict[str, str] = {}
    if patch_file.exists():
        diff_summaries = _diff_summaries_from_patch(
            patch_file.read_text(encoding="utf-8", errors="surrogateescape")
        )
    sorted_items = sorted(file_actions.items())
    console.print(f"  files mutated ({len(sorted_items)}):")
    action_marker = {"created": "+", "deleted": "-", "modified": "M"}
    for path, action in sorted_items:
        marker = action_marker.get(action, "?")
        stats = diff_summaries.get(path, "")
        suffix = f"  diff: {stats}" if stats else ""
        console.print(f"    {marker}  {path}{suffix}")


def _render_plugins_section_show(
    target: transitions.TransitionDir, console: Console
) -> None:
    """Render the ``plugins:`` block if a plugins.json sidecar exists."""
    delta = transitions.load_plugin_delta(target)
    if delta is None or delta.is_empty():
        return
    console.print("  plugins:")
    for plugin_id in delta.installed:
        console.print(f"    + {plugin_id}  (installed)")
    for plugin_id in delta.enabled:
        console.print(f"    + {plugin_id}  (enabled)")
    for plugin_id in delta.disabled:
        console.print(f"    - {plugin_id}  (disabled)")
    for name in delta.marketplaces_added:
        console.print(f"    + marketplace:{name}")
    for name, _source in delta.marketplaces_removed:
        console.print(f"    - marketplace:{name}")


def _render_codex_plugins_section_show(
    target: transitions.TransitionDir, console: Console
) -> None:
    delta = transitions.load_codex_plugin_delta(target)
    if delta is None or delta.is_empty():
        return
    console.print("  Codex plugins:")
    for plugin_id in delta.installed:
        console.print(f"    + {plugin_id}  (installed)")
    for plugin_id in delta.removed:
        console.print(f"    - {plugin_id}  (removed)")
    for name in delta.marketplaces_added:
        console.print(f"    + marketplace:{name}")
    for name, _source in delta.marketplaces_removed:
        console.print(f"    - marketplace:{name}")


def _render_extensions_section_show(
    target: transitions.TransitionDir, console: Console
) -> None:
    """Render the ``extensions:`` block if an extensions.json sidecar exists."""
    delta = transitions.load_extension_delta(target)
    if delta is None or delta.is_empty():
        return
    console.print("  extensions:")
    for ext_id in delta.added:
        console.print(f"    + {ext_id}  (installed)")
    for ext_id in delta.removed:
        console.print(f"    - {ext_id}  (uninstalled)")
