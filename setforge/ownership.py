"""Durable resource identity and ownership claims.

This module is deliberately independent from provisioner ``Identity``. Runtime
inventory matching and durable mutation authority have different collision and
migration contracts; callers must cross that boundary explicitly.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import stat
import subprocess
import unicodedata
import uuid
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from setforge.errors import (
    CorruptOwnershipState,
    OwnershipCollisionError,
    OwnershipError,
)
from setforge.locking import (
    TargetLockGuard,
    config_identity_lock,
    require_resources_lock,
)
from setforge.transitions import state_root

__all__ = [
    "Authority",
    "ClaimEvent",
    "ClaimLifecycle",
    "LegacyOwnershipEvidence",
    "OwnershipClaim",
    "OwnershipStore",
    "ProvenanceFact",
    "ProvenanceFactKind",
    "ResourceId",
    "ResourceScope",
    "ScopeKind",
    "load_or_create_owner_id",
    "read_owner_id",
    "scan_legacy_receipts",
    "scan_legacy_reconcile",
]

_SCHEMA = "1.0"
_FILE_MODE = 0o600
_OWNER_ID_RELATIVE = Path("setforge") / "owner-id"
_CLAIM_ACTIONS = frozenset({"claim", "move", "refresh", "release", "transfer"})


class ScopeKind(StrEnum):
    """Namespace in which a resource coordinate is unique."""

    USER_HOST = "user-host"
    TARGET_ROOT = "target-root"
    APPLICATION = "application"


class Authority(StrEnum):
    """Current permission granted by a claim."""

    NONE = "none"
    MANAGE = "manage"


class ClaimLifecycle(StrEnum):
    """Origin-neutral durable claim state."""

    CLAIMED = "claimed"
    RELEASED = "released"


class ProvenanceFactKind(StrEnum):
    """Closed provenance fact names retained without precedence loss."""

    ORIGIN = "origin"
    ACQUISITION = "acquisition"
    GENERATOR = "generator"
    RESOLVER = "resolver"
    ARTIFACT = "artifact"
    INTEGRITY = "integrity"
    PLATFORM = "platform"


@dataclass(frozen=True, slots=True, order=True)
class ProvenanceFact:
    """One validated provenance observation."""

    kind: ProvenanceFactKind
    value: str

    def __post_init__(self) -> None:
        _require_text(self.value, field="provenance value")


@dataclass(frozen=True, slots=True, order=True, init=False)
class ResourceScope:
    """Typed target scope for one durable resource."""

    kind: ScopeKind
    key: str

    def __init__(self, kind: ScopeKind, key: str) -> None:
        if kind is ScopeKind.TARGET_ROOT:
            raise OwnershipError(
                "target-root scopes must be created from verified filesystem state"
            )
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "key", key)
        self._validate()

    @classmethod
    def _from_wire(cls, kind: ScopeKind, key: str) -> ResourceScope:
        scope = object.__new__(cls)
        object.__setattr__(scope, "kind", kind)
        object.__setattr__(scope, "key", key)
        scope._validate()
        return scope

    @classmethod
    def target_root(cls, target: Path) -> ResourceScope:
        """Create an alias-safe scope from verified filesystem identity.

        Existing targets use device/inode identity, unifying symlink and bind
        aliases. Missing targets use a stable parent-identity/leaf coordinate;
        after creation callers explicitly move that claim to the object scope.
        """
        absolute = target.absolute()
        if not absolute.name:
            raise OwnershipError("target-root scope requires a non-root path")
        parent = absolute.parent.resolve(strict=True)
        try:
            resolved = absolute.resolve(strict=True)
        except FileNotFoundError:
            try:
                leaf = absolute.lstat()
            except FileNotFoundError:
                leaf = None
            if leaf is not None:
                raise OwnershipError(
                    "target-root scope refuses a dangling symlink"
                ) from None
            info = parent.stat()
            leaf_digest = hashlib.sha256(absolute.name.encode()).hexdigest()[:24]
            return cls._from_wire(
                ScopeKind.TARGET_ROOT,
                f"coordinate:{info.st_dev}:{info.st_ino}:{leaf_digest}",
            )
        info = resolved.stat()
        if not stat.S_ISDIR(info.st_mode):
            raise OwnershipError("target-root scope requires a directory")
        return cls._from_wire(
            ScopeKind.TARGET_ROOT, f"object:{info.st_dev}:{info.st_ino}"
        )

    @classmethod
    def target_root_guarded(cls, guard: TargetLockGuard) -> ResourceScope:
        """Create an object scope from the descriptor held by a target lock."""
        guard.verify_expected()
        if guard.target_fd is None:
            raise OwnershipError("target-root object does not exist")
        info = os.fstat(guard.target_fd)
        if not stat.S_ISDIR(info.st_mode):
            raise OwnershipError("target-root scope requires a directory")
        return cls._from_wire(
            ScopeKind.TARGET_ROOT, f"object:{info.st_dev}:{info.st_ino}"
        )

    def _validate(self) -> None:
        _require_text(self.key, field="scope key")
        if unicodedata.normalize("NFC", self.key) != self.key:
            raise OwnershipError("scope key is not Unicode-canonical")
        if self.kind is ScopeKind.USER_HOST and self.key != "current-user":
            raise OwnershipError("user-host scope must use the current-user key")
        if (
            self.kind is ScopeKind.TARGET_ROOT
            and re.fullmatch(
                r"(?:object:\d+:\d+|coordinate:\d+:\d+:[0-9a-f]{24})", self.key
            )
            is None
        ):
            raise OwnershipError(
                "target-root scope requires a verified filesystem identity"
            )


@dataclass(frozen=True, slots=True, order=True)
class ResourceId:
    """Canonical durable identity, separate from display/profile metadata."""

    kind: str
    provider: str
    coordinate: str
    scope: ResourceScope

    def __post_init__(self) -> None:
        _require_component(self.kind, field="resource kind")
        _require_component(self.provider, field="resource provider")
        _require_text(self.coordinate, field="resource coordinate")
        if unicodedata.normalize("NFC", self.coordinate) != self.coordinate:
            raise OwnershipError("resource coordinate is not Unicode-canonical")
        if self.kind == "package":
            canonical = _canonical_package_coordinate(self.provider, self.coordinate)
            if canonical != self.coordinate:
                raise OwnershipError("package coordinate is not canonical")
        if self.kind in {"file", "region"}:
            segments = self.coordinate.split("/")
            if (
                self.scope.kind is not ScopeKind.TARGET_ROOT
                or self.coordinate.startswith("/")
                or "\\" in self.coordinate
                or any(segment in {"", ".", ".."} for segment in segments)
            ):
                raise OwnershipError("file coordinate is not canonical and contained")

    def canonical(self) -> str:
        """Return the deterministic wire identity used for hashing and ordering."""
        return json.dumps(
            {
                "coordinate": self.coordinate,
                "kind": self.kind,
                "provider": self.provider,
                "scope": {"key": self.scope.key, "kind": self.scope.kind.value},
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )


@dataclass(frozen=True, slots=True)
class ClaimEvent:
    """One authority/owner transition retained in claim history."""

    action: str
    owner_id: uuid.UUID
    generation: int

    def __post_init__(self) -> None:
        _require_component(self.action, field="claim event action")
        if self.action not in _CLAIM_ACTIONS:
            raise OwnershipError(f"unsupported claim event action: {self.action}")
        if self.generation < 1:
            raise OwnershipError("claim event generation must be positive")


@dataclass(frozen=True, slots=True)
class OwnershipClaim:
    """One current claim plus immutable provenance and transfer history."""

    resource_id: ResourceId
    owner_id: uuid.UUID
    declaration_refs: tuple[str, ...]
    authority: Authority
    lifecycle: ClaimLifecycle
    provenance: tuple[ProvenanceFact, ...]
    locator: str
    fingerprint: str
    generation: int
    history: tuple[ClaimEvent, ...]

    def __post_init__(self) -> None:
        refs = tuple(sorted(set(self.declaration_refs)))
        if not refs or refs != self.declaration_refs:
            raise OwnershipError(
                "declaration refs must be non-empty, unique, and sorted"
            )
        for ref in refs:
            _require_text(ref, field="declaration ref")
        if tuple(sorted(set(self.provenance))) != self.provenance:
            raise OwnershipError("provenance facts must be unique and sorted")
        _require_text(self.locator, field="resource locator")
        _require_text(self.fingerprint, field="resource fingerprint")
        if self.generation < 1:
            raise OwnershipError("claim generation must be positive")
        if (
            self.lifecycle is ClaimLifecycle.RELEASED
            and self.authority is not Authority.NONE
        ):
            raise OwnershipError("a released claim cannot retain management authority")
        if (
            self.lifecycle is ClaimLifecycle.CLAIMED
            and self.authority is not Authority.MANAGE
        ):
            raise OwnershipError("an active claim must retain management authority")
        _validate_claim_history(self)


@dataclass(frozen=True, slots=True)
class LegacyOwnershipEvidence:
    """Read-only legacy evidence that never grants management authority."""

    source: str
    identity_hint: str
    locator: Path
    ambiguous: bool = False
    corrupt: bool = False


def _validate_claim_history(claim: OwnershipClaim) -> None:
    expected_generations = tuple(range(1, claim.generation + 1))
    if tuple(event.generation for event in claim.history) != expected_generations:
        raise OwnershipError(
            "claim history must be non-empty, contiguous, and end at generation"
        )
    if claim.history[0].action != "claim":
        raise OwnershipError("claim history must begin with a claim event")
    _validate_claim_owner_sequence(claim)
    if claim.lifecycle is ClaimLifecycle.RELEASED:
        if claim.history[-1].action != "release":
            raise OwnershipError("released claim history must end with release")
    elif claim.history[-1].action == "release":
        raise OwnershipError("active claim history cannot end with release")


def _validate_claim_owner_sequence(claim: OwnershipClaim) -> None:
    current_owner = claim.history[0].owner_id
    for event in claim.history[1:]:
        if event.action == "claim":
            raise OwnershipError("claim history cannot contain a second claim event")
        if event.action == "transfer":
            current_owner = event.owner_id
        elif event.owner_id != current_owner:
            raise OwnershipError(
                "claim event owner is inconsistent with transfer history"
            )
    if current_owner != claim.owner_id:
        raise OwnershipError("claim history owner does not match current owner")


@dataclass(frozen=True, slots=True)
class _MoveIntent:
    intent_id: uuid.UUID
    source: OwnershipClaim
    destination: OwnershipClaim

    def __post_init__(self) -> None:
        source = self.source
        destination = self.destination
        if source.resource_id == destination.resource_id:
            raise OwnershipError("ownership move must change resource identity")
        expected = replace(
            source,
            resource_id=destination.resource_id,
            generation=source.generation + 1,
            history=(
                *source.history,
                ClaimEvent("move", source.owner_id, source.generation + 1),
            ),
        )
        if destination != expected:
            raise OwnershipError("ownership move intent is not an exact identity move")


class OwnershipStore:
    """Versioned per-resource claims with fail-closed move recovery.

    Mutating methods are named ``*_locked`` because callers must hold the
    user-global resource lock, normally through ``mutation_locks(resources=True)``.
    This lets later CLI flows compose resource, config, target, and profile locks
    once without re-entrant flock acquisition.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = root if root is not None else state_root() / "ownership"

    @property
    def claims_root(self) -> Path:
        return self.root / "claims"

    @property
    def intents_root(self) -> Path:
        return self.root / "intents"

    def read(self, resource_id: ResourceId) -> OwnershipClaim | None:
        """Read one claim, refusing while any identity move is unresolved."""
        self._refuse_unresolved_intents()
        return self._read_path(self._claim_path(resource_id), expected=resource_id)

    def list_claims(self) -> tuple[OwnershipClaim, ...]:
        """Return every validated claim in canonical identity order."""
        self._refuse_unresolved_intents()
        with _open_dir_chain(self.claims_root, create=False) as directory_fd:
            if directory_fd is None:
                return ()
            names = sorted(
                name
                for name in os.listdir(directory_fd)  # noqa: PTH208 - anchored dirfd
                if name.endswith(".json")
            )
            claims = [
                claim
                for name in names
                if (
                    claim := self._read_path(
                        self.claims_root / name, directory_fd=directory_fd
                    )
                )
                is not None
            ]
        identities = [claim.resource_id for claim in claims]
        if len(identities) != len(set(identities)):
            raise CorruptOwnershipState(
                "duplicate resource identities in ownership store"
            )
        return tuple(sorted(claims, key=lambda claim: claim.resource_id.canonical()))

    def claim_locked(
        self,
        *,
        resource_id: ResourceId,
        owner_id: uuid.UUID,
        declaration_refs: Iterable[str],
        provenance: Iterable[ProvenanceFact],
        locator: str,
        fingerprint: str,
        expected_generation: int | None,
    ) -> OwnershipClaim:
        """Create or refresh a claim using an exact generation CAS."""
        require_resources_lock()
        self._refuse_unresolved_intents()
        with _open_dir_chain(self.claims_root, create=True) as claims_fd:
            assert claims_fd is not None
            current = self._read_path(
                self._claim_path(resource_id),
                expected=resource_id,
                directory_fd=claims_fd,
            )
            refs = tuple(sorted(set(declaration_refs)))
            facts = tuple(sorted(set(provenance)))
            if current is None:
                if expected_generation is not None:
                    raise OwnershipError(
                        "ownership claim disappeared; retry from discovery"
                    )
                claim = OwnershipClaim(
                    resource_id=resource_id,
                    owner_id=owner_id,
                    declaration_refs=refs,
                    authority=Authority.MANAGE,
                    lifecycle=ClaimLifecycle.CLAIMED,
                    provenance=facts,
                    locator=locator,
                    fingerprint=fingerprint,
                    generation=1,
                    history=(ClaimEvent("claim", owner_id, 1),),
                )
                self._write_claim(claim, directory_fd=claims_fd)
                return claim
            if current.owner_id != owner_id:
                raise OwnershipCollisionError(
                    "resource is claimed by another config owner: "
                    f"{resource_id.canonical()}"
                )
            _require_generation(current, expected_generation)
            preserved_facts = tuple(sorted(set((*current.provenance, *facts))))
            desired = replace(
                current,
                declaration_refs=refs,
                authority=Authority.MANAGE,
                lifecycle=ClaimLifecycle.CLAIMED,
                provenance=preserved_facts,
                locator=locator,
                fingerprint=fingerprint,
            )
            if desired == current:
                return current
            generation = current.generation + 1
            desired = replace(
                desired,
                generation=generation,
                history=(
                    *current.history,
                    ClaimEvent("refresh", owner_id, generation),
                ),
            )
            self._write_claim(desired, directory_fd=claims_fd)
            return desired

    def transfer_locked(
        self,
        resource_id: ResourceId,
        *,
        expected_owner: uuid.UUID,
        new_owner: uuid.UUID,
        expected_generation: int,
        declaration_refs: Iterable[str],
    ) -> OwnershipClaim:
        """Transfer one exact current claim without changing resource bytes."""
        require_resources_lock()
        with _open_dir_chain(self.claims_root, create=False) as claims_fd:
            if claims_fd is None:
                raise OwnershipError("ownership claim not found; retry from discovery")
            current = self._require_claim(resource_id, directory_fd=claims_fd)
            if current.owner_id != expected_owner:
                raise OwnershipCollisionError(
                    "ownership transfer expected a different owner"
                )
            _require_generation(current, expected_generation)
            generation = current.generation + 1
            transferred = replace(
                current,
                owner_id=new_owner,
                declaration_refs=tuple(sorted(set(declaration_refs))),
                generation=generation,
                history=(
                    *current.history,
                    ClaimEvent("transfer", new_owner, generation),
                ),
            )
            self._write_claim(transferred, directory_fd=claims_fd)
            return transferred

    def release_locked(
        self,
        resource_id: ResourceId,
        *,
        expected_owner: uuid.UUID,
        expected_generation: int,
    ) -> OwnershipClaim:
        """Release management authority while retaining the resource and tombstone."""
        require_resources_lock()
        with _open_dir_chain(self.claims_root, create=False) as claims_fd:
            if claims_fd is None:
                raise OwnershipError("ownership claim not found; retry from discovery")
            current = self._require_claim(resource_id, directory_fd=claims_fd)
            if current.owner_id != expected_owner:
                raise OwnershipCollisionError(
                    "ownership release expected a different owner"
                )
            _require_generation(current, expected_generation)
            if current.lifecycle is ClaimLifecycle.RELEASED:
                return current
            generation = current.generation + 1
            released = replace(
                current,
                authority=Authority.NONE,
                lifecycle=ClaimLifecycle.RELEASED,
                generation=generation,
                history=(
                    *current.history,
                    ClaimEvent("release", expected_owner, generation),
                ),
            )
            self._write_claim(released, directory_fd=claims_fd)
            return released

    def move_locked(
        self,
        source_id: ResourceId,
        destination_id: ResourceId,
        *,
        expected_owner: uuid.UUID,
        expected_generation: int,
    ) -> OwnershipClaim:
        """Move a claim to a new identity through a recoverable durable intent."""
        require_resources_lock()
        self._refuse_unresolved_intents()
        with _open_dir_chain(self.root, create=False) as root_fd:
            if root_fd is None:
                raise OwnershipError("ownership claim not found; retry from discovery")
            with _open_bound_child(
                root_fd, self.root, "claims", create=False
            ) as claims_fd:
                if claims_fd is None:
                    raise OwnershipError(
                        "ownership claim not found; retry from discovery"
                    )
                source = self._require_claim(source_id, directory_fd=claims_fd)
                if source.owner_id != expected_owner:
                    raise OwnershipCollisionError(
                        "ownership move expected a different owner"
                    )
                _require_generation(source, expected_generation)
                if self._read_path(
                    self._claim_path(destination_id),
                    expected=destination_id,
                    directory_fd=claims_fd,
                ):
                    raise OwnershipCollisionError(
                        "ownership move destination is already claimed"
                    )
                generation = source.generation + 1
                destination = replace(
                    source,
                    resource_id=destination_id,
                    generation=generation,
                    history=(
                        *source.history,
                        ClaimEvent("move", expected_owner, generation),
                    ),
                )
                intent = _MoveIntent(uuid.uuid4(), source, destination)
                with _open_bound_child(
                    root_fd, self.root, "intents", create=True
                ) as intents_fd:
                    assert intents_fd is not None
                    self._verify_move_bindings(root_fd, claims_fd, intents_fd)
                    self._write_intent(intent, directory_fd=intents_fd)
                    self._verify_move_bindings(root_fd, claims_fd, intents_fd)
                    self._write_claim(destination, directory_fd=claims_fd)
                    self._verify_move_bindings(root_fd, claims_fd, intents_fd)
                    self._unlink_claim(source_id, directory_fd=claims_fd)
                    self._verify_move_bindings(root_fd, claims_fd, intents_fd)
                    self._unlink_intent(intent.intent_id, directory_fd=intents_fd)
                    self._verify_move_bindings(root_fd, claims_fd, intents_fd)
                    return destination

    def recover_moves_locked(self) -> None:
        """Complete valid interrupted moves; retain ambiguous intents fail-closed."""
        require_resources_lock()
        with _open_dir_chain(self.root, create=False) as root_fd:
            if root_fd is None:
                return
            with (
                _open_bound_child(
                    root_fd, self.root, "claims", create=False
                ) as claims_fd,
                _open_bound_child(
                    root_fd, self.root, "intents", create=False
                ) as intents_fd,
            ):
                if intents_fd is None:
                    return
                if claims_fd is None:
                    raise CorruptOwnershipState(
                        "ownership claims directory disappeared"
                    )
                self._recover_move_names(root_fd, claims_fd, intents_fd)

    def _recover_move_names(
        self, root_fd: int, claims_fd: int, intents_fd: int
    ) -> None:
        names = sorted(
            name for name in os.listdir(intents_fd) if name.endswith(".json")
        )
        for name in names:
            path = self.intents_root / name
            intent = self._read_intent(path, directory_fd=intents_fd)
            source = self._read_path(
                self._claim_path(intent.source.resource_id), directory_fd=claims_fd
            )
            destination = self._read_path(
                self._claim_path(intent.destination.resource_id),
                directory_fd=claims_fd,
            )
            if source == intent.source and destination is None:
                self._verify_move_bindings(root_fd, claims_fd, intents_fd)
                self._write_claim(intent.destination, directory_fd=claims_fd)
                self._verify_move_bindings(root_fd, claims_fd, intents_fd)
                destination = intent.destination
            if source == intent.source and destination == intent.destination:
                self._verify_move_bindings(root_fd, claims_fd, intents_fd)
                self._unlink_claim(intent.source.resource_id, directory_fd=claims_fd)
                self._verify_move_bindings(root_fd, claims_fd, intents_fd)
                source = None
            if source is None and destination == intent.destination:
                self._verify_move_bindings(root_fd, claims_fd, intents_fd)
                self._unlink_intent(intent.intent_id, directory_fd=intents_fd)
                self._verify_move_bindings(root_fd, claims_fd, intents_fd)
                continue
            raise CorruptOwnershipState(
                f"ownership move intent {path} conflicts with live claims"
            )

    def _verify_move_bindings(
        self, root_fd: int, claims_fd: int, intents_fd: int
    ) -> None:
        _verify_directory_binding(self.root, root_fd)
        _verify_bound_child(root_fd, self.root, "claims", claims_fd)
        _verify_bound_child(root_fd, self.root, "intents", intents_fd)

    def _require_claim(
        self, resource_id: ResourceId, *, directory_fd: int | None = None
    ) -> OwnershipClaim:
        self._refuse_unresolved_intents()
        claim = self._read_path(
            self._claim_path(resource_id),
            expected=resource_id,
            directory_fd=directory_fd,
        )
        if claim is None:
            raise OwnershipError("ownership claim not found; retry from discovery")
        return claim

    def _claim_path(self, resource_id: ResourceId) -> Path:
        digest = hashlib.sha256(resource_id.canonical().encode()).hexdigest()
        return self.claims_root / f"{digest}.json"

    def _intent_path(self, intent_id: uuid.UUID) -> Path:
        return self.intents_root / f"{intent_id}.json"

    def _intent_paths(self) -> tuple[Path, ...]:
        with _open_dir_chain(self.intents_root, create=False) as directory_fd:
            if directory_fd is None:
                return ()
            names = sorted(
                name
                for name in os.listdir(directory_fd)  # noqa: PTH208 - anchored dirfd
                if name.endswith(".json")
            )
        return tuple(self.intents_root / name for name in names)

    def _refuse_unresolved_intents(self) -> None:
        paths = self._intent_paths()
        if paths:
            raise OwnershipError(
                f"unfinished ownership move {paths[0].name}; "
                "run recovery before retrying"
            )

    def _write_claim(
        self, claim: OwnershipClaim, *, directory_fd: int | None = None
    ) -> None:
        if directory_fd is not None:
            _atomic_write_at(
                directory_fd,
                self._claim_path(claim.resource_id).name,
                (json.dumps(_claim_to_json(claim), sort_keys=True) + "\n").encode(),
            )
            return
        with _open_dir_chain(self.claims_root, create=True) as directory_fd:
            assert directory_fd is not None
            _atomic_write_at(
                directory_fd,
                self._claim_path(claim.resource_id).name,
                (json.dumps(_claim_to_json(claim), sort_keys=True) + "\n").encode(),
            )

    def _read_path(
        self,
        path: Path,
        *,
        expected: ResourceId | None = None,
        directory_fd: int | None = None,
    ) -> OwnershipClaim | None:
        if path.parent != self.claims_root or path.name != Path(path.name).name:
            raise CorruptOwnershipState(f"invalid ownership claim path: {path}")
        try:
            if directory_fd is None:
                with _open_dir_chain(self.claims_root, create=False) as opened_fd:
                    if opened_fd is None:
                        return None
                    payload = _read_regular_at(opened_fd, path.name)
            else:
                payload = _read_regular_at(directory_fd, path.name)
            if payload is None:
                return None
            raw = json.loads(payload.decode("utf-8", errors="strict"))
        except FileNotFoundError:
            return None
        except CorruptOwnershipState:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CorruptOwnershipState(f"cannot read ownership claim {path}") from exc
        try:
            claim = _claim_from_json(_require_mapping(raw, "ownership claim"))
        except (KeyError, TypeError, ValueError, OwnershipError) as exc:
            raise CorruptOwnershipState(f"invalid ownership claim {path}") from exc
        canonical_path = self._claim_path(claim.resource_id)
        if path != canonical_path or (
            expected is not None and claim.resource_id != expected
        ):
            raise CorruptOwnershipState(
                f"ownership claim identity/path mismatch: {path}"
            )
        return claim

    def _write_intent(
        self, intent: _MoveIntent, *, directory_fd: int | None = None
    ) -> None:
        payload = (
            json.dumps(
                {
                    "destination": _claim_to_json(intent.destination),
                    "intent_id": str(intent.intent_id),
                    "schema_version": _SCHEMA,
                    "source": _claim_to_json(intent.source),
                },
                sort_keys=True,
            )
            + "\n"
        ).encode()
        if directory_fd is not None:
            _atomic_write_at(
                directory_fd, self._intent_path(intent.intent_id).name, payload
            )
            return
        with _open_dir_chain(self.intents_root, create=True) as directory_fd:
            assert directory_fd is not None
            _atomic_write_at(
                directory_fd,
                self._intent_path(intent.intent_id).name,
                payload,
            )

    def _read_intent(
        self, path: Path, *, directory_fd: int | None = None
    ) -> _MoveIntent:
        try:
            if path.parent != self.intents_root or path.name != Path(path.name).name:
                raise ValueError("invalid ownership intent path")
            if directory_fd is None:
                with _open_dir_chain(self.intents_root, create=False) as opened_fd:
                    if opened_fd is None:
                        raise FileNotFoundError(path)
                    payload = _read_regular_at(opened_fd, path.name)
            else:
                payload = _read_regular_at(directory_fd, path.name)
            if payload is None:
                raise FileNotFoundError(path)
            raw = _require_mapping(
                json.loads(payload.decode("utf-8", errors="strict")), "move intent"
            )
            _require_exact_keys(
                raw,
                {"destination", "intent_id", "schema_version", "source"},
                "move intent",
            )
            if raw["schema_version"] != _SCHEMA:
                raise ValueError("unsupported ownership intent schema")
            intent_id = uuid.UUID(_require_string(raw, "intent_id"))
            if path != self._intent_path(intent_id):
                raise ValueError("ownership intent identity/path mismatch")
            return _MoveIntent(
                intent_id,
                _claim_from_json(_require_mapping(raw["source"], "source claim")),
                _claim_from_json(
                    _require_mapping(raw["destination"], "destination claim")
                ),
            )
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
            OwnershipError,
        ) as exc:
            raise CorruptOwnershipState(
                f"invalid ownership move intent {path}"
            ) from exc

    def _unlink_claim(
        self, resource_id: ResourceId, *, directory_fd: int | None = None
    ) -> None:
        if directory_fd is not None:
            os.unlink(self._claim_path(resource_id).name, dir_fd=directory_fd)
            os.fsync(directory_fd)
            return
        with _open_dir_chain(self.claims_root, create=False) as directory_fd:
            if directory_fd is None:
                raise CorruptOwnershipState("ownership claims directory disappeared")
            os.unlink(self._claim_path(resource_id).name, dir_fd=directory_fd)
            os.fsync(directory_fd)

    def _unlink_intent(
        self, intent_id: uuid.UUID, *, directory_fd: int | None = None
    ) -> None:
        if directory_fd is not None:
            os.unlink(self._intent_path(intent_id).name, dir_fd=directory_fd)
            os.fsync(directory_fd)
            return
        with _open_dir_chain(self.intents_root, create=False) as directory_fd:
            if directory_fd is None:
                raise CorruptOwnershipState("ownership intents directory disappeared")
            os.unlink(self._intent_path(intent_id).name, dir_fd=directory_fd)
            os.fsync(directory_fd)


