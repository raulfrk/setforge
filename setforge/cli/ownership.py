"""Inspect and explicitly change durable SetForge ownership authority."""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from setforge.cli import (
    _CONFIG_OPTION,
    _require_output_path,
    _resolve_config_arg,
    app,
)
from setforge.cli._help_examples import (
    OWNERSHIP_HISTORY_EXAMPLES,
    OWNERSHIP_LIST_EXAMPLES,
    OWNERSHIP_RECOVER_EXAMPLES,
    OWNERSHIP_RELEASE_EXAMPLES,
    OWNERSHIP_REVERT_EXAMPLES,
)
from setforge.cli._output import render
from setforge.compare import expand_tracked_file, resolve_dst, resolve_src
from setforge.config import (
    Config,
    ExtensionPackage,
    PluginPackage,
    TreePolicy,
    load_config,
)
from setforge.errors import ConfirmRequiresInteractive, OwnershipError
from setforge.file_ownership import observe_file, observe_tree
from setforge.locking import MutationLockGuards, mutation_locks
from setforge.ownership import (
    ClaimLifecycle,
    OwnershipClaim,
    OwnershipStore,
    ResourceId,
    read_owner_id,
    read_owner_id_locked,
    resolve_owner_common_dir,
)
from setforge.ownership_history import OwnershipHistoryStore, OwnershipTransition
from setforge.provision.bundle import resolve_bundle_items
from setforge.provision.identity import package_identity
from setforge.provision.ownership import observation_fingerprint, package_resource_id
from setforge.provision.protocol import ProvisionItem
from setforge.provision.registry import build
from setforge.tree_management import scan_tree

ownership_app: typer.Typer = typer.Typer(
    help="Inspect, release, reverse, and recover durable ownership authority.",
    no_args_is_help=True,
    rich_markup_mode=None,
)
app.add_typer(ownership_app, name="ownership")


@dataclass(frozen=True, slots=True)
class _AuthorityValidationPlan:
    """Config snapshot and exact lock scopes for one possible authority grant."""

    config_digest: str
    target_roots: tuple[Path, ...]


@ownership_app.callback()
def _ownership_output_contract(ctx: typer.Context) -> None:
    """Enforce structured output only for read-only ownership leaves."""
    if ctx.invoked_subcommand is not None:
        _require_output_path(ctx.obj, ("ownership", ctx.invoked_subcommand))


@ownership_app.command("list", epilog=OWNERSHIP_LIST_EXAMPLES)
def ownership_list(ctx: typer.Context) -> None:
    """List every durable ownership claim without loading configuration."""
    store = OwnershipStore()
    entries = [_claim_data(store, claim) for claim in store.list_claims()]

    def _human() -> None:
        if not entries:
            typer.echo("(no ownership claims)")
            return
        for entry in entries:
            typer.echo(entry["claim_id"])
            typer.echo(f"  resource:     {entry['resource_id']}")
            typer.echo(
                f"  scope:        {entry['scope']['kind']}:{entry['scope']['key']}"
            )
            typer.echo(f"  locator:      {entry['locator']}")
            typer.echo(f"  owner:        {entry['owner_id']}")
            typer.echo(
                f"  state:        {entry['lifecycle']} / {entry['authority']} "
                f"(generation {entry['generation']})"
            )
            typer.echo(f"  declarations: {', '.join(entry['declaration_refs'])}")
            typer.echo(f"  fingerprint:  {entry['fingerprint']}")
            typer.echo(
                f"  history:      {entry['history_summary']['event_count']} events; "
                f"last={entry['history_summary']['last_action']}"
            )
            provenance = entry["provenance"]
            typer.echo(
                "  provenance:   "
                + (
                    ", ".join(f"{fact['kind']}={fact['value']}" for fact in provenance)
                    if provenance
                    else "none"
                )
            )

    render(ctx.obj, "ownership list", {"claims": entries}, human_fn=_human)


