"""``cleanup`` subcommand — review and remove provisioned binaries."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from ruamel.yaml import YAML

from setforge import binaries, operations, transitions
from setforge.cli import (
    _CONFIG_OPTION,
    _PROFILE_OPTION,
    _resolve_config_arg,
    app,
)
from setforge.config import load_config, resolve_effective_profile
from setforge.locking import mutation_locks
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


def __getattr__(name: str) -> Any:  # noqa: ANN401
    if name == "button_bar":
        from setforge.ui.widgets import button_bar

        return button_bar
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class ConfinementError(Exception):
    pass


class CleanupAction(StrEnum):
    SKIP = "skip"
    DELETE = "delete"
    MARK_ORPHAN = "mark-orphan"


@dataclass(frozen=True, slots=True)
class CleanupItem:
    identity: Identity
    path: Path | None


def _receipt_store() -> ReceiptStore:
    return ReceiptStore(default_receipt_root())


def _confinement_root() -> Path:
    return Path.home()


def load_ignored_provisioned() -> frozenset[str]:
    path = binaries.LOCAL_CONFIG_PATH
    if not path.exists():
        return frozenset()
    try:
        data = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    except Exception:
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
    # Receipt-scoped only (no $PATH/FS scan); a corrupt receipt is skipped, not fatal.
    items: list[CleanupItem] = []
    for entry in store.iter_receipts():
        if entry.corrupt_path is not None:
            console.print(
                f"[yellow]warning:[/yellow] skipping corrupt receipt: "
                f"{entry.corrupt_path}"
            )
            continue
        assert entry.identity is not None
        if entry.identity in declared:
            continue
        items.append(CleanupItem(identity=entry.identity, path=entry.path))
    return items


def _lstat_safe(path: Path) -> bool:
    # lstat, never stat/resolve() — must not dereference a symlink before unlink.
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _confined_unlink(path: Path, *, confine_root: Path, console: Console) -> None:
    # resolve() the PARENT only: collapses ".." / symlink swaps a lexical
    # is_relative_to would wrongly pass, without resolving the link we unlink.
    root = confine_root.resolve()
    parent = path.parent.resolve()
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
    # Binary-unlink precedes receipt-drop so a crash mid-delete can't strand a gap.
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
    cfg = load_config(config_path)
    repo_root = config_path.resolve().parent
    resolved = resolve_effective_profile(cfg, profile, repo_root).resolved
    declared = {item.identity for item in resolve_provision_items(cfg, resolved)}
    declared |= {Identity(key=key, display=key) for key in load_ignored_provisioned()}
    return declared


def _pick_action(item: CleanupItem) -> CleanupAction:
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
    *,
    config_dir: Path | None = None,
) -> None:
    confine_root = _confinement_root()
    for item in items:
        action = _pick_action(item)
        if action is CleanupAction.SKIP:
            console.print(f"  skipped: {item.identity.display}")
            continue
        if action is CleanupAction.MARK_ORPHAN:
            mark_orphan(item.identity, console=console)
            continue
        # Serialize each delete (transition write + unlink) under
        # profile_lock, like every other mutating verb, so a concurrent
        # install/sync writing the same profile's state cannot interleave.
        # Per-item (not whole-loop): _pick_action above is interactive and
        # must not run while holding the lock.
        with mutation_locks(resources=True, config_dir=config_dir, profile=profile):
            operations.refuse_active(profile)
            transitions.ensure_state_dir_writable()
            meta = transitions.make_meta(
                transitions.TransitionCommand.CLEANUP_ORPHANS, profile
            )
            # file_pre == file_post == {path: None}, so the patch is empty:
            # this transition is an AUDIT MARKER, not a restore point. A
            # provisioned binary is re-obtainable via `install`, and binaries
            # are not text-patchable like tracked-file orphans, so `revert`
            # deliberately does NOT resurrect a cleanup-deleted binary.
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
    """Review and clean up undeclared provisioned binaries for ``profile``."""
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

    _apply_cleanup(
        profile,
        items,
        store,
        console,
        config_dir=resolved_config.resolve().parent,
    )