def scan_legacy_receipts(root: Path) -> tuple[LegacyOwnershipEvidence, ...]:
    """Read old receipt identities as unverified evidence, never as claims."""
    try:
        root_info = root.lstat()
    except FileNotFoundError:
        return ()
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
        return (LegacyOwnershipEvidence("receipt", root.name, root, corrupt=True),)
    evidence: list[LegacyOwnershipEvidence] = []
    seen: set[str] = set()
    for path in sorted(root.glob("*.json")):
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise OSError("legacy receipt is not a regular file")
            raw = _require_mapping(
                json.loads(path.read_text(encoding="utf-8")), "receipt"
            )
            key = _require_string(raw, "key")
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ):
            evidence.append(
                LegacyOwnershipEvidence("receipt", path.name, path, corrupt=True)
            )
            continue
        duplicate = key in seen
        seen.add(key)
        evidence.append(
            LegacyOwnershipEvidence("receipt", key, path, ambiguous=duplicate)
        )
    duplicate_keys = {
        item.identity_hint
        for item in evidence
        if sum(other.identity_hint == item.identity_hint for other in evidence) > 1
    }
    return tuple(
        replace(item, ambiguous=True) if item.identity_hint in duplicate_keys else item
        for item in evidence
    )


def scan_legacy_reconcile(root: Path) -> tuple[LegacyOwnershipEvidence, ...]:
    """Inventory legacy reconcile artifacts without inferring ownership."""
    try:
        root_info = root.lstat()
    except FileNotFoundError:
        return ()
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
        return (LegacyOwnershipEvidence("reconcile", root.name, root, corrupt=True),)
    evidence: list[LegacyOwnershipEvidence] = []
    for leg in ("base", "local", "index", "drafts"):
        leg_root = root / leg
        try:
            leg_info = leg_root.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(leg_info.st_mode) or stat.S_ISLNK(leg_info.st_mode):
            evidence.append(
                LegacyOwnershipEvidence(f"reconcile-{leg}", leg, leg_root, corrupt=True)
            )
            continue
        for path in sorted(leg_root.rglob("*")):
            try:
                info = path.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode):
                evidence.append(
                    LegacyOwnershipEvidence(
                        f"reconcile-{leg}",
                        path.relative_to(leg_root).as_posix(),
                        path,
                        corrupt=True,
                    )
                )
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            relative = path.relative_to(leg_root).as_posix()
            evidence.append(LegacyOwnershipEvidence(f"reconcile-{leg}", relative, path))
    return tuple(evidence)


