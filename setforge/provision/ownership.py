"""Package-specific decisions over discovery and durable ownership claims."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from enum import StrEnum

from setforge.ownership import (
    Authority,
    ClaimLifecycle,
    OwnershipClaim,
    OwnershipStore,
    ProvenanceFact,
    ProvenanceFactKind,
    ResourceId,
)
from setforge.provision.protocol import PackageObservation, ProvisionItem


class PackageAction(StrEnum):
    """The only actions package reconciliation may select."""

    INSTALL = "install"
    ADOPT = "adopt"
    UPGRADE = "upgrade"
    HOLD = "hold"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class PackageDecision:
    """One exact ownership-aware package decision."""

    item: ProvisionItem
    resource_id: ResourceId
    observation: PackageObservation | None
    claim: OwnershipClaim | None
    action: PackageAction
    detail: str


def package_resource_id(item: ProvisionItem) -> ResourceId:
    """Return the canonical user-host identity for a provision item."""
    return ResourceId.package(item.type, item.identity.key)


def observation_fingerprint(observation: PackageObservation) -> str:
    """Hash the complete normalized observation used by ownership CAS."""
    payload = json.dumps(
        {
            "artifact": observation.artifact,
            "checksum": observation.checksum,
            "fingerprint": observation.fingerprint,
            "identity": observation.identity.key,
            "locator": observation.locator,
            "origin": observation.origin.value,
            "platform": observation.platform,
            "source": observation.source,
            "version": observation.version,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def decide_package(
    item: ProvisionItem,
    observation: PackageObservation | None,
    claim: OwnershipClaim | None,
    *,
    owner_id: uuid.UUID,
) -> PackageDecision:
    """Choose an effect without treating presence or provenance as authority."""
    resource_id = package_resource_id(item)
    if observation is None:
        if claim is not None and (
            claim.owner_id != owner_id
            or claim.authority is not Authority.MANAGE
            or claim.lifecycle is not ClaimLifecycle.CLAIMED
        ):
            return PackageDecision(
                item,
                resource_id,
                None,
                claim,
                PackageAction.HOLD,
                "missing package has no current matching management claim",
            )
        return PackageDecision(
            item,
            resource_id,
            None,
            claim,
            PackageAction.INSTALL,
            "absent" if claim is None else "managed package missing; reinstall",
        )
    if claim is None:
        return PackageDecision(
            item,
            resource_id,
            observation,
            None,
            PackageAction.ADOPT,
            "present, external, unowned",
        )
    if claim.owner_id != owner_id:
        return PackageDecision(
            item,
            resource_id,
            observation,
            claim,
            PackageAction.HOLD,
            "present, claimed by another configuration",
        )
    if (
        claim.authority is not Authority.MANAGE
        or claim.lifecycle is not ClaimLifecycle.CLAIMED
    ):
        return PackageDecision(
            item,
            resource_id,
            observation,
            claim,
            PackageAction.HOLD,
            "present, released, unowned",
        )
    if claim.fingerprint != observation_fingerprint(observation):
        return PackageDecision(
            item,
            resource_id,
            observation,
            claim,
            PackageAction.HOLD,
            "present package drifted since ownership was recorded",
        )
    desired_matches = (
        item.version is None or item.version == observation.version
    ) and (item.checksum is None or item.checksum == observation.checksum)
    desired_matches = desired_matches and (
        item.artifact is None or item.artifact == observation.artifact
    )
    desired_matches = desired_matches and (
        item.platform is None or item.platform == observation.platform
    )
    return PackageDecision(
        item,
        resource_id,
        observation,
        claim,
        PackageAction.NONE if desired_matches else PackageAction.UPGRADE,
        "managed and current" if desired_matches else "managed upgrade required",
    )


def observation_provenance(
    observation: PackageObservation, *, acquisition: str
) -> tuple[ProvenanceFact, ...]:
    """Translate provider evidence into cumulative ledger facts."""
    values = [
        ProvenanceFact(ProvenanceFactKind.ORIGIN, observation.origin.value),
        ProvenanceFact(ProvenanceFactKind.ACQUISITION, acquisition),
    ]
    if observation.source is not None:
        values.append(ProvenanceFact(ProvenanceFactKind.RESOLVER, observation.source))
    if observation.version is not None:
        values.append(ProvenanceFact(ProvenanceFactKind.ARTIFACT, observation.version))
    if observation.checksum is not None:
        values.append(
            ProvenanceFact(ProvenanceFactKind.INTEGRITY, observation.checksum)
        )
    if observation.artifact is not None:
        values.append(ProvenanceFact(ProvenanceFactKind.ARTIFACT, observation.artifact))
    if observation.platform is not None:
        values.append(ProvenanceFact(ProvenanceFactKind.PLATFORM, observation.platform))
    return tuple(values)


def publish_claim_locked(
    store: OwnershipStore,
    decision: PackageDecision,
    *,
    owner_id: uuid.UUID,
    declaration_ref: str,
    acquisition: str,
) -> OwnershipClaim:
    """Publish a claim for an exact observed package under the resources lock."""
    if decision.observation is None:
        raise ValueError("cannot claim a package without a current observation")
    observation = decision.observation
    return store.claim_locked(
        resource_id=decision.resource_id,
        owner_id=owner_id,
        declaration_refs=(declaration_ref,),
        provenance=observation_provenance(observation, acquisition=acquisition),
        locator=observation.locator or observation.identity.display,
        fingerprint=observation_fingerprint(observation),
        expected_generation=(
            decision.claim.generation if decision.claim is not None else None
        ),
    )