@ownership_app.command("release", epilog=OWNERSHIP_RELEASE_EXAMPLES)
def ownership_release(
    claim_id: str = typer.Argument(..., help="Full 64-character ownership claim ID."),
    config: Path = _CONFIG_OPTION,
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Confirm release non-interactively."
    ),
) -> None:
    """Release authority for one claim without observing or changing the resource."""
    config_path = _resolved_config(config)
    owner_id = read_owner_id(config_path.parent)
    ledger = OwnershipStore()
    preview = ledger.read_claim_id(claim_id)
    if preview is None:
        raise OwnershipError("ownership claim not found")
    if preview.owner_id != owner_id:
        raise OwnershipError("ownership claim is not held by the current config owner")
    if preview.lifecycle is not ClaimLifecycle.CLAIMED:
        raise OwnershipError("ownership claim is already released")
    typer.echo(f"release ownership only (resource is preserved): {claim_id}")
    typer.echo(f"  {preview.resource_id.canonical()}")
    _confirm("release this ownership claim?", yes=yes, operation="ownership release")

    with mutation_locks(resources=True):
        if read_owner_id(config_path.parent) != owner_id:
            raise OwnershipError("config owner identity changed before release; retry")
        if ledger.read_claim_id(claim_id) != preview:
            raise OwnershipError("ownership claim changed after confirmation; retry")
        transition = OwnershipHistoryStore().release_locked(ledger, owner_id, claim_id)
    typer.echo(f"released ownership transition {transition.transition_id}")


@ownership_app.command("history", epilog=OWNERSHIP_HISTORY_EXAMPLES)
def ownership_history(
    ctx: typer.Context,
    transition_id: str | None = typer.Argument(
        None, help="Optional full transition UUID to show."
    ),
    config: Path = _CONFIG_OPTION,
) -> None:
    """List this checkout owner's transitions, or show one exact transition."""
    config_path = _resolved_config(config)
    owner_id = read_owner_id(config_path.parent)
    history = OwnershipHistoryStore()
    if transition_id is not None:
        transition = history.read(owner_id, transition_id)
        data = _transition_data(transition, detailed=True)

        def _human_one() -> None:
            _render_transition(transition, detailed=True)

        render(ctx.obj, "ownership history", data, human_fn=_human_one)
        return

    transitions = history.list(owner_id)
    data = {
        "owner_id": str(owner_id),
        "transitions": [
            _transition_data(transition, detailed=False) for transition in transitions
        ],
    }

    def _human_all() -> None:
        if not transitions:
            typer.echo("(no ownership transitions)")
            return
        for transition in transitions:
            _render_transition(transition, detailed=False)

    render(ctx.obj, "ownership history", data, human_fn=_human_all)


@ownership_app.command("revert", epilog=OWNERSHIP_REVERT_EXAMPLES)
def ownership_revert(
    transition_id: str = typer.Argument(..., help="Full ownership transition UUID."),
    config: Path = _CONFIG_OPTION,
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Confirm reversal non-interactively."
    ),
) -> None:
    """Reverse one exact current ownership transition."""
    config_path = _resolved_config(config)
    owner_id = read_owner_id(config_path.parent)
    history = OwnershipHistoryStore()
    preview = history.read(owner_id, transition_id)
    validation_plans = _prepare_authority_plans(
        config_path,
        (preview.after,) if preview.after.lifecycle is ClaimLifecycle.RELEASED else (),
    )
    target_roots = _planned_target_roots(validation_plans)
    typer.echo(
        f"reverse {preview.action.value} transition {preview.transition_id} "
        f"for {preview.after.resource_id.canonical()}"
    )
    _confirm(
        "reverse this ownership transition?",
        yes=yes,
        operation="ownership revert",
    )

    common_dir = resolve_owner_common_dir(config_path.parent)
    with mutation_locks(
        resources=True,
        config_identity_dir=common_dir,
        config_dir=config_path.parent if validation_plans else None,
        target_roots=target_roots,
    ) as mutation_guards:
        identity_guard = mutation_guards.config_identity
        if identity_guard is None:  # pragma: no cover - declared lock invariant
            raise OwnershipError("config owner identity lock was not acquired")
        if (
            read_owner_id_locked(config_path.parent, identity_guard.directory_fd)
            != owner_id
        ):
            raise OwnershipError("config owner identity changed before revert; retry")
        if history.read(owner_id, transition_id) != preview:
            raise OwnershipError("ownership transition changed after confirmation")
        reverted = history.revert_locked(
            OwnershipStore(),
            owner_id,
            transition_id,
            validate_authority=lambda claim: _validate_authority(
                config_path,
                claim,
                validation_plans.get(claim.resource_id),
                mutation_guards,
            ),
        )
    typer.echo(f"reverted ownership transition {reverted.transition_id}")


