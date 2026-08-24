"""Owner-scoped, crash-recoverable ownership authority transitions."""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from setforge.errors import CorruptOwnershipState, OwnershipError
from setforge.locking import require_resources_lock
from setforge.ownership import (
    Authority,
    ClaimEvent,
    ClaimLifecycle,
    OwnershipClaim,
    OwnershipStore,
    _atomic_write_at,
    _open_bound_child,
    _open_dir_chain,
    _read_regular_at,
    ownership_claim_from_json,
    ownership_claim_to_json,
)
from setforge.transitions import state_root

__all__ = [
    "OwnershipHistoryStore",
    "OwnershipTransition",
    "OwnershipTransitionAction",
]

_SCHEMA = "1.0"
_UUID_TEXT = (
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_UUID_FILE_RE = re.compile(rf"{_UUID_TEXT}\.json")
_ATOMIC_TEMP_FILE_RE = re.compile(rf"\.{_UUID_TEXT}\.json\.[0-9a-f]{{32}}\.tmp")


class OwnershipTransitionAction(StrEnum):
    """Public authority-transition actions."""

    RELEASE = "release"
    REVERT = "revert"


@dataclass(frozen=True, slots=True)
class OwnershipTransition:
    """One immutable owner-scoped before/after authority transition."""

    transition_id: uuid.UUID
    owner_id: uuid.UUID
    action: OwnershipTransitionAction
    created_at: datetime
    before: OwnershipClaim
    after: OwnershipClaim
    reverts_transition_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if self.created_at.tzinfo is None:
            raise OwnershipError(
                "ownership transition timestamp must be timezone-aware"
            )
        if (
            self.before.owner_id != self.owner_id
            or self.after.owner_id != self.owner_id
        ):
            raise OwnershipError("ownership transition crosses config owners")
        if self.before.resource_id != self.after.resource_id:
            raise OwnershipError("ownership transition changes resource identity")
        if self.action is OwnershipTransitionAction.RELEASE:
            if self.reverts_transition_id is not None:
                raise OwnershipError(
                    "release transition cannot name a reverted transition"
                )
            expected = _release_successor(self.before)
        else:
            if self.reverts_transition_id is None:
                raise OwnershipError(
                    "revert transition must name its source transition"
                )
            if self.reverts_transition_id == self.transition_id:
                raise OwnershipError("ownership transition cannot revert itself")
            expected = _toggle_successor(self.before)
        if self.after != expected:
            raise OwnershipError(
                "ownership transition is not an exact authority toggle"
            )


AuthorityValidator = Callable[[OwnershipClaim], None]


class OwnershipHistoryStore:
    """Immutable owner histories plus durable pending publication records."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root if root is not None else state_root() / "ownership-history"

    def list(self, owner_id: uuid.UUID) -> tuple[OwnershipTransition, ...]:
        """List one owner's committed transitions in chronological order."""
        records = self._read_all(owner_id, "transitions")
        return tuple(
            sorted(records, key=lambda item: (item.created_at, str(item.transition_id)))
        )

    def pending(self, owner_id: uuid.UUID) -> tuple[OwnershipTransition, ...]:
        """List durable transitions that still require recovery."""
        records = self._read_all(owner_id, "pending")
        if len(records) > 1:
            raise CorruptOwnershipState(
                "multiple pending ownership transitions are ambiguous"
            )
        return records

    def read(self, owner_id: uuid.UUID, transition_id: str) -> OwnershipTransition:
        """Read one exact full transition ID from one owner's namespace."""
        parsed = _parse_transition_id(transition_id)
        with self._open_records(owner_id, "transitions", create=False) as records_fd:
            if records_fd is None:
                raise OwnershipError("ownership transition not found")
            transition = self._read_record_at(
                records_fd, owner_id, parsed, kind="committed"
            )
        if transition is None:
            raise OwnershipError("ownership transition not found")
        return transition

    def release_locked(
        self,
        ledger: OwnershipStore,
        owner_id: uuid.UUID,
        claim_id: str,
    ) -> OwnershipTransition:
        """Release one exact claim and publish its owner-scoped history record."""
        require_resources_lock()
        self._refuse_pending(owner_id)
        current = ledger.read_claim_id(claim_id)
        if current is None:
            raise OwnershipError("ownership claim not found")
        if current.owner_id != owner_id:
            raise OwnershipError(
                "ownership claim is not held by the current config owner"
            )
        if current.lifecycle is not ClaimLifecycle.CLAIMED:
            raise OwnershipError("ownership claim is already released")
        transition = OwnershipTransition(
            uuid.uuid4(),
            owner_id,
            OwnershipTransitionAction.RELEASE,
            datetime.now(UTC),
            current,
            _release_successor(current),
        )
        self._write_pending(transition)
        released = ledger.release_locked(
            current.resource_id,
            expected_owner=owner_id,
            expected_generation=current.generation,
        )
        if released != transition.after:
            raise CorruptOwnershipState(
                "released ownership claim does not match its pending transition"
            )
        self._commit_transition(transition)
        self._unlink_pending(transition)
        return transition

    def revert_locked(
        self,
        ledger: OwnershipStore,
        owner_id: uuid.UUID,
        transition_id: str,
        *,
        validate_authority: AuthorityValidator,
    ) -> OwnershipTransition:
        """Reverse one exact current transition and record the new reversible toggle."""
        require_resources_lock()
        self._refuse_pending(owner_id)
        source = self.read(owner_id, transition_id)
        current = ledger.read(source.after.resource_id)
        if current != source.after:
            raise OwnershipError(
                "ownership transition is no longer current and cannot be reverted"
            )
        if current.lifecycle is ClaimLifecycle.RELEASED:
            validate_authority(current)
        transition = OwnershipTransition(
            uuid.uuid4(),
            owner_id,
            OwnershipTransitionAction.REVERT,
            datetime.now(UTC),
            current,
            _toggle_successor(current),
            source.transition_id,
        )
        self._write_pending(transition)
        if current.lifecycle is ClaimLifecycle.RELEASED:
            validate_authority(current)
        changed = self._apply_toggle(ledger, transition.before)
        if changed != transition.after:
            raise CorruptOwnershipState(
                "reverted ownership claim does not match its pending transition"
            )
        self._commit_transition(transition)
        self._unlink_pending(transition)
        return transition

    def recover_locked(
        self,
        ledger: OwnershipStore,
        owner_id: uuid.UUID,
        *,
        validate_authority: AuthorityValidator,
    ) -> tuple[OwnershipTransition, ...]:
        """Complete every unambiguous owner-scoped pending transition."""
        require_resources_lock()
        recovered: list[OwnershipTransition] = []
        for transition in self.pending(owner_id):
            current = ledger.read(transition.before.resource_id)
            if current == transition.before:
                if transition.after.lifecycle is ClaimLifecycle.CLAIMED:
                    validate_authority(current)
                current = self._apply_toggle(ledger, transition.before)
            if current != transition.after:
                raise CorruptOwnershipState(
                    f"pending ownership transition {transition.transition_id} "
                    "conflicts with the ownership claim"
                )
            self._commit_transition(transition)
            self._unlink_pending(transition)
            recovered.append(transition)
        return tuple(recovered)

    def _apply_toggle(
        self, ledger: OwnershipStore, before: OwnershipClaim
    ) -> OwnershipClaim:
        if before.lifecycle is ClaimLifecycle.RELEASED:
            return ledger.restore_locked(before)
        return ledger.release_locked(
            before.resource_id,
            expected_owner=before.owner_id,
            expected_generation=before.generation,
        )

    def _refuse_pending(self, owner_id: uuid.UUID) -> None:
        pending = self.pending(owner_id)
        if pending:
            raise OwnershipError(
                f"unfinished ownership transition {pending[0].transition_id}; "
                "run ownership recover before retrying"
            )

    def _write_pending(self, transition: OwnershipTransition) -> None:
        self._write_record(transition, "pending")

    def _commit_transition(self, transition: OwnershipTransition) -> None:
        self._write_record(transition, "transitions")

    def _write_record(self, transition: OwnershipTransition, kind: str) -> None:
        with self._open_records(transition.owner_id, kind, create=True) as records_fd:
            assert records_fd is not None
            existing = self._read_record_at(
                records_fd,
                transition.owner_id,
                transition.transition_id,
                kind=kind,
            )
            if existing is not None:
                if existing != transition:
                    raise CorruptOwnershipState(
                        f"ownership {kind} transition changed after publication"
                    )
                return
            _atomic_write_at(
                records_fd,
                f"{transition.transition_id}.json",
                (
                    json.dumps(_transition_to_json(transition), sort_keys=True) + "\n"
                ).encode(),
            )

    def _unlink_pending(self, transition: OwnershipTransition) -> None:
        with self._open_records(
            transition.owner_id, "pending", create=False
        ) as records_fd:
            if records_fd is None:
                raise CorruptOwnershipState("ownership pending directory disappeared")
            try:
                os.unlink(f"{transition.transition_id}.json", dir_fd=records_fd)
            except FileNotFoundError as exc:
                raise CorruptOwnershipState(
                    "ownership pending transition disappeared"
                ) from exc
            os.fsync(records_fd)

    def _read_all(
        self, owner_id: uuid.UUID, kind: str
    ) -> tuple[OwnershipTransition, ...]:
        with self._open_records(owner_id, kind, create=False) as records_fd:
            if records_fd is None:
                return ()
            all_names = sorted(
                os.listdir(records_fd)  # noqa: PTH208 - anchored directory fd
            )
            if any(
                _UUID_FILE_RE.fullmatch(name) is None
                and _ATOMIC_TEMP_FILE_RE.fullmatch(name) is None
                for name in all_names
            ):
                raise CorruptOwnershipState(
                    f"invalid ownership {kind} transition filename"
                )
            for name in all_names:
                if _ATOMIC_TEMP_FILE_RE.fullmatch(name) is None:
                    continue
                try:
                    info = os.stat(name, dir_fd=records_fd, follow_symlinks=False)
                except OSError as exc:
                    raise CorruptOwnershipState(
                        f"invalid ownership {kind} temporary transition"
                    ) from exc
                if not stat.S_ISREG(info.st_mode):
                    raise CorruptOwnershipState(
                        f"invalid ownership {kind} temporary transition"
                    )
            names = [
                name for name in all_names if _UUID_FILE_RE.fullmatch(name) is not None
            ]
            records = []
            for name in names:
                transition_id = uuid.UUID(name.removesuffix(".json"))
                record = self._read_record_at(
                    records_fd, owner_id, transition_id, kind=kind
                )
                if record is None:
                    raise CorruptOwnershipState(
                        f"ownership {kind} transition disappeared"
                    )
                records.append(record)
            return tuple(records)

    def _read_record_at(
        self,
        records_fd: int,
        owner_id: uuid.UUID,
        transition_id: uuid.UUID,
        *,
        kind: str,
    ) -> OwnershipTransition | None:
        name = f"{transition_id}.json"
        try:
            payload = _read_regular_at(records_fd, name)
            if payload is None:
                return None
            raw = json.loads(payload.decode("utf-8", errors="strict"))
            transition = _transition_from_json(_mapping(raw, "transition"))
        except CorruptOwnershipState:
            raise
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
            TypeError,
        ) as exc:
            raise CorruptOwnershipState(
                f"invalid ownership {kind} transition {name}"
            ) from exc
        if transition.owner_id != owner_id or transition.transition_id != transition_id:
            raise CorruptOwnershipState(
                f"ownership {kind} transition identity/path mismatch"
            )
        return transition

    @contextmanager
    def _open_records(
        self, owner_id: uuid.UUID, kind: str, *, create: bool
    ) -> Iterator[int | None]:
        if kind not in {"pending", "transitions"}:
            raise ValueError("invalid ownership history directory")
        owner_name = str(owner_id)
        with _open_dir_chain(self.root, create=create) as root_fd:
            if root_fd is None:
                yield None
                return
            with _open_bound_child(
                root_fd, self.root, owner_name, create=create
            ) as owner_fd:
                if owner_fd is None:
                    yield None
                    return
                owner_root = self.root / owner_name
                with _open_bound_child(
                    owner_fd, owner_root, kind, create=create
                ) as records_fd:
                    yield records_fd


