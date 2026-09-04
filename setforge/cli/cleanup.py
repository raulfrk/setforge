"""``cleanup`` subcommand — review and remove provisioned binaries."""

from __future__ import annotations

import sys
import uuid
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
from setforge.config import (
    Config,
    ResolvedProfile,
    load_config,
    resolve_effective_profile,
)
from setforge.errors import SetforgeError
from setforge.locking import mutation_locks
from setforge.ownership import (
    Authority,
    ClaimLifecycle,
    OwnershipError,
    OwnershipStore,
    ResourceId,
    read_owner_id,
)
from setforge.provision.bundle import resolve_bundle_items
from setforge.provision.dispatch import resolve_provision_items
from setforge.provision.ownership import observation_fingerprint, package_resource_id
from setforge.provision.protocol import (
    Identity,
    ObservationOrigin,
    PackageObservation,
    ProvisionItem,
)
from setforge.provision.receipt import ReceiptEntry, ReceiptStore, default_receipt_root
from setforge.provision.registry import build

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
    provider: str | None = None
    managed: bool = True
    refusal: str = ""
    owner_id: uuid.UUID | None = None
    claim_generation: int | None = None


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


def _provision_ignore_id(identity: Identity, provider: str | None) -> str:
    return identity.key if provider is None else f"{provider}:{identity.key}"


def _ignore_receipt(
    entry: ReceiptEntry,
    ignored: frozenset[str],
    represented: set[ResourceId],
) -> bool:
    assert entry.identity is not None
    if _provision_ignore_id(entry.identity, entry.provider) not in ignored:
        return False
    if entry.provider is not None:
        represented.add(
            package_resource_id(
                ProvisionItem(type=entry.provider, identity=entry.identity)
            )
        )
    return True


def discover_cleanup_items(
    store: ReceiptStore,
    *,
    declared: set[Identity],
    console: Console,
    ownership_store: OwnershipStore | None = None,
    owner_id: uuid.UUID | None = None,
    declared_resources: frozenset[ResourceId] = frozenset(),
    ignored: frozenset[str] = frozenset(),
) -> list[CleanupItem]:
    # Receipt-scoped only (no $PATH/FS scan); a corrupt receipt is skipped, not fatal.
    items: list[CleanupItem] = []
    represented: set[ResourceId] = set()
    for entry in store.iter_receipts():
        if entry.corrupt_path is not None:
            console.print(
                f"[yellow]warning:[/yellow] skipping corrupt receipt: "
                f"{entry.corrupt_path}"
            )
            continue
        assert entry.identity is not None
        if _ignore_receipt(entry, ignored, represented) or (
            entry.provider is None and entry.identity in declared
        ):
            continue
        managed = ownership_store is None
        refusal = ""
        claim_generation: int | None = None
        if ownership_store is not None:
            if entry.provider is None:
                managed = False
                refusal = "legacy receipt is unverified"
            else:
                resource_id = package_resource_id(
                    ProvisionItem(type=entry.provider, identity=entry.identity)
                )
                if resource_id in declared_resources:
                    continue
                represented.add(resource_id)
                claim = ownership_store.read(resource_id)
                observation = PackageObservation(
                    entry.identity,
                    ObservationOrigin.CURRENT_RECEIPT,
                    version=entry.version,
                    locator=str(entry.path) if entry.path is not None else None,
                    fingerprint=entry.source_digest,
                    checksum=entry.checksum,
                )
                managed = bool(
                    claim is not None
                    and claim.owner_id == owner_id
                    and claim.authority is Authority.MANAGE
                    and claim.lifecycle is ClaimLifecycle.CLAIMED
                    and claim.fingerprint == observation_fingerprint(observation)
                )
                if not managed:
                    refusal = "no current matching ownership claim"
                elif claim is not None:
                    claim_generation = claim.generation
        items.append(
            CleanupItem(
                identity=entry.identity,
                path=entry.path,
                provider=entry.provider,
                managed=managed,
                refusal=refusal,
                owner_id=owner_id if managed else None,
                claim_generation=claim_generation,
            )
        )
    if ownership_store is None:
        return items
    items.extend(
        _discover_claim_items(
            ownership_store,
            owner_id=owner_id,
            declared=declared,
            declared_resources=declared_resources,
            represented=represented,
        )
    )
    return items