@ownership_app.command("recover", epilog=OWNERSHIP_RECOVER_EXAMPLES)
def ownership_recover(
    config: Path = _CONFIG_OPTION,
    apply: bool = typer.Option(
        False, "--apply", help="Complete unambiguous pending transitions."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Confirm recovery non-interactively."
    ),
) -> None:
    """Inspect or complete interrupted owner-scoped transition publication."""
    config_path = _resolved_config(config)
    owner_id = read_owner_id(config_path.parent)
    history = OwnershipHistoryStore()
    pending = history.pending(owner_id)
    if not pending:
        typer.echo("(no pending ownership transitions)")
        return
    for transition in pending:
        typer.echo(
            f"pending {transition.action.value} {transition.transition_id} "
            f"{transition.before.resource_id.canonical()}"
        )
    if not apply:
        typer.echo(
            f"recover with: setforge ownership recover --config={config_path} --apply"
        )
        return
    granting_claims = tuple(
        transition.before
        for transition in pending
        if transition.after.lifecycle is ClaimLifecycle.CLAIMED
    )
    validation_plans = _prepare_authority_plans(config_path, granting_claims)
    target_roots = _planned_target_roots(validation_plans)
    _confirm(
        "complete these ownership transitions?",
        yes=yes,
        operation="ownership recover --apply",
    )
    common_dir = resolve_owner_common_dir(config_path.parent)
    with mutation_locks(
        resources=True,
        config_identity_dir=common_dir,
        config_dir=config_path.parent if validation_plans else None,
        target_roots=target_roots,
    ) as mutation_guards:
        identity_guard = mutation_guards.config_identity
        if identity_guard is None:  # pragma: no cover - declared lock invariant
            raise OwnershipError("config owner identity lock was not acquired")
        if (
            read_owner_id_locked(config_path.parent, identity_guard.directory_fd)
            != owner_id
        ):
            raise OwnershipError("config owner identity changed before recovery; retry")
        recovered = history.recover_locked(
            OwnershipStore(),
            owner_id,
            validate_authority=lambda claim: _validate_authority(
                config_path,
                claim,
                validation_plans.get(claim.resource_id),
                mutation_guards,
            ),
        )
    suffix = "" if len(recovered) == 1 else "s"
    typer.echo(f"recovered {len(recovered)} ownership transition{suffix}")


def _resolved_config(config: Path) -> Path:
    return _resolve_config_arg(config).resolve()


def _confirm(prompt: str, *, yes: bool, operation: str) -> None:
    if yes:
        return
    if not sys.stdin.isatty():
        raise ConfirmRequiresInteractive(
            f"{operation} requires --yes when stdin is not a TTY"
        )
    if not typer.confirm(prompt, default=False):
        raise OwnershipError("ownership operation declined; no changes applied")


