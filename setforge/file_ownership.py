"""Ownership decisions for logical tracked-file containers."""

from __future__ import annotations

import hashlib
import json
import stat
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from setforge.ownership import (
    Authority,
    ClaimLifecycle,
    OwnershipClaim,
    OwnershipError,
    OwnershipStore,
    ProvenanceFact,
    ProvenanceFactKind,
    ResourceId,
    ResourceScope,
)


class FileAction(StrEnum):
    """Effects permitted by one frozen file ownership decision."""

    INSTALL = "install"
    ADOPT = "adopt"
    MANAGE = "manage"
    REVIEW = "review"
    HOLD = "hold"


@dataclass(frozen=True, slots=True)
class FileObservation:
    """One no-follow logical destination plus its effective file content."""

    resource_id: ResourceId
    locator: str
    present: bool
    object_kind: str
    mode: int | None
    symlink_target: str | None
    content_hash: str | None
    fingerprint: str
    topology: bool = False


@dataclass(frozen=True, slots=True)
class FileDecision:
    """Authority-aware action for one tracked-file container."""

    observation: FileObservation
    claim: OwnershipClaim | None
    action: FileAction
    detail: str


def file_resource_id(destination: Path) -> ResourceId:
    """Create an alias-safe identity stable across parent-directory creation."""
    absolute = destination.absolute()
    if not absolute.name:
        raise OwnershipError("file destination cannot be a filesystem root")
    logical = absolute.parent.resolve(strict=False) / absolute.name
    parts = logical.parts
    if len(parts) < 3:
        raise OwnershipError("file destination must be below a target root")
    target_root = Path(parts[0]) / parts[1]
    coordinate = logical.relative_to(target_root).as_posix()
    return ResourceId(
        kind="file",
        provider="tracked",
        coordinate=coordinate,
        scope=ResourceScope.target_root(target_root),
    )


