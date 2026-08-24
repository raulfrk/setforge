from __future__ import annotations

import json
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from setforge import operations, transitions
from setforge.cli import revert as revert_mod
from setforge.ownership import (
    Authority,
    ClaimEvent,
    ClaimLifecycle,
    OwnershipClaim,
    OwnershipStore,
    ResourceId,
    ownership_claim_to_json,
)


def _transfer_pair() -> tuple[OwnershipClaim, OwnershipClaim]:
    old_owner = uuid.uuid4()
    new_owner = uuid.uuid4()
    before = OwnershipClaim(
        resource_id=ResourceId.package("cargo", "ripgrep"),
        owner_id=old_owner,
        declaration_refs=("packages.cargo.ripgrep",),
        authority=Authority.MANAGE,
        lifecycle=ClaimLifecycle.CLAIMED,
        provenance=(),
        locator="ripgrep",
        fingerprint="f" * 64,
        generation=1,
        history=(ClaimEvent("claim", old_owner, 1),),
    )
    after = replace(
        before,
        owner_id=new_owner,
        generation=2,
        history=(*before.history, ClaimEvent("transfer", new_owner, 2)),
    )
    return before, after


def test_ownership_transfer_sidecar_round_trips_and_old_records_load_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    before, after = _transfer_pair()
    delta = transitions.OwnershipTransferDelta(before=before, after=after)

    current = transitions.write_transition(
        transitions.make_meta(transitions.TransitionCommand.INSTALL, "p"),
        {},
        {},
        None,
        ownership_transfers=(delta,),
    )
    old = transitions.write_transition(
        transitions.make_meta(transitions.TransitionCommand.INSTALL, "old"),
        {},
        {},
        None,
    )

    assert transitions.load_ownership_transfers(current) == (delta,)
    assert transitions.load_ownership_transfers(old) == ()


@pytest.mark.parametrize(
    "payload",
    [
        "not json\n",
        json.dumps({"schema_version": 2, "entries": []}),
        json.dumps({"schema_version": 1, "entries": {}}),
    ],
)
def test_ownership_transfer_sidecar_rejects_malformed_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: str
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    transition = transitions.write_transition(
        transitions.make_meta(transitions.TransitionCommand.INSTALL, "p"),
        {},
        {},
        None,
    )
    (transition / "ownership_transfers.json").write_text(payload, encoding="utf-8")

    with pytest.raises(transitions.InvalidTransitionRecord):
        transitions.load_ownership_transfers(transition)
    with pytest.raises(transitions.InvalidTransitionRecord):
        transitions.list_transitions()


def test_ownership_transfer_sidecar_rejects_duplicate_resources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    before, after = _transfer_pair()
    transition = transitions.write_transition(
        transitions.make_meta(transitions.TransitionCommand.INSTALL, "p"),
        {},
        {},
        None,
        ownership_transfers=(transitions.OwnershipTransferDelta(before, after),),
    )
    sidecar = transition / "ownership_transfers.json"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["entries"].append(payload["entries"][0])
    sidecar.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(transitions.InvalidTransitionRecord):
        transitions.load_ownership_transfers(transition)


def test_ownership_transfer_delta_requires_exact_forward_successor() -> None:
    before, after = _transfer_pair()

    invalid = (
        replace(after, fingerprint="changed"),
        replace(after, locator="other"),
        replace(after, resource_id=ResourceId.package("cargo", "other")),
        replace(
            after,
            generation=3,
            history=(*after.history, ClaimEvent("refresh", after.owner_id, 3)),
        ),
    )
    for candidate in invalid:
        with pytest.raises(ValueError, match="exact claim successor"):
            transitions.OwnershipTransferDelta(before, candidate)


def test_revert_journal_guards_claim_and_filesystem_delta_ancestry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    before, after = _transfer_pair()
    store = OwnershipStore()
    claim_path = store.claim_path(after.resource_id)
    claim_path.parent.mkdir(parents=True)
    claim_path.write_text(json.dumps(ownership_claim_to_json(after)), encoding="utf-8")
    live = tmp_path / "live" / "item"
    live.parent.mkdir()
    filesystem_delta = transitions.FilesystemDelta(
        live,
        transitions.FilesystemImage(transitions.FilesystemKind.ABSENT),
        transitions.FilesystemImage(transitions.FilesystemKind.ABSENT),
    )
    transition = transitions.write_transition(
        transitions.make_meta(transitions.TransitionCommand.INSTALL, "p"),
        {},
        {},
        None,
        filesystem_deltas=(filesystem_delta,),
        ownership_transfers=(transitions.OwnershipTransferDelta(before, after),),
    )
    config = tmp_path / "repo" / "setforge.yaml"
    config.parent.mkdir()
    config.write_text(
        "schema_version: '6.0'\ntracked_files: {}\nprofiles: {p: {}}\n",
        encoding="utf-8",
    )

    journal = revert_mod._prepare_revert_journal((transition,), "p", config)
    try:
        guarded = {guard.path for guard in journal.path_guards}
        assert claim_path.parent in guarded
        assert live.parent in guarded
    finally:
        operations.complete(journal)