def load_or_create_owner_id(config_dir: Path) -> uuid.UUID:
    """Return the stable UUID for one Git config checkout.

    The ID lives in the Git common directory, so linked worktrees agree while a
    normal clone receives a new ID. Non-Git repositories fail closed.
    """
    with _locked_common_dir(config_dir) as common_fd:
        owner_dir_fd = _open_child_dir_at(common_fd, _OWNER_ID_RELATIVE.parent.name)
        try:
            return _create_or_read_owner_id_at(
                owner_dir_fd, _OWNER_ID_RELATIVE.name, uuid.uuid4()
            )
        finally:
            os.close(owner_dir_fd)


def read_owner_id(config_dir: Path) -> uuid.UUID:
    """Read an existing checkout UUID without creating one."""
    with _locked_common_dir(config_dir) as common_fd:
        owner_dir_fd = _open_child_dir_at(
            common_fd, _OWNER_ID_RELATIVE.parent.name, create=False
        )
        try:
            return _parse_owner_id_at(owner_dir_fd, _OWNER_ID_RELATIVE.name)
        finally:
            os.close(owner_dir_fd)


@contextmanager
def _locked_common_dir(config_dir: Path) -> Iterator[int]:
    common_dir = _git_common_dir(config_dir)
    with config_identity_lock(common_dir) as common_fd:
        _require_common_dir_binding(config_dir, common_fd)
        yield common_fd
        _require_common_dir_binding(config_dir, common_fd)