def _file_fingerprint(
    resource_id: ResourceId,
    *,
    present: bool,
    object_kind: str,
    mode: int | None,
    symlink_target: str | None,
    content_hash: str | None,
) -> str:
    payload = json.dumps(
        {
            "content_hash": content_hash,
            "mode": mode,
            "object_kind": object_kind,
            "present": present,
            "resource": resource_id.canonical(),
            "symlink_target": symlink_target,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def observe_file(destination: Path, *, allow_topology: bool = False) -> FileObservation:
    """Observe one destination without following its logical leaf implicitly."""
    absolute = destination.absolute()
    resource_id = file_resource_id(absolute)
    try:
        logical = absolute.lstat()
    except FileNotFoundError:
        fingerprint = _file_fingerprint(
            resource_id,
            present=False,
            object_kind="absent",
            mode=None,
            symlink_target=None,
            content_hash=None,
        )
        return FileObservation(
            resource_id,
            str(absolute),
            False,
            "absent",
            None,
            None,
            None,
            fingerprint,
            allow_topology,
        )

    symlink_target: str | None = None
    if stat.S_ISLNK(logical.st_mode):
        symlink_target = str(absolute.readlink())
        try:
            effective = absolute.stat()
        except FileNotFoundError:
            if not allow_topology:
                raise OwnershipError(
                    f"tracked destination is a dangling symlink: {absolute}"
                ) from None
            effective = None
        object_kind = "symlink"
    else:
        effective = logical
        object_kind = "directory" if stat.S_ISDIR(logical.st_mode) else "file"
    if effective is not None and not stat.S_ISREG(effective.st_mode):
        if allow_topology and stat.S_ISDIR(effective.st_mode):
            content_hash = None
            mode = stat.S_IMODE(effective.st_mode)
            fingerprint = _file_fingerprint(
                resource_id,
                present=True,
                object_kind=object_kind,
                mode=mode,
                symlink_target=symlink_target,
                content_hash=content_hash,
            )
            return FileObservation(
                resource_id,
                str(absolute),
                True,
                object_kind,
                mode,
                symlink_target,
                content_hash,
                fingerprint,
                True,
            )
        raise OwnershipError(f"tracked destination is not a regular file: {absolute}")

    content_hash = (
        hashlib.sha256(absolute.read_bytes()).hexdigest()
        if effective is not None
        else None
    )
    mode = stat.S_IMODE((effective or logical).st_mode)
    fingerprint = _file_fingerprint(
        resource_id,
        present=True,
        object_kind=object_kind,
        mode=mode,
        symlink_target=symlink_target,
        content_hash=content_hash,
    )
    return FileObservation(
        resource_id,
        str(absolute),
        True,
        object_kind,
        mode,
        symlink_target,
        content_hash,
        fingerprint,
        allow_topology,
    )


def observe_tree(destination: Path, inventory_fingerprint: str) -> FileObservation:
    """Bind one no-follow directory root to its canonical entry inventory."""
    absolute = destination.absolute()
    resource_id = file_resource_id(absolute)
    try:
        logical = absolute.lstat()
    except FileNotFoundError:
        present = False
        mode = None
        object_kind = "absent-tree"
        content_hash = None
    else:
        if not stat.S_ISDIR(logical.st_mode) or stat.S_ISLNK(logical.st_mode):
            raise OwnershipError(
                f"managed tree root is not a real directory: {absolute}"
            )
        present = True
        mode = stat.S_IMODE(logical.st_mode)
        object_kind = "tree"
        content_hash = inventory_fingerprint
    fingerprint = _file_fingerprint(
        resource_id,
        present=present,
        object_kind=object_kind,
        mode=mode,
        symlink_target=None,
        content_hash=content_hash,
    )
    return FileObservation(
        resource_id,
        str(absolute),
        present,
        object_kind,
        mode,
        None,
        content_hash,
        fingerprint,
        True,
    )


def decide_file(
    observation: FileObservation,
    claim: OwnershipClaim | None,
    *,
    owner_id: uuid.UUID,
    protected_units: bool = False,
) -> FileDecision:
    """Choose an effect without deriving authority from bytes or declarations."""
    if claim is not None and claim.resource_id != observation.resource_id:
        raise OwnershipError("file claim identity does not match observation")
    if claim is None:
        if observation.present:
            return FileDecision(
                observation, claim, FileAction.ADOPT, "present, external, unowned"
            )
        return FileDecision(observation, claim, FileAction.INSTALL, "absent")
    if claim.owner_id != owner_id:
        return FileDecision(
            observation, claim, FileAction.HOLD, "claimed by another configuration"
        )
    if (
        claim.authority is not Authority.MANAGE
        or claim.lifecycle is not ClaimLifecycle.CLAIMED
    ):
        return FileDecision(observation, claim, FileAction.HOLD, "released, unowned")
    if not observation.present:
        if protected_units:
            return FileDecision(
                observation,
                claim,
                FileAction.HOLD,
                "missing file retains protected LOCAL or PENDING units",
            )
        return FileDecision(
            observation, claim, FileAction.INSTALL, "managed file missing"
        )
    if claim.fingerprint != observation.fingerprint:
        return FileDecision(
            observation,
            claim,
            FileAction.REVIEW,
            "managed file changed since ownership was recorded",
        )
    return FileDecision(observation, claim, FileAction.MANAGE, "managed and current")


def publish_file_claim_locked(
    store: OwnershipStore,
    decision: FileDecision,
    *,
    owner_id: uuid.UUID,
    declaration_ref: str,
    acquisition: str,
    provenance: tuple[ProvenanceFact, ...] = (),
) -> OwnershipClaim:
    """Publish one exact observed container claim under the resources lock."""
    if not decision.observation.present:
        raise OwnershipError("cannot adopt an absent tracked file")
    return store.claim_locked(
        resource_id=decision.observation.resource_id,
        owner_id=owner_id,
        declaration_refs=(declaration_ref,),
        provenance=(
            ProvenanceFact(ProvenanceFactKind.ORIGIN, "filesystem"),
            ProvenanceFact(ProvenanceFactKind.ACQUISITION, acquisition),
            *provenance,
        ),
        locator=decision.observation.locator,
        fingerprint=decision.observation.fingerprint,
        expected_generation=(
            decision.claim.generation if decision.claim is not None else None
        ),
    )
