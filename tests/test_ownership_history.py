"""Owner-scoped ownership release history and recovery contracts."""

from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from setforge.errors import CorruptOwnershipState, OwnershipError
from setforge.locking import install_resources_lock
from setforge.ownership import (
    Authority,
    ClaimLifecycle,
    OwnershipClaim,
    OwnershipStore,
    ProvenanceFact,
    ProvenanceFactKind,
    ResourceId,
)
from setforge.ownership_history import OwnershipHistoryStore


def _claim(store: OwnershipStore, owner_id: uuid.UUID) -> OwnershipClaim:
    with install_resources_lock():
        return store.claim_locked(
            resource_id=ResourceId.package("cargo", "ripgrep"),
            owner_id=owner_id,
            declaration_refs=("packages.cargo.ripgrep",),
            provenance=(
                ProvenanceFact(ProvenanceFactKind.ORIGIN, "provider-inventory"),
            ),
            locator="~/.cargo/bin/rg",
            fingerprint="observed-ripgrep",
            expected_generation=None,
        )


def test_claim_ids_are_full_lowercase_hashes_and_resolve_exactly(
    tmp_path: Path,
) -> None:
    ledger = OwnershipStore(tmp_path / "ledger")
    owner_id = uuid.uuid4()
    claim = _claim(ledger, owner_id)

    claim_id = ledger.claim_id(claim.resource_id)

    assert len(claim_id) == 64
    assert claim_id == claim_id.lower()
    assert ledger.read_claim_id(claim_id) == claim
    for invalid in (claim_id[:-1], claim_id.upper(), "g" * 64, f"../{claim_id}"):
        with pytest.raises(OwnershipError, match="claim ID"):
            ledger.read_claim_id(invalid)


def test_release_is_owner_scoped_preserves_metadata_and_records_transition(
    tmp_path: Path,
) -> None:
    ledger = OwnershipStore(tmp_path / "ledger")
    history = OwnershipHistoryStore(tmp_path / "history")
    owner_id = uuid.uuid4()
    foreign_owner = uuid.uuid4()
    claimed = _claim(ledger, owner_id)
    claim_id = ledger.claim_id(claimed.resource_id)

    with install_resources_lock():
        with pytest.raises(OwnershipError, match="current config owner"):
            history.release_locked(ledger, foreign_owner, claim_id)
        transition = history.release_locked(ledger, owner_id, claim_id)

    released = ledger.read_claim_id(claim_id)
    assert released is not None
    assert transition.before == claimed
    assert transition.after == released
    assert released.lifecycle is ClaimLifecycle.RELEASED
    assert released.authority is Authority.NONE
    assert released.locator == claimed.locator
    assert released.fingerprint == claimed.fingerprint
    assert released.provenance == claimed.provenance
    assert released.declaration_refs == claimed.declaration_refs
    restarted_history = OwnershipHistoryStore(tmp_path / "history")
    assert restarted_history.list(owner_id) == (transition,)
    assert restarted_history.read(owner_id, str(transition.transition_id)) == transition
    assert history.list(foreign_owner) == ()
    with pytest.raises(OwnershipError, match="not found"):
        history.read(foreign_owner, str(transition.transition_id))


def test_revert_requires_exact_post_state_and_authority_validation(
    tmp_path: Path,
) -> None:
    ledger = OwnershipStore(tmp_path / "ledger")
    history = OwnershipHistoryStore(tmp_path / "history")
    owner_id = uuid.uuid4()
    claimed = _claim(ledger, owner_id)
    with install_resources_lock():
        released = history.release_locked(
            ledger, owner_id, ledger.claim_id(claimed.resource_id)
        )

    validated: list[OwnershipClaim] = []
    with install_resources_lock():
        reverted = history.revert_locked(
            ledger,
            owner_id,
            str(released.transition_id),
            validate_authority=validated.append,
        )

    restored = ledger.read(claimed.resource_id)
    assert restored is not None
    assert validated == [released.after, released.after]
    assert restored.lifecycle is ClaimLifecycle.CLAIMED
    assert restored.authority is Authority.MANAGE
    assert reverted.before == released.after
    assert reverted.after == restored
    assert reverted.reverts_transition_id == released.transition_id

    with install_resources_lock():
        with pytest.raises(OwnershipError, match="no longer current"):
            history.revert_locked(
                ledger,
                owner_id,
                str(released.transition_id),
                validate_authority=lambda _claim: None,
            )

        reverse_revert = history.revert_locked(
            ledger,
            owner_id,
            str(reverted.transition_id),
            validate_authority=lambda _claim: pytest.fail(
                "authority-reducing reversal must not inspect live resources"
            ),
        )

    assert reverse_revert.after.lifecycle is ClaimLifecycle.RELEASED