def _claim_data(store: OwnershipStore, claim: OwnershipClaim) -> dict[str, Any]:
    return {
        "authority": claim.authority.value,
        "claim_id": store.claim_id(claim.resource_id),
        "declaration_refs": list(claim.declaration_refs),
        "fingerprint": claim.fingerprint,
        "generation": claim.generation,
        "history_summary": {
            "event_count": len(claim.history),
            "last_action": claim.history[-1].action,
        },
        "lifecycle": claim.lifecycle.value,
        "locator": claim.locator,
        "owner_id": str(claim.owner_id),
        "provenance": [
            {"kind": fact.kind.value, "value": fact.value} for fact in claim.provenance
        ],
        "resource_id": claim.resource_id.canonical(),
        "scope": {
            "kind": claim.resource_id.scope.kind.value,
            "key": claim.resource_id.scope.key,
        },
    }


def _transition_data(
    transition: OwnershipTransition, *, detailed: bool
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "action": transition.action.value,
        "after_generation": transition.after.generation,
        "after_lifecycle": transition.after.lifecycle.value,
        "before_generation": transition.before.generation,
        "before_lifecycle": transition.before.lifecycle.value,
        "created_at": transition.created_at.isoformat(),
        "owner_id": str(transition.owner_id),
        "resource_id": transition.after.resource_id.canonical(),
        "reverts_transition_id": (
            None
            if transition.reverts_transition_id is None
            else str(transition.reverts_transition_id)
        ),
        "transition_id": str(transition.transition_id),
    }
    if detailed:
        store = OwnershipStore()
        data["before"] = _claim_data(store, transition.before)
        data["after"] = _claim_data(store, transition.after)
    return data


def _render_transition(transition: OwnershipTransition, *, detailed: bool) -> None:
    typer.echo(
        f"{transition.transition_id}  {transition.action.value}  "
        f"{transition.before.lifecycle.value}->{transition.after.lifecycle.value}  "
        f"generation {transition.before.generation}->{transition.after.generation}"
    )
    if detailed:
        typer.echo(f"  owner:    {transition.owner_id}")
        typer.echo(f"  resource: {transition.after.resource_id.canonical()}")
        typer.echo(f"  created:  {transition.created_at.isoformat()}")
        if transition.reverts_transition_id is not None:
            typer.echo(f"  reverts:  {transition.reverts_transition_id}")


def _prepare_authority_plans(
    config_path: Path,
    claims: tuple[OwnershipClaim, ...],
) -> dict[ResourceId, _AuthorityValidationPlan]:
    """Freeze config bytes and exact target-lock scopes before confirmation."""
    if not claims:
        return {}
    digest = _config_digest(config_path)
    cfg = load_config(config_path)
    plans = {
        claim.resource_id: _AuthorityValidationPlan(
            digest,
            _authority_target_roots(config_path, cfg, claim),
        )
        for claim in claims
    }
    if _config_digest(config_path) != digest:
        raise OwnershipError("configuration changed while planning ownership revert")
    return plans


def _planned_target_roots(
    plans: dict[ResourceId, _AuthorityValidationPlan],
) -> tuple[Path, ...]:
    return tuple(
        sorted(
            {target for plan in plans.values() for target in plan.target_roots},
            key=str,
        )
    )


def _authority_target_roots(
    config_path: Path,
    cfg: Config,
    claim: OwnershipClaim,
) -> tuple[Path, ...]:
    if claim.resource_id.kind == "file":
        destination, policy = _resolve_file_authority(config_path, cfg, claim)
        target = destination if policy is not None else destination.parent
        if not target.name:
            raise OwnershipError(
                f"ownership target cannot be locked safely: {destination}"
            )
        return (target.absolute(),)
    if claim.resource_id.kind == "package":
        _resolve_package_authority(cfg, claim)
        return ()
    raise OwnershipError(
        f"unsupported ownership resource kind for revert: {claim.resource_id.kind}"
    )


