"""``cleanup`` subcommand — review and remove provisioned binaries.

A provisioned binary is a tool setforge installed for a list-less
ecosystem (cargo / go / github_release) and recorded in the install
receipt store. When such a tool is no longer declared by the resolved
profile, this wizard offers two safe actions per item:

- **delete** — a confined unlink of the recorded binary followed by the
  receipt drop. Ordering is crash-consistent: a transition record is
  written FIRST, then the binary is unlinked, then the receipt. The
  unlink is confined (:func:`_confined_unlink` refuses any path outside
  the confinement root), uses ``lstat`` (never ``stat``), removes the
  LINK not its target, and never recurses (no ``rmtree`` / ``removedirs``).
- **mark-orphan** — config-only bookkeeping: the binary is kept and its
  identity is appended to ``provision_ignore`` in
  ``~/.config/setforge/local.yaml`` so it stops being flagged. Zero
  filesystem writes touch the binary.

Discovery is receipt-scoped ONLY (no ``$PATH`` / filesystem scan). A
corrupt receipt is named + skipped, never fatal. The declared set is
re-resolved LIVE on every run — a still-declared item is never offered
for delete. Binaries are NEVER auto-pruned; deletion happens only
through this explicit, confirmation-gated wizard.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from ruamel.yaml import YAML

from setforge import binaries, transitions
from setforge.cli import (
    _CONFIG_OPTION,
    _PROFILE_OPTION,
    _resolve_config_arg,
    app,
)
from setforge.config import load_config, resolve_profile
from setforge.provision.dispatch import resolve_provision_items
from setforge.provision.protocol import Identity
from setforge.provision.receipt import ReceiptStore, default_receipt_root

__all__ = [
    "CleanupAction",
    "CleanupItem",
    "ConfinementError",
    "cleanup",
    "delete_provisioned",
    "discover_cleanup_items",
    "load_ignored_provisioned",
    "mark_orphan",
]

_PROVISION_IGNORE_KEY = "provision_ignore"


def __getattr__(name: str) -> Any:  # noqa: ANN401 — PEP 562 module hook returns Any
    if name == "button_bar":
        from setforge.ui.widgets import button_bar

        return button_bar
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class ConfinementError(Exception):
    """A recorded binary path escapes the confinement root — refuse to unlink."""


class CleanupAction(StrEnum):
    """User's per-item choice from the cleanup wizard.

    - ``SKIP`` — leave this item alone (default safe).
    - ``DELETE`` — confined unlink of the binary + receipt drop.
    - ``MARK_ORPHAN`` — keep the binary; append it to the host-local
      ignore list so it stops being flagged.
    """

    SKIP = "skip"
    DELETE = "delete"
    MARK_ORPHAN = "mark-orphan"


@dataclass(frozen=True, slots=True)
class CleanupItem:
    """One undeclared provisioned binary offered for cleanup.

    ``path`` is the receipt's recorded install path, or ``None`` for an
    old receipt written before the path field existed (delete then drops
    only the receipt).
    """

    identity: Identity
    path: Path | None


def _receipt_store() -> ReceiptStore:
    """The real receipt store (overridden in tests via monkeypatch)."""
    return ReceiptStore(default_receipt_root())


def _confinement_root() -> Path:
    """The confinement root for provisioned-binary deletes.

    User-scope provisioners land binaries under the user's home
    (``~/.cargo/bin``, ``~/.local/bin``, ``~/go/bin`` …), so the home
    directory is the sane confinement boundary: any recorded path outside
    it is refused before any unlink. Overridden in tests via monkeypatch.
    """
    return Path.home()


def load_ignored_provisioned() -> frozenset[str]:
    """Return the ``provision_ignore`` identity keys from local.yaml.

    Best-effort: a missing or malformed local.yaml yields an empty set
    (never crashes cleanup). Values are coerced to plain ``str`` so no
    ruamel round-trip objects leak into the returned set.
    """
    path = binaries.LOCAL_CONFIG_PATH
    if not path.exists():
        return frozenset()
    try:
        data = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    except Exception:  # malformed YAML must not crash cleanup
        return frozenset()
    if not isinstance(data, dict):
        return frozenset()
    raw = data.get(_PROVISION_IGNORE_KEY)
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(str(entry) for entry in raw)


def discover_cleanup_items(
    store: ReceiptStore, *, declared: set[Identity], console: Console
) -> list[CleanupItem]:
    """List undeclared provisioned binaries from the receipt store ONLY.

    Receipt-scoped discovery (no ``$PATH`` / filesystem scan). Each
    receipt is read one at a time: a corrupt receipt is named on
    ``console`` and skipped (never fatal). An item whose identity is in
    ``declared`` (the live-resolved declared set) is excluded — a
    still-declared tool is never offered for delete.
    """
    items: list[CleanupItem] = []
    for entry in store.iter_receipts():
        if entry.corrupt_path is not None:
            console.print(
                f"[yellow]warning:[/yellow] skipping corrupt receipt: "
                f"{entry.corrupt_path}"
            )
            continue
        assert entry.identity is not None  # non-corrupt entries always carry one
        if entry.identity in declared:
            continue
        items.append(CleanupItem(identity=entry.identity, path=entry.path))
    return items


def _lstat_safe(path: Path) -> bool:
    """Return True iff ``path`` is a live directory entry (via ``lstat``).

    Uses ``lstat`` (not ``stat``) so a symlink is detected as an existing
    entry without dereferencing — never ``resolve()`` before unlink, which
    would point at the user's real data.
    """
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _confined_unlink(path: Path, *, confine_root: Path, console: Console) -> None:
    """Unlink ``path`` after proving it is inside ``confine_root``.

    Safety, in order:

    1. Confinement on the RESOLVED parent directory — ``realpath`` on both
       ``confine_root`` and ``path.parent`` collapses any ``..`` and defeats
       a parent-directory symlink swap, so a lexical ``<home>/../../etc``
       escape (which bare ``is_relative_to`` would pass) raises
       :class:`ConfinementError` BEFORE any filesystem touch. The FINAL
       component is deliberately NOT resolved — it is the link we unlink,
       never follow.
    2. ``lstat`` existence probe — a missing binary logs a warning and
       returns (the caller still reaps the receipt); NEVER
       ``unlink(missing_ok=True)`` — the race is handled explicitly.
    3. ``unlink()`` removes the LINK, never a symlink's target; single
       file only (no ``rmtree`` / ``removedirs`` / parent walk).
    """
    root = Path(os.path.realpath(confine_root))
    parent = Path(os.path.realpath(path.parent))
    if not (parent == root or root in parent.parents):
        raise ConfinementError(
            f"refusing to delete {path}: outside confinement root {confine_root}"
        )
    target = parent / path.name
    if not _lstat_safe(target):
        console.print(
            f"[yellow]warning:[/yellow] binary missing, reaping receipt only: {path}"
        )
        return
    target.unlink()
    console.print(f"  deleted  {path}")


def delete_provisioned(
    store: ReceiptStore,
    item: CleanupItem,
    *,
    confine_root: Path,
    console: Console,
) -> None:
    """Delete one provisioned binary + its receipt (crash-consistent order).

    Ordering: the caller records accounting FIRST (the transition), THEN
    this unlinks the binary, THEN drops the receipt — so a crash mid-delete
    never leaves a receipt whose binary is already gone-and-unaccounted.

    - ``item.path is None`` (old receipt): skip the binary-unlink, drop
      only the receipt + warn.
    - a confinement escape raises :class:`ConfinementError` BEFORE any
      unlink AND before the receipt is dropped (nothing is mutated).
    - a missing binary warns + continues and still reaps the receipt.
    """
    if item.path is None:
        console.print(
            f"[yellow]warning:[/yellow] receipt has no recorded path, "
            f"dropping receipt only: {item.identity.display}"
        )
        store.remove(item.identity)
        return
    _confined_unlink(item.path, confine_root=confine_root, console=console)
    store.remove(item.identity)


def mark_orphan(identity: Identity, *, console: Console) -> None:
    """Record ``identity`` as orphaned — config-only, zero binary writes.

    Appends the identity key to ``provision_ignore`` in
    ``~/.config/setforge/local.yaml`` (round-trip loader preserves
    comments + ordering). The binary and its receipt are left untouched;
    the item simply stops being flagged by future discovery. Idempotent —
    re-marking an existing key is a no-op.
    """
    path = binaries.LOCAL_CONFIG_PATH
    yaml = YAML(typ="rt")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = yaml.load(path.read_text(encoding="utf-8")) if path.exists() else None
    if not isinstance(data, dict):
        data = {}
    raw = data.get(_PROVISION_IGNORE_KEY)
    if not isinstance(raw, list):
        raw = []
    if identity.key not in raw:
        raw.append(identity.key)
    data[_PROVISION_IGNORE_KEY] = raw
    with path.open("w", encoding="utf-8") as fh:
        yaml.dump(data, fh)
    console.print(f"  marked orphan (kept binary): {identity.display}")


def _resolve_declared(config_path: Path, profile: str) -> set[Identity]:
    """Re-resolve the profile LIVE and return its declared provision identities.

    Loaded fresh on every call — never a stale snapshot — so a tool
    re-declared since the last run is correctly excluded from the
    delete-offer set. The host-local ``provision_ignore`` keys are folded
    in so a marked-orphan item is treated as declared (stops being flagged).
    """
    cfg = load_config(config_path)
    resolved = resolve_profile(cfg, profile)
    declared = {item.identity for item in resolve_provision_items(cfg, resolved)}
    declared |= {Identity(key=key, display=key) for key in load_ignored_provisioned()}
    return declared


def _pick_action(item: CleanupItem) -> CleanupAction:
    """Prompt for one item's action via the arrow-key wizard.

    Esc / Ctrl-C (the :data:`~setforge.ui.widgets.CANCEL` sentinel) maps
    to :attr:`CleanupAction.SKIP` (the safe default). Lazily imports the
    widget so tests can monkeypatch ``button_bar`` on this module.
    """
    from setforge.cli import cleanup as _self
    from setforge.ui.widgets import CANCEL, Button

    where = str(item.path) if item.path is not None else "(no recorded path)"
    choice = _self.button_bar(
        [
            Button("skip (default)", CleanupAction.SKIP),
            Button("delete binary + receipt", CleanupAction.DELETE),
            Button("mark orphan (keep binary)", CleanupAction.MARK_ORPHAN),
        ],
        title=f"setforge cleanup — {item.identity.display}",
        body=f"Undeclared provisioned binary at {where}. What would you like to do?",
        initial=0,
    )
    if choice is CANCEL:
        return CleanupAction.SKIP
    return choice


def _print_dry_run(items: list[CleanupItem], console: Console) -> None:
    """Print the default-mode dry-run listing (nothing is mutated)."""
    if not items:
        console.print("=== no undeclared provisioned binaries ===")
        return
    console.print("=== DRY-RUN — nothing will be deleted ===")
    for item in items:
        where = str(item.path) if item.path is not None else "(no recorded path)"
        console.print(f"UNDECLARED  {item.identity.display}  →  {where}")
    console.print("=== rerun with --apply to choose delete / mark-orphan ===")


def _apply_cleanup(
    profile: str,
    items: list[CleanupItem],
    store: ReceiptStore,
    console: Console,
) -> None:
    """Drive the per-item apply loop (TTY wizard), executing each choice.

    For a DELETE choice the transition record is written FIRST (accounting),
    then the confined unlink + receipt drop run. Requires a TTY — a
    non-interactive ``--apply`` is refused by the caller.
    """
    confine_root = _confinement_root()
    for item in items:
        action = _pick_action(item)
        if action is CleanupAction.SKIP:
            console.print(f"  skipped: {item.identity.display}")
            continue
        if action is CleanupAction.MARK_ORPHAN:
            mark_orphan(item.identity, console=console)
            continue
        # DELETE — accounting first (transition), then binary, then receipt.
        transitions.ensure_state_dir_writable()
        meta = transitions.make_meta(
            transitions.TransitionCommand.CLEANUP_ORPHANS, profile
        )
        recorded = {item.path: None} if item.path is not None else {}
        transitions.write_transition(meta, recorded, dict(recorded), ext_delta=None)
        delete_provisioned(store, item, confine_root=confine_root, console=console)


@app.command("cleanup")
def cleanup(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Actually run the per-item delete / mark-orphan wizard. "
        "Without this, the command is a dry-run.",
    ),
) -> None:
    """Review and clean up undeclared provisioned binaries for ``profile``.

    Discovery is receipt-scoped: it lists binaries setforge recorded that
    the LIVE-resolved profile no longer declares. Default is a dry-run;
    ``--apply`` fires a per-item arrow-key wizard (skip / delete / mark
    orphan). Binaries are never auto-pruned — deletion happens only through
    this explicit, confirmation-gated path. ``--apply`` requires a TTY (the
    wizard is interactive).
    """
    resolved_config = _resolve_config_arg(config)
    console = Console(stderr=True)
    store = _receipt_store()

    declared = _resolve_declared(resolved_config, profile)
    items = discover_cleanup_items(store, declared=declared, console=console)

    if not apply:
        _print_dry_run(items, console)
        return

    if not items:
        console.print("=== no undeclared provisioned binaries ===")
        return

    if not sys.stdin.isatty():
        console.print(
            "[red]✗[/red] setforge cleanup --apply requires an interactive terminal"
        )
        raise typer.Exit(code=2)

    _apply_cleanup(profile, items, store, console)