def _require_common_dir_binding(config_dir: Path, common_fd: int) -> None:
    rebound = _git_common_dir(config_dir)
    rebound_info = rebound.stat()
    held_info = os.fstat(common_fd)
    if (rebound_info.st_dev, rebound_info.st_ino) != (
        held_info.st_dev,
        held_info.st_ino,
    ):
        raise OwnershipError("Git common directory changed while holding UUID lock")


def _git_common_dir(config_dir: Path) -> Path:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(config_dir),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OwnershipError("cannot resolve config checkout identity") from exc
    if result.returncode != 0:
        raise OwnershipError("ownership requires a Git-backed config checkout")
    try:
        rendered = result.stdout.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise OwnershipError("Git returned an invalid common-directory path") from exc
    if not rendered:
        raise OwnershipError("Git returned an empty common-directory path")
    common_dir = Path(rendered).resolve(strict=True)
    if not common_dir.is_dir():
        raise OwnershipError("Git common directory is not a directory")
    return common_dir


def _parse_owner_id_at(directory_fd: int, name: str) -> uuid.UUID:
    try:
        payload = _read_regular_at(directory_fd, name)
        if payload is None:
            raise FileNotFoundError(name)
        raw = payload.decode("ascii", errors="strict")
        if raw != raw.strip() + "\n":
            raise ValueError("owner UUID is not canonical")
        owner_id = uuid.UUID(raw.strip())
        if str(owner_id) != raw.strip():
            raise ValueError("owner UUID is not canonical")
        return owner_id
    except (FileNotFoundError, OSError, UnicodeDecodeError, ValueError) as exc:
        raise CorruptOwnershipState(f"invalid config owner identity {name}") from exc