def _release_successor(claim: OwnershipClaim) -> OwnershipClaim:
    if claim.lifecycle is not ClaimLifecycle.CLAIMED:
        raise OwnershipError("only an active ownership claim can be released")
    generation = claim.generation + 1
    return replace(
        claim,
        authority=Authority.NONE,
        lifecycle=ClaimLifecycle.RELEASED,
        generation=generation,
        history=(*claim.history, ClaimEvent("release", claim.owner_id, generation)),
    )


def _toggle_successor(claim: OwnershipClaim) -> OwnershipClaim:
    if claim.lifecycle is ClaimLifecycle.CLAIMED:
        return _release_successor(claim)
    generation = claim.generation + 1
    return replace(
        claim,
        authority=Authority.MANAGE,
        lifecycle=ClaimLifecycle.CLAIMED,
        generation=generation,
        history=(*claim.history, ClaimEvent("revert", claim.owner_id, generation)),
    )


def _transition_to_json(transition: OwnershipTransition) -> dict[str, object]:
    return {
        "action": transition.action.value,
        "after": ownership_claim_to_json(transition.after),
        "before": ownership_claim_to_json(transition.before),
        "created_at": transition.created_at.astimezone(UTC).isoformat(),
        "owner_id": str(transition.owner_id),
        "reverts_transition_id": (
            None
            if transition.reverts_transition_id is None
            else str(transition.reverts_transition_id)
        ),
        "schema_version": _SCHEMA,
        "transition_id": str(transition.transition_id),
    }