def _discover_claim_items(
    ownership_store: OwnershipStore,
    *,
    owner_id: uuid.UUID | None,
    declared: set[Identity],
    declared_resources: frozenset[ResourceId],
    represented: set[ResourceId],
) -> list[CleanupItem]:
    items: list[CleanupItem] = []
    for claim in ownership_store.list_claims():
        resource_id = claim.resource_id
        if (
            resource_id.kind != "package"
            or resource_id in represented
            or resource_id in declared_resources
            or claim.owner_id != owner_id
            or claim.authority is not Authority.MANAGE
            or claim.lifecycle is not ClaimLifecycle.CLAIMED
        ):
            continue
        identity = Identity(resource_id.coordinate, resource_id.coordinate)
        provider = build(ProvisionItem(type=resource_id.provider, identity=identity))
        installed = provider.probe()
        observations = {
            observation.identity: observation
            for observation in provider.observations(installed)
        }
        observation = observations.get(identity)
        managed = bool(
            observation is not None
            and claim.fingerprint == observation_fingerprint(observation)
        )
        items.append(
            CleanupItem(
                identity=identity,
                path=Path(claim.locator) if claim.locator else None,
                provider=resource_id.provider,
                managed=managed,
                refusal="" if managed else "managed package evidence drifted",
                owner_id=owner_id if managed else None,
                claim_generation=claim.generation if managed else None,
            )
        )
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
    if not item.managed:
        raise ConfinementError(
            f"refusing to delete unowned package {item.identity.display}: "
            f"{item.refusal}"
        )
    # Binary-unlink precedes receipt-drop so a crash mid-delete can't strand a gap.
    if item.path is None:
        console.print(
            f"[yellow]warning:[/yellow] receipt has no recorded path, "
            f"dropping receipt only: {item.identity.display}"
        )
        store.remove(item.identity, provider=item.provider)
        return
    _confined_unlink(item.path, confine_root=confine_root, console=console)
    store.remove(item.identity, provider=item.provider)


def mark_orphan(
    identity: Identity, *, provider: str | None = None, console: Console
) -> None:
    path = binaries.LOCAL_CONFIG_PATH
    yaml = YAML(typ="rt")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = yaml.load(path.read_text(encoding="utf-8")) if path.exists() else None
    if not isinstance(data, dict):
        data = {}
    raw = data.get(_PROVISION_IGNORE_KEY)
    if not isinstance(raw, list):
        raw = []
    ignore_id = _provision_ignore_id(identity, provider)
    if ignore_id not in raw:
        raw.append(ignore_id)
    data[_PROVISION_IGNORE_KEY] = raw
    with path.open("w", encoding="utf-8") as fh:
        yaml.dump(data, fh)
    console.print(f"  marked orphan (kept binary): {identity.display}")


def _resolve_declared(config_path: Path, profile: str) -> set[Identity]:
    cfg = load_config(config_path)
    repo_root = config_path.resolve().parent
    resolved = resolve_effective_profile(cfg, profile, repo_root).resolved
    declared = {item.identity for item in _declared_package_items(cfg, resolved)}
    declared |= {Identity(key=key, display=key) for key in load_ignored_provisioned()}
    return declared


def _resolve_declared_resources(
    config_path: Path, profile: str
) -> frozenset[ResourceId]:
    cfg = load_config(config_path)
    repo_root = config_path.resolve().parent
    resolved = resolve_effective_profile(cfg, profile, repo_root).resolved
    return frozenset(
        package_resource_id(item) for item in _declared_package_items(cfg, resolved)
    )


def _declared_package_items(
    cfg: Config, resolved: ResolvedProfile
) -> list[ProvisionItem]:
    """Return direct and selected-bundle package declarations for cleanup."""
    direct = resolve_provision_items(cfg, resolved)
    bundle_items = [
        item
        for name in resolved.bundles
        for item in resolve_bundle_items(cfg.bundles[name], cfg)
    ]
    by_key: dict[tuple[str, str], ProvisionItem] = {}
    for item in (*direct, *bundle_items):
        key = (item.type, item.identity.key)
        previous = by_key.get(key)
        if previous is not None and (
            previous.version != item.version
            or previous.checksum != item.checksum
            or previous.config.model_dump_json() != item.config.model_dump_json()
        ):
            raise SetforgeError(
                f"package identity collision for {item.type}:{item.identity.key}; "
                "declarations disagree on source or integrity"
            )
        by_key[key] = item
    return list(by_key.values())