def _create_or_read_owner_id_at(
    directory_fd: int, name: str, proposed: uuid.UUID
) -> uuid.UUID:
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            _FILE_MODE,
            dir_fd=directory_fd,
        )
    except FileExistsError:
        return _parse_owner_id_at(directory_fd, name)
    try:
        payload = f"{proposed}\n".encode("ascii")
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    os.fsync(directory_fd)
    return proposed


def _open_child_dir_at(parent_fd: int, name: str, *, create: bool = True) -> int:
    try:
        return os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
    except FileNotFoundError:
        if not create:
            raise CorruptOwnershipState(
                f"config owner identity directory is missing: {name}"
            ) from None
        with suppress(FileExistsError):
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
        try:
            return os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
        except OSError as exc:
            raise CorruptOwnershipState(
                f"config owner identity directory is not trusted: {name}"
            ) from exc
    except OSError as exc:
        raise CorruptOwnershipState(
            f"config owner identity directory is not trusted: {name}"
        ) from exc


@contextmanager
def _open_dir_chain(path: Path, *, create: bool) -> Iterator[int | None]:
    """Open an absolute directory chain without following symlinks."""
    descriptor = _open_dir_chain_fd(path, create=create)
    if descriptor is None:
        yield None
        return
    info = os.fstat(descriptor)
    identity = info.st_dev, info.st_ino
    try:
        yield descriptor
        _verify_directory_binding(path, descriptor, expected=identity)
    finally:
        os.close(descriptor)