def test_authority_grant_is_revalidated_after_pending_publication(
    tmp_path: Path,
) -> None:
    ledger = OwnershipStore(tmp_path / "ledger")
    history = OwnershipHistoryStore(tmp_path / "history")
    owner_id = uuid.uuid4()
    claimed = _claim(ledger, owner_id)
    with install_resources_lock():
        released = history.release_locked(
            ledger, owner_id, ledger.claim_id(claimed.resource_id)
        )

    calls = 0

    def _racing_validator(_claim: OwnershipClaim) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OwnershipError("live authority inputs raced")

    with (
        install_resources_lock(),
        pytest.raises(OwnershipError, match="authority inputs raced"),
    ):
        history.revert_locked(
            ledger,
            owner_id,
            str(released.transition_id),
            validate_authority=_racing_validator,
        )

    assert calls == 2
    assert ledger.read(claimed.resource_id) == released.after
    assert history.list(owner_id) == (released,)
    pending = history.pending(owner_id)
    assert len(pending) == 1
    assert pending[0].before == released.after
    assert pending[0].after.lifecycle is ClaimLifecycle.CLAIMED


def test_interrupted_release_is_visible_and_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = OwnershipStore(tmp_path / "ledger")
    history = OwnershipHistoryStore(tmp_path / "history")
    owner_id = uuid.uuid4()
    claimed = _claim(ledger, owner_id)
    original = OwnershipHistoryStore._commit_transition

    def _crash(self: OwnershipHistoryStore, transition: object) -> None:
        raise RuntimeError("injected crash after tombstone")

    monkeypatch.setattr(OwnershipHistoryStore, "_commit_transition", _crash)
    with install_resources_lock(), pytest.raises(RuntimeError, match="injected"):
        history.release_locked(ledger, owner_id, ledger.claim_id(claimed.resource_id))

    pending = history.pending(owner_id)
    assert len(pending) == 1
    assert ledger.read(claimed.resource_id) == pending[0].after
    with install_resources_lock(), pytest.raises(OwnershipError, match="unfinished"):
        history.release_locked(ledger, owner_id, ledger.claim_id(claimed.resource_id))

    monkeypatch.setattr(OwnershipHistoryStore, "_commit_transition", original)
    with install_resources_lock():
        recovered = history.recover_locked(
            ledger, owner_id, validate_authority=lambda _claim: None
        )
    assert recovered == pending
    assert history.pending(owner_id) == ()
    assert history.list(owner_id) == pending


def test_release_interrupted_before_claim_mutation_recovers_from_before_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = OwnershipStore(tmp_path / "ledger")
    history = OwnershipHistoryStore(tmp_path / "history")
    owner_id = uuid.uuid4()
    claimed = _claim(ledger, owner_id)
    original = OwnershipStore.release_locked

    def _crash_before_mutation(
        self: OwnershipStore,
        resource_id: ResourceId,
        *,
        expected_owner: uuid.UUID,
        expected_generation: int,
    ) -> OwnershipClaim:
        raise RuntimeError("injected crash before claim mutation")

    monkeypatch.setattr(OwnershipStore, "release_locked", _crash_before_mutation)
    with install_resources_lock(), pytest.raises(RuntimeError, match="before claim"):
        history.release_locked(ledger, owner_id, ledger.claim_id(claimed.resource_id))

    pending = history.pending(owner_id)
    assert len(pending) == 1
    assert ledger.read(claimed.resource_id) == claimed

    monkeypatch.setattr(OwnershipStore, "release_locked", original)
    restarted_history = OwnershipHistoryStore(tmp_path / "history")
    restarted_ledger = OwnershipStore(tmp_path / "ledger")
    with install_resources_lock():
        recovered = restarted_history.recover_locked(
            restarted_ledger,
            owner_id,
            validate_authority=lambda _claim: pytest.fail(
                "release recovery must not validate live authority"
            ),
        )

    assert recovered == pending
    assert restarted_history.pending(owner_id) == ()
    assert restarted_history.list(owner_id) == pending
    assert restarted_ledger.read(claimed.resource_id) == pending[0].after