def _transition_from_json(raw: Mapping[str, object]) -> OwnershipTransition:
    expected = {
        "action",
        "after",
        "before",
        "created_at",
        "owner_id",
        "reverts_transition_id",
        "schema_version",
        "transition_id",
    }
    if set(raw) != expected or raw.get("schema_version") != _SCHEMA:
        raise ValueError("unsupported ownership transition schema")
    reverted = raw["reverts_transition_id"]
    if reverted is not None and not isinstance(reverted, str):
        raise TypeError("reverts_transition_id must be a string or null")
    return OwnershipTransition(
        transition_id=uuid.UUID(_string(raw, "transition_id")),
        owner_id=uuid.UUID(_string(raw, "owner_id")),
        action=OwnershipTransitionAction(_string(raw, "action")),
        created_at=datetime.fromisoformat(_string(raw, "created_at")),
        before=ownership_claim_from_json(_mapping(raw["before"], "before claim")),
        after=ownership_claim_from_json(_mapping(raw["after"], "after claim")),
        reverts_transition_id=None if reverted is None else uuid.UUID(reverted),
    )


def _parse_transition_id(value: str) -> uuid.UUID:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as exc:
        raise OwnershipError(
            "ownership transition ID must be a full canonical UUID"
        ) from exc
    if str(parsed) != value:
        raise OwnershipError("ownership transition ID must be a full canonical UUID")
    return parsed


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise TypeError(f"{label} must be an object")
    return value


def _string(raw: Mapping[str, object], key: str) -> str:
    value = raw[key]
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    return value