def _verify_directory_binding(
    path: Path, descriptor: int, *, expected: tuple[int, int] | None = None
) -> None:
    held = os.fstat(descriptor)
    identity = expected or (held.st_dev, held.st_ino)
    try:
        live = path.lstat()
    except OSError as exc:
        raise CorruptOwnershipState(
            f"ownership state directory binding changed: {path}"
        ) from exc
    if not stat.S_ISDIR(live.st_mode) or (live.st_dev, live.st_ino) != identity:
        raise CorruptOwnershipState(
            f"ownership state directory binding changed: {path}"
        )


def _open_dir_chain_fd(path: Path, *, create: bool) -> int | None:
    absolute = path.absolute()
    parts = absolute.parts
    descriptor = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in parts[1:]:
            try:
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                if not create:
                    os.close(descriptor)
                    return None
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
                os.fsync(descriptor)
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=descriptor,
                )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError as exc:
        os.close(descriptor)
        raise CorruptOwnershipState(
            f"ownership state directory is not trusted: {path}"
        ) from exc


@contextmanager
def _open_bound_child(
    parent_fd: int, parent_path: Path, name: str, *, create: bool
) -> Iterator[int | None]:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
    except FileNotFoundError:
        if not create:
            yield None
            return
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            os.fsync(parent_fd)
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
        except OSError as exc:
            raise CorruptOwnershipState(
                f"ownership state directory is not trusted: {parent_path / name}"
            ) from exc
    except OSError as exc:
        raise CorruptOwnershipState(
            f"ownership state directory is not trusted: {parent_path / name}"
        ) from exc
    try:
        _verify_bound_child(parent_fd, parent_path, name, descriptor)
        yield descriptor
        _verify_bound_child(parent_fd, parent_path, name, descriptor)
    finally:
        os.close(descriptor)