def test_recovery_fails_closed_when_claim_conflicts_with_pending_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = OwnershipStore(tmp_path / "ledger")
    history = OwnershipHistoryStore(tmp_path / "history")
    owner_id = uuid.uuid4()
    claimed = _claim(ledger, owner_id)

    monkeypatch.setattr(
        OwnershipHistoryStore,
        "_commit_transition",
        lambda _self, _transition: (_ for _ in ()).throw(RuntimeError("crash")),
    )
    with install_resources_lock(), pytest.raises(RuntimeError, match="crash"):
        history.release_locked(ledger, owner_id, ledger.claim_id(claimed.resource_id))

    with install_resources_lock():
        current = ledger.read(claimed.resource_id)
        assert current is not None
        ledger.restore_locked(current)

    with (
        install_resources_lock(),
        pytest.raises(
            CorruptOwnershipState, match="conflicts with the ownership claim"
        ),
    ):
        history.recover_locked(ledger, owner_id, validate_authority=lambda _claim: None)


def test_multiple_pending_transitions_are_ambiguous_and_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = OwnershipStore(tmp_path / "ledger")
    history = OwnershipHistoryStore(tmp_path / "history")
    owner_id = uuid.uuid4()
    claimed = _claim(ledger, owner_id)
    monkeypatch.setattr(
        OwnershipHistoryStore,
        "_commit_transition",
        lambda _self, _transition: (_ for _ in ()).throw(RuntimeError("crash")),
    )
    with install_resources_lock(), pytest.raises(RuntimeError, match="crash"):
        history.release_locked(ledger, owner_id, ledger.claim_id(claimed.resource_id))
    first = history.pending(owner_id)[0]
    history._write_pending(replace(first, transition_id=uuid.uuid4()))

    with pytest.raises(CorruptOwnershipState, match="multiple pending"):
        history.pending(owner_id)


def test_history_rejects_corrupt_and_cross_owner_state(tmp_path: Path) -> None:
    ledger = OwnershipStore(tmp_path / "ledger")
    history = OwnershipHistoryStore(tmp_path / "history")
    owner_id = uuid.uuid4()
    claimed = _claim(ledger, owner_id)
    with install_resources_lock():
        transition = history.release_locked(
            ledger, owner_id, ledger.claim_id(claimed.resource_id)
        )

    record_path = (
        history.root
        / str(owner_id)
        / "transitions"
        / f"{transition.transition_id}.json"
    )
    record_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(CorruptOwnershipState, match="transition"):
        history.list(owner_id)


def test_history_ignores_interrupted_atomic_write_temporary_files(
    tmp_path: Path,
) -> None:
    ledger = OwnershipStore(tmp_path / "ledger")
    history = OwnershipHistoryStore(tmp_path / "history")
    owner_id = uuid.uuid4()
    claimed = _claim(ledger, owner_id)
    with install_resources_lock():
        transition = history.release_locked(
            ledger, owner_id, ledger.claim_id(claimed.resource_id)
        )

    records = history.root / str(owner_id) / "transitions"
    temporary = records / f".{transition.transition_id}.json.{'d' * 32}.tmp"
    temporary.write_text("partial", encoding="utf-8")

    restarted = OwnershipHistoryStore(tmp_path / "history")
    assert restarted.list(owner_id) == (transition,)

    temporary.unlink()
    temporary.symlink_to(records / "missing-temp-target")
    with pytest.raises(CorruptOwnershipState, match="temporary"):
        restarted.list(owner_id)
    temporary.unlink()

    (records / "unexpected.tmp").write_text("unknown", encoding="utf-8")
    with pytest.raises(CorruptOwnershipState, match="filename"):
        restarted.list(owner_id)
