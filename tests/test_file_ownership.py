from __future__ import annotations

import os
import stat
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from setforge.errors import OwnershipError
from setforge.file_ownership import (
    FileAction,
    decide_file,
    file_resource_id,
    observe_file,
    publish_file_claim_locked,
)
from setforge.ownership import (
    Authority,
    ClaimEvent,
    ClaimLifecycle,
    OwnershipClaim,
    OwnershipStore,
    ProvenanceFact,
    ProvenanceFactKind,
)


def _claim(
    path: Path,
    *,
    owner: uuid.UUID,
    fingerprint: str,
    authority: Authority = Authority.MANAGE,
    lifecycle: ClaimLifecycle = ClaimLifecycle.CLAIMED,
) -> OwnershipClaim:
    return OwnershipClaim(
        resource_id=file_resource_id(path),
        owner_id=owner,
        declaration_refs=("tracked_files.shell",),
        authority=authority,
        lifecycle=lifecycle,
        provenance=(
            ProvenanceFact(ProvenanceFactKind.ACQUISITION, "adopted-external"),
        ),
        locator=str(path),
        fingerprint=fingerprint,
        generation=1,
        history=(
            ClaimEvent(
                "claim" if lifecycle is ClaimLifecycle.CLAIMED else "release",
                owner,
                1,
            ),
        ),
    )


def test_file_identity_unifies_parent_directory_aliases(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)

    assert file_resource_id(root / "settings.ini") == file_resource_id(
        alias / "settings.ini"
    )


def test_file_identity_refuses_root_destination(tmp_path: Path) -> None:
    with pytest.raises(OwnershipError, match="file destination"):
        file_resource_id(Path("/"))

    with pytest.raises(OwnershipError, match="below a target root"):
        file_resource_id(Path("/settings.ini"))


def test_observation_binds_bytes_mode_and_symlink_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"value\n")
    target.chmod(0o640)
    link = tmp_path / "logical"
    link.symlink_to("target")

    first = observe_file(link)
    assert first.present is True
    assert first.symlink_target == "target"
    assert first.mode == stat.S_IMODE(target.stat().st_mode)

    target.write_bytes(b"changed\n")
    assert observe_file(link).fingerprint != first.fingerprint
    target.write_bytes(b"value\n")
    target.chmod(0o600)
    assert observe_file(link).fingerprint != first.fingerprint
    link.unlink()
    link.symlink_to(str(target))
    assert observe_file(link).fingerprint != first.fingerprint


def test_observation_refuses_dangling_symlink(tmp_path: Path) -> None:
    link = tmp_path / "logical"
    link.symlink_to("missing")

    with pytest.raises(OwnershipError, match="dangling symlink"):
        observe_file(link)


def test_observation_refuses_non_file(tmp_path: Path) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()

    with pytest.raises(OwnershipError, match="not a regular file"):
        observe_file(directory)


def test_present_unowned_file_requires_metadata_adoption(tmp_path: Path) -> None:
    path = tmp_path / "settings.ini"
    path.write_text("local\n")
    owner = uuid.uuid4()

    decision = decide_file(observe_file(path), None, owner_id=owner)

    assert decision.action is FileAction.ADOPT
    assert "unowned" in decision.detail


def test_current_claim_allows_managed_reconcile(tmp_path: Path) -> None:
    path = tmp_path / "settings.ini"
    path.write_text("local\n")
    owner = uuid.uuid4()
    observed = observe_file(path)

    decision = decide_file(
        observed,
        _claim(path, owner=owner, fingerprint=observed.fingerprint),
        owner_id=owner,
    )

    assert decision.action is FileAction.MANAGE


def test_decision_refuses_claim_for_another_resource(tmp_path: Path) -> None:
    path = tmp_path / "settings.ini"
    other = tmp_path / "other.ini"
    path.write_text("local\n")
    owner = uuid.uuid4()
    observed = observe_file(path)
    claim = _claim(path, owner=owner, fingerprint=observed.fingerprint)

    with pytest.raises(OwnershipError, match="identity does not match"):
        decide_file(
            observed,
            replace(claim, resource_id=file_resource_id(other)),
            owner_id=owner,
        )


@pytest.mark.parametrize("foreign", [False, True])
def test_released_or_foreign_claim_holds_file(tmp_path: Path, foreign: bool) -> None:
    path = tmp_path / "settings.ini"
    path.write_text("local\n")
    owner = uuid.uuid4()
    observed = observe_file(path)
    claim_owner = uuid.uuid4() if foreign else owner
    claim = _claim(path, owner=claim_owner, fingerprint=observed.fingerprint)
    if not foreign:
        claim = OwnershipClaim(
            resource_id=claim.resource_id,
            owner_id=claim.owner_id,
            declaration_refs=claim.declaration_refs,
            authority=Authority.NONE,
            lifecycle=ClaimLifecycle.RELEASED,
            provenance=claim.provenance,
            locator=claim.locator,
            fingerprint=claim.fingerprint,
            generation=2,
            history=(
                claim.history[0],
                ClaimEvent("release", claim.owner_id, 2),
            ),
        )

    assert decide_file(observed, claim, owner_id=owner).action is FileAction.HOLD


def test_drift_is_reviewable_but_protected_removal_holds(tmp_path: Path) -> None:
    path = tmp_path / "settings.ini"
    path.write_text("before\n")
    owner = uuid.uuid4()
    original = observe_file(path)
    claim = _claim(path, owner=owner, fingerprint=original.fingerprint)
    path.write_text("after\n")

    drift = decide_file(observe_file(path), claim, owner_id=owner)
    assert drift.action is FileAction.REVIEW

    path.unlink()
    removal = decide_file(
        observe_file(path), claim, owner_id=owner, protected_units=True
    )
    assert removal.action is FileAction.HOLD
    assert "protected" in removal.detail

    recreate = decide_file(observe_file(path), claim, owner_id=owner)
    assert recreate.action is FileAction.INSTALL


def test_absent_observation_cannot_be_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    owner = uuid.uuid4()
    decision = decide_file(observe_file(tmp_path / "missing"), None, owner_id=owner)

    with pytest.raises(OwnershipError, match="cannot adopt an absent"):
        publish_file_claim_locked(
            OwnershipStore(),
            decision,
            owner_id=owner,
            declaration_ref="tracked_files.missing",
            acquisition="setforge-installed",
        )


def test_present_observation_publishes_exact_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge.locking import install_resources_lock

    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / "settings.ini"
    path.write_text("local\n")
    owner = uuid.uuid4()
    decision = decide_file(observe_file(path), None, owner_id=owner)
    store = OwnershipStore()

    with install_resources_lock():
        claim = publish_file_claim_locked(
            store,
            decision,
            owner_id=owner,
            declaration_ref="tracked_files.settings",
            acquisition="adopted-external",
        )

    assert store.read(decision.observation.resource_id) == claim
    assert claim.fingerprint == decision.observation.fingerprint


def test_observation_of_regular_file_does_not_change_it(tmp_path: Path) -> None:
    path = tmp_path / "settings.ini"
    path.write_bytes(b"value\n")
    before = (path.read_bytes(), os.lstat(path).st_mode)

    observe_file(path)

    assert (path.read_bytes(), os.lstat(path).st_mode) == before