def _verify_bound_child(
    parent_fd: int, parent_path: Path, name: str, child_fd: int
) -> None:
    held = os.fstat(child_fd)
    try:
        live = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise CorruptOwnershipState(
            f"ownership state directory binding changed: {parent_path / name}"
        ) from exc
    if not stat.S_ISDIR(live.st_mode) or (live.st_dev, live.st_ino) != (
        held.st_dev,
        held.st_ino,
    ):
        raise CorruptOwnershipState(
            f"ownership state directory binding changed: {parent_path / name}"
        )


def _read_regular_at(directory_fd: int, name: str) -> bytes | None:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=directory_fd,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CorruptOwnershipState(
            f"ownership state leaf is not trusted: {name}"
        ) from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise CorruptOwnershipState(
                f"ownership state leaf is not a regular file: {name}"
            )
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 64 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _atomic_write_at(directory_fd: int, name: str, payload: bytes) -> None:
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        _FILE_MODE,
        dir_fd=directory_fd,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            temporary,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        os.close(descriptor)
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory_fd)


def _claim_to_json(claim: OwnershipClaim) -> dict[str, object]:
    return {
        "authority": claim.authority.value,
        "declaration_refs": list(claim.declaration_refs),
        "fingerprint": claim.fingerprint,
        "generation": claim.generation,
        "history": [
            {
                "action": event.action,
                "generation": event.generation,
                "owner_id": str(event.owner_id),
            }
            for event in claim.history
        ],
        "lifecycle": claim.lifecycle.value,
        "locator": claim.locator,
        "owner_id": str(claim.owner_id),
        "provenance": [
            {"kind": fact.kind.value, "value": fact.value} for fact in claim.provenance
        ],
        "resource_id": json.loads(claim.resource_id.canonical()),
        "schema_version": _SCHEMA,
    }


