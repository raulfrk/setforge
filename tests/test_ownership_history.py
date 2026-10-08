"""Owner-scoped ownership release history and recovery contracts."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from setforge import operations
from setforge.errors import CorruptOwnershipState, OwnershipError
from setforge.locking import mutation_locks
from setforge.ownership import (
    Authority,
    ClaimLifecycle,
    OwnershipClaim,
    OwnershipStore,
    ProvenanceFact,
    ProvenanceFactKind,
    ResourceId,
)
from setforge.ownership_history import (
    OwnershipHistoryStore,
    ownership_operation_profile,
)
from tests.shared_helpers import legacy_crash_log


def _claim(store: OwnershipStore, owner_id: uuid.UUID) -> OwnershipClaim:
    with mutation_locks(resources=True):
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

    with mutation_locks(resources=True):
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
    with mutation_locks(resources=True):
        released = history.release_locked(
            ledger, owner_id, ledger.claim_id(claimed.resource_id)
        )

    validated: list[OwnershipClaim] = []
    with mutation_locks(resources=True):
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

    with mutation_locks(resources=True):
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


def test_authority_grant_is_revalidated_after_the_journal_is_published(
    tmp_path: Path,
) -> None:
    ledger = OwnershipStore(tmp_path / "ledger")
    history = OwnershipHistoryStore(tmp_path / "history")
    owner_id = uuid.uuid4()
    claimed = _claim(ledger, owner_id)
    with mutation_locks(resources=True):
        released = history.release_locked(
            ledger, owner_id, ledger.claim_id(claimed.resource_id)
        )

    calls = 0

    def _racing_validator(_claim: OwnershipClaim) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            assert operations.active(ownership_operation_profile(owner_id))
            raise OwnershipError("live authority inputs raced")

    with (
        mutation_locks(resources=True),
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
    assert history.pending(owner_id) == ()
    assert operations.active(ownership_operation_profile(owner_id)) is None


def test_failed_release_is_undone_and_can_be_repeated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = OwnershipStore(tmp_path / "ledger")
    history = OwnershipHistoryStore(tmp_path / "history")
    owner_id = uuid.uuid4()
    claimed = _claim(ledger, owner_id)
    claim_bytes = ledger.claim_path(claimed.resource_id).read_bytes()
    original = OwnershipHistoryStore._commit_transition

    def _fail(self: OwnershipHistoryStore, transition: object) -> None:
        raise RuntimeError("injected failure after tombstone")

    monkeypatch.setattr(OwnershipHistoryStore, "_commit_transition", _fail)
    with mutation_locks(resources=True), pytest.raises(RuntimeError, match="injected"):
        history.release_locked(ledger, owner_id, ledger.claim_id(claimed.resource_id))

    assert ledger.claim_path(claimed.resource_id).read_bytes() == claim_bytes
    assert history.list(owner_id) == ()
    assert history.pending(owner_id) == ()
    assert not (history.root / str(owner_id) / "pending").exists()
    assert operations.active(ownership_operation_profile(owner_id)) is None

    monkeypatch.setattr(OwnershipHistoryStore, "_commit_transition", original)
    with mutation_locks(resources=True):
        repeated = history.release_locked(
            ledger, owner_id, ledger.claim_id(claimed.resource_id)
        )
    assert history.list(owner_id) == (repeated,)


@pytest.mark.parametrize("claim_mutated", [True, False])
def test_legacy_crash_log_refuses_new_transitions_until_it_is_completed(
    tmp_path: Path, claim_mutated: bool
) -> None:
    ledger = OwnershipStore(tmp_path / "ledger")
    history = OwnershipHistoryStore(tmp_path / "history")
    owner_id = uuid.uuid4()
    claimed = _claim(ledger, owner_id)
    claim_path = ledger.claim_path(claimed.resource_id)
    claim_before = (claim_path, claim_path.read_bytes())
    with mutation_locks(resources=True):
        logged = history.release_locked(
            ledger, owner_id, ledger.claim_id(claimed.resource_id)
        )
    log_path = legacy_crash_log(
        history, logged, None if claim_mutated else claim_before
    )

    assert history.pending(owner_id) == (logged,)
    assert history.list(owner_id) == ()
    with mutation_locks(resources=True):
        with pytest.raises(OwnershipError, match="ownership recover --apply"):
            history.release_locked(
                ledger, owner_id, ledger.claim_id(claimed.resource_id)
            )
        with pytest.raises(OwnershipError, match="ownership recover --apply"):
            history.revert_locked(
                ledger,
                owner_id,
                str(logged.transition_id),
                validate_authority=lambda _claim: None,
            )
    assert log_path.is_file()
    assert operations.active(ownership_operation_profile(owner_id)) is None

    restarted_history = OwnershipHistoryStore(tmp_path / "history")
    restarted_ledger = OwnershipStore(tmp_path / "ledger")
    with mutation_locks(resources=True):
        recovered = restarted_history.recover_locked(
            restarted_ledger,
            owner_id,
            validate_authority=lambda _claim: pytest.fail(
                "release recovery must not validate live authority"
            ),
        )

    assert recovered == (logged,)
    assert restarted_history.pending(owner_id) == ()
    assert restarted_history.list(owner_id) == (logged,)
    assert restarted_ledger.read(claimed.resource_id) == logged.after
    assert not log_path.exists()


def test_legacy_recovery_fails_closed_when_claim_conflicts_with_the_crash_log(
    tmp_path: Path,
) -> None:
    ledger = OwnershipStore(tmp_path / "ledger")
    history = OwnershipHistoryStore(tmp_path / "history")
    owner_id = uuid.uuid4()
    claimed = _claim(ledger, owner_id)
    with mutation_locks(resources=True):
        legacy_crash_log(
            history,
            history.release_locked(
                ledger, owner_id, ledger.claim_id(claimed.resource_id)
            ),
        )
        current = ledger.read(claimed.resource_id)
        assert current is not None
        ledger.restore_locked(current)

    with (
        mutation_locks(resources=True),
        pytest.raises(
            CorruptOwnershipState, match="conflicts with the ownership claim"
        ),
    ):
        history.recover_locked(ledger, owner_id, validate_authority=lambda _claim: None)
    assert len(history.pending(owner_id)) == 1


def test_multiple_legacy_crash_logs_are_ambiguous_and_fail_closed(
    tmp_path: Path,
) -> None:
    ledger = OwnershipStore(tmp_path / "ledger")
    history = OwnershipHistoryStore(tmp_path / "history")
    owner_id = uuid.uuid4()
    claimed = _claim(ledger, owner_id)
    with mutation_locks(resources=True):
        first = history.release_locked(
            ledger, owner_id, ledger.claim_id(claimed.resource_id)
        )
    log_path = legacy_crash_log(history, first)
    second = uuid.uuid4()
    (log_path.parent / f"{second}.json").write_text(
        log_path.read_text(encoding="utf-8").replace(
            str(first.transition_id), str(second)
        ),
        encoding="utf-8",
    )

    with pytest.raises(CorruptOwnershipState, match="multiple pending"):
        history.pending(owner_id)
    with (
        mutation_locks(resources=True),
        pytest.raises(CorruptOwnershipState, match="multiple pending"),
    ):
        history.release_locked(ledger, owner_id, ledger.claim_id(claimed.resource_id))


def test_history_rejects_corrupt_and_cross_owner_state(tmp_path: Path) -> None:
    ledger = OwnershipStore(tmp_path / "ledger")
    history = OwnershipHistoryStore(tmp_path / "history")
    owner_id = uuid.uuid4()
    claimed = _claim(ledger, owner_id)
    with mutation_locks(resources=True):
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
    with mutation_locks(resources=True):
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