def _pick_action(item: CleanupItem) -> CleanupAction:
    from setforge.cli import cleanup as _self
    from setforge.ui.widgets import CANCEL, Button

    where = str(item.path) if item.path is not None else "(no recorded path)"
    buttons = [Button("skip (default)", CleanupAction.SKIP)]
    if item.managed:
        buttons.append(Button("delete managed package", CleanupAction.DELETE))
    buttons.append(Button("mark orphan (keep package)", CleanupAction.MARK_ORPHAN))
    choice = _self.button_bar(
        buttons,
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
            if (
                item.provider is not None
                and item.owner_id is not None
                and item.claim_generation is not None
            ):
                with mutation_locks(
                    resources=True, config_dir=config_dir, profile=profile
                ):
                    OwnershipStore().release_locked(
                        package_resource_id(
                            ProvisionItem(type=item.provider, identity=item.identity)
                        ),
                        expected_owner=item.owner_id,
                        expected_generation=item.claim_generation,
                    )
                    mark_orphan(item.identity, provider=item.provider, console=console)
            else:
                mark_orphan(item.identity, provider=item.provider, console=console)
            continue
        # Serialize each delete (transition write + unlink) under
        # profile_lock, like every other mutating verb, so a concurrent
        # install/sync writing the same profile's state cannot interleave.
        # Per-item (not whole-loop): _pick_action above is interactive and
        # must not run while holding the lock.
        with (
            mutation_locks(resources=True, config_dir=config_dir, profile=profile),
            operations.recover_on_error(profile, "cleanup"),
        ):
            operations.refuse_active(profile)
            ownership_store = OwnershipStore()
            if (
                item.provider is None
                or item.owner_id is None
                or item.claim_generation is None
            ):
                raise ConfinementError(
                    f"refusing to delete unverified package {item.identity.display}"
                )
            resource_id = package_resource_id(
                ProvisionItem(type=item.provider, identity=item.identity)
            )
            claim = ownership_store.read(resource_id)
            if (
                claim is None
                or claim.owner_id != item.owner_id
                or claim.generation != item.claim_generation
                or claim.authority is not Authority.MANAGE
                or claim.lifecycle is not ClaimLifecycle.CLAIMED
            ):
                raise ConfinementError(
                    f"ownership changed for {item.identity.display}; retry cleanup"
                )
            provider = build(ProvisionItem(type=item.provider, identity=item.identity))
            observations = {
                observation.identity: observation
                for observation in provider.observations(provider.probe())
            }
            observation = observations.get(item.identity)
            if (
                observation is None
                or observation_fingerprint(observation) != claim.fingerprint
            ):
                raise ConfinementError(
                    f"package evidence changed for {item.identity.display}; "
                    "refusing removal"
                )
            journal = operations.prepare(
                command="cleanup",
                profile=profile,
                config_dir=config_dir,
                resources_lock=True,
                command_line=("cleanup", "--apply"),
                paths=(),
            )
            journal = operations.begin_checkpoint(
                journal,
                name=f"remove-package-{item.provider}-{item.identity.key}",
                kind=operations.CheckpointKind.IRREVERSIBLE,
                recovery=(
                    "inspect the package manager and ownership claim; rerun install "
                    "to restore a missing managed package"
                ),
                paths=(),
                restore_state=False,
                restore_transitions=False,
                adapters=(),
            )
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
            if item.path is not None:
                delete_provisioned(
                    store, item, confine_root=confine_root, console=console
                )
            else:
                provider.uninstall_one(item.identity)
            if item.identity in provider.probe():
                raise SetforgeError(
                    f"package removal could not be verified for "
                    f"{item.identity.display}; ownership claim retained"
                )
            ownership_store.release_locked(
                resource_id,
                expected_owner=item.owner_id,
                expected_generation=item.claim_generation,
            )
            journal = operations.finish_checkpoint(journal)
            operations.complete(journal)


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
    declared_resources = _resolve_declared_resources(resolved_config, profile)
    try:
        owner_id = read_owner_id(resolved_config.resolve().parent)
    except OwnershipError:
        owner_id = None
    items = discover_cleanup_items(
        store,
        declared=declared,
        console=console,
        ownership_store=OwnershipStore(),
        owner_id=owner_id,
        declared_resources=declared_resources,
        ignored=load_ignored_provisioned(),
    )

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