def _validate_authority(
    config_path: Path,
    claim: OwnershipClaim,
    plan: _AuthorityValidationPlan | None,
    mutation_guards: MutationLockGuards,
) -> None:
    """Reproduce config and live evidence inside the grant lock envelope."""
    if plan is None:
        raise OwnershipError("ownership authority grant was not in the frozen plan")
    if _config_digest(config_path) != plan.config_digest:
        raise OwnershipError("configuration changed after ownership confirmation")
    cfg = load_config(config_path)
    if _authority_target_roots(config_path, cfg, claim) != plan.target_roots:
        raise OwnershipError("ownership target scope changed after confirmation")
    if claim.resource_id.kind == "file":
        _validate_file_authority(config_path, cfg, claim)
    elif claim.resource_id.kind == "package":
        _validate_package_authority(cfg, claim)
    else:  # pragma: no cover - frozen-plan invariant
        raise OwnershipError(
            f"unsupported ownership resource kind for revert: {claim.resource_id.kind}"
        )
    if _config_digest(config_path) != plan.config_digest:
        raise OwnershipError("configuration changed during ownership validation; retry")
    mutation_guards.verify_targets()


def _validate_file_authority(
    config_path: Path, cfg: Config, claim: OwnershipClaim
) -> None:
    destination, policy = _resolve_file_authority(config_path, cfg, claim)
    if policy is not None:
        inventory = scan_tree(
            destination, policy.model_copy(update={"symlinks": "preserve"})
        ).inventory
        observed = observe_tree(destination, inventory.fingerprint)
    else:
        observed = observe_file(destination)
    if observed.resource_id != claim.resource_id:
        raise OwnershipError("tracked-file resource identity changed")
    if observed.fingerprint != claim.fingerprint:
        raise OwnershipError("tracked-file fingerprint changed")


def _resolve_file_authority(
    config_path: Path, cfg: Config, claim: OwnershipClaim
) -> tuple[Path, TreePolicy | None]:
    candidates: list[tuple[str, Path, TreePolicy | None]] = []
    for name, tracked in cfg.tracked_files.items():
        destination = resolve_dst(tracked)
        if tracked.tree is not None:
            candidates.append((f"tracked_files.{name}", destination, tracked.tree))
            continue
        source = resolve_src(tracked, config_path.parent)
        candidates.extend(
            (f"tracked_files.{sub_name}", sub_destination, None)
            for sub_name, _sub_source, sub_destination in expand_tracked_file(
                name, source, destination
            )
        )
    matches = [
        candidate
        for candidate in candidates
        if candidate[0] in claim.declaration_refs
        and str(candidate[1].absolute()) == claim.locator
    ]
    if len(matches) != 1:
        raise OwnershipError("tracked-file declaration no longer matches the claim")
    _ref, destination, policy = matches[0]
    return destination, policy


def _validate_package_authority(cfg: Config, claim: OwnershipClaim) -> None:
    item = _resolve_package_authority(cfg, claim)
    provider = build(item)
    observations = {
        observation.identity: observation
        for observation in provider.observations(provider.probe())
    }
    observed = observations.get(item.identity)
    if observed is None:
        raise OwnershipError("package resource is missing")
    if observation_fingerprint(observed) != claim.fingerprint:
        raise OwnershipError("package fingerprint changed")


def _resolve_package_authority(cfg: Config, claim: OwnershipClaim) -> ProvisionItem:
    declared: list[ProvisionItem] = []
    for package in cfg.packages.values():
        if isinstance(package, PluginPackage | ExtensionPackage):
            continue
        declared.append(
            ProvisionItem(
                type=package.type.value,
                identity=package_identity(package),
                config=package,
            )
        )
    declared.extend(
        item
        for bundle in cfg.bundles.values()
        for item in resolve_bundle_items(bundle, cfg)
    )
    matches = [
        item
        for item in declared
        if package_resource_id(item) == claim.resource_id
        and f"packages.{item.type}.{item.identity.key}" in claim.declaration_refs
    ]
    if not matches:
        raise OwnershipError("package declaration no longer matches the claim")
    return matches[0]


def _config_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise OwnershipError(
            f"cannot read configuration during ownership validation: {path}"
        ) from exc