def _claim_from_json(raw: Mapping[str, object]) -> OwnershipClaim:
    _require_exact_keys(
        raw,
        {
            "authority",
            "declaration_refs",
            "fingerprint",
            "generation",
            "history",
            "lifecycle",
            "locator",
            "owner_id",
            "provenance",
            "resource_id",
            "schema_version",
        },
        "ownership claim",
    )
    if raw["schema_version"] != _SCHEMA:
        raise ValueError("unsupported ownership schema")
    resource = _require_mapping(raw["resource_id"], "resource id")
    scope = _require_mapping(resource["scope"], "resource scope")
    _require_exact_keys(
        resource, {"coordinate", "kind", "provider", "scope"}, "resource id"
    )
    _require_exact_keys(scope, {"key", "kind"}, "resource scope")
    resource_id = ResourceId(
        kind=_require_string(resource, "kind"),
        provider=_require_string(resource, "provider"),
        coordinate=_require_string(resource, "coordinate"),
        scope=_resource_scope_from_json(scope),
    )
    history_raw = _require_list(raw, "history")
    provenance_raw = _require_list(raw, "provenance")
    refs_raw = _require_list(raw, "declaration_refs")
    return OwnershipClaim(
        resource_id=resource_id,
        owner_id=uuid.UUID(_require_string(raw, "owner_id")),
        declaration_refs=tuple(_require_list_strings(refs_raw, "declaration_refs")),
        authority=Authority(_require_string(raw, "authority")),
        lifecycle=ClaimLifecycle(_require_string(raw, "lifecycle")),
        provenance=tuple(_provenance_from_json(value) for value in provenance_raw),
        locator=_require_string(raw, "locator"),
        fingerprint=_require_string(raw, "fingerprint"),
        generation=_require_int(raw, "generation"),
        history=tuple(_claim_event_from_json(value) for value in history_raw),
    )


def _resource_scope_from_json(raw: Mapping[str, object]) -> ResourceScope:
    kind = ScopeKind(_require_string(raw, "kind"))
    key = _require_string(raw, "key")
    if kind is ScopeKind.TARGET_ROOT:
        return ResourceScope._from_wire(kind, key)
    return ResourceScope(kind, key)


def _provenance_from_json(value: object) -> ProvenanceFact:
    raw = _require_mapping(value, "provenance fact")
    _require_exact_keys(raw, {"kind", "value"}, "provenance fact")
    return ProvenanceFact(
        ProvenanceFactKind(_require_string(raw, "kind")),
        _require_string(raw, "value"),
    )


def _claim_event_from_json(value: object) -> ClaimEvent:
    raw = _require_mapping(value, "claim event")
    _require_exact_keys(raw, {"action", "generation", "owner_id"}, "claim event")
    return ClaimEvent(
        _require_string(raw, "action"),
        uuid.UUID(_require_string(raw, "owner_id")),
        _require_int(raw, "generation"),
    )


def _require_generation(claim: OwnershipClaim, expected: int | None) -> None:
    if expected is None or claim.generation != expected:
        raise OwnershipError(
            f"stale ownership generation: expected {expected}, found {claim.generation}"
        )


def _canonical_package_coordinate(provider: str, coordinate: str) -> str:
    if provider == "cargo":
        return coordinate.casefold()
    if provider == "python":
        return re.sub(r"[-_.]+", "-", coordinate.casefold())
    if provider == "github_release":
        parts = coordinate.split("/")
        if len(parts) != 2 or any(part in {"", ".", ".."} for part in parts):
            raise OwnershipError("GitHub release coordinate must be owner/repository")
        return coordinate.casefold()
    if provider == "go":
        if (
            coordinate.startswith("/")
            or "\\" in coordinate
            or posixpath.normpath(coordinate) != coordinate
            or any(part in {"", ".", ".."} for part in coordinate.split("/"))
        ):
            raise OwnershipError("Go module coordinate is not canonical")
        return coordinate
    if provider == "extension":
        return coordinate.casefold()
    if provider in {"local", "plugin"}:
        if provider == "local" and (
            "/" in coordinate or "\\" in coordinate or coordinate in {".", ".."}
        ):
            raise OwnershipError("local package coordinate must be a bare filename")
        return coordinate
    raise OwnershipError(f"unsupported package identity provider: {provider}")


def _require_component(value: str, *, field: str) -> None:
    _require_text(value, field=field)
    if any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789-_."
        for character in value
    ):
        raise OwnershipError(f"{field} is not canonical: {value!r}")


def _require_text(value: str, *, field: str) -> None:
    if (
        not value
        or value != value.strip()
        or any(ord(character) < 32 for character in value)
    ):
        raise OwnershipError(f"{field} must be non-empty canonical text")


def _require_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise TypeError(f"{label} must be an object")
    return value


def _require_exact_keys(
    raw: Mapping[str, object], expected: set[str], label: str
) -> None:
    if set(raw) != expected:
        raise ValueError(f"{label} fields do not match schema")


def _require_string(raw: Mapping[str, object], key: str) -> str:
    value = raw[key]
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    return value


def _require_int(raw: Mapping[str, object], key: str) -> int:
    value = raw[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{key} must be an integer")
    return value


def _require_list(raw: Mapping[str, object], key: str) -> list[object]:
    value = raw[key]
    if not isinstance(value, list):
        raise TypeError(f"{key} must be an array")
    return value


def _require_list_strings(values: list[object], label: str) -> list[str]:
    if not all(isinstance(value, str) for value in values):
        raise TypeError(f"{label} must contain strings")
    return [value for value in values if isinstance(value, str)]
