from __future__ import annotations

import uuid

from setforge.locking import mutation_locks
from setforge.ownership import OwnershipStore, ProvenanceFactKind
from setforge.provision.ownership import (
    PackageAction,
    decide_package,
    observation_fingerprint,
    observation_provenance,
    package_resource_id,
    publish_claim_locked,
)
from setforge.provision.protocol import (
    Identity,
    ObservationOrigin,
    PackageObservation,
    ProvisionItem,
)


def _item(*, version: str | None = "2") -> ProvisionItem:
    return ProvisionItem(
        type="cargo",
        identity=Identity("ripgrep", "ripgrep"),
        version=version,
    )


def _observation(*, version: str = "1") -> PackageObservation:
    return PackageObservation(
        Identity("ripgrep", "ripgrep"),
        ObservationOrigin.EXTERNAL,
        version=version,
        source="crates.io",
    )


def test_present_without_claim_is_metadata_only_adoption() -> None:
    decision = decide_package(_item(), _observation(), None, owner_id=uuid.uuid4())
    assert decision.action is PackageAction.ADOPT
    assert decision.detail == "present, external, unowned"


def test_absent_package_installs() -> None:
    decision = decide_package(_item(), None, None, owner_id=uuid.uuid4())
    assert decision.action is PackageAction.INSTALL


def test_adopt_then_second_run_allows_upgrade(tmp_path) -> None:
    owner = uuid.uuid4()
    store = OwnershipStore(tmp_path / "ownership")
    observed = _observation()
    decision = decide_package(_item(), observed, None, owner_id=owner)
    with mutation_locks(resources=True):
        claim = publish_claim_locked(
            store,
            decision,
            owner_id=owner,
            declaration_ref="packages.ripgrep",
            acquisition="adopted-external",
        )
    repeated = decide_package(_item(), observed, claim, owner_id=owner)
    assert repeated.action is PackageAction.UPGRADE
    assert claim.fingerprint == observation_fingerprint(observed)


def test_matching_claim_is_noop_and_foreign_or_drifted_claim_holds(tmp_path) -> None:
    owner = uuid.uuid4()
    observed = _observation(version="2")
    store = OwnershipStore(tmp_path / "ownership")
    initial = decide_package(_item(), observed, None, owner_id=owner)
    with mutation_locks(resources=True):
        claim = publish_claim_locked(
            store,
            initial,
            owner_id=owner,
            declaration_ref="packages.ripgrep",
            acquisition="adopted-external",
        )
    assert (
        decide_package(_item(), observed, claim, owner_id=owner).action
        is PackageAction.NONE
    )
    assert (
        decide_package(_item(), observed, claim, owner_id=uuid.uuid4()).action
        is PackageAction.HOLD
    )
    changed = _observation(version="3")
    assert (
        decide_package(_item(), changed, claim, owner_id=owner).action
        is PackageAction.HOLD
    )
    assert (
        decide_package(_item(), None, claim, owner_id=uuid.uuid4()).action
        is PackageAction.HOLD
    )
    assert (
        decide_package(_item(), None, claim, owner_id=owner).action
        is PackageAction.INSTALL
    )


def test_provider_is_part_of_durable_identity() -> None:
    cargo = package_resource_id(_item())
    python = package_resource_id(
        ProvisionItem(type="python", identity=Identity("ripgrep", "ripgrep"))
    )
    assert cargo != python
    assert cargo.canonical() != python.canonical()


def test_provider_coordinate_uses_existing_canonical_rules() -> None:
    cargo = package_resource_id(
        ProvisionItem(type="cargo", identity=Identity("RipGrep", "RipGrep"))
    )
    python = package_resource_id(
        ProvisionItem(type="python", identity=Identity("My_Tool", "My_Tool"))
    )
    assert cargo.coordinate == "ripgrep"
    assert python.coordinate == "my-tool"


def test_platform_artifact_is_part_of_desired_state_and_provenance() -> None:
    item = ProvisionItem(
        type="github_release",
        identity=Identity("owner/tool", "owner/tool"),
        version="v1",
        checksum="sha256:linux",
        artifact="tool-linux.tar.gz",
        platform="linux-x86_64",
    )
    observed = PackageObservation(
        item.identity,
        ObservationOrigin.CURRENT_RECEIPT,
        version="v1",
        checksum="sha256:linux",
        artifact="tool-macos.tar.gz",
        platform="macos-aarch64",
    )

    facts = observation_provenance(observed, acquisition="setforge-installed")

    assert (
        decide_package(item, observed, None, owner_id=uuid.uuid4()).action
        is PackageAction.ADOPT
    )
    assert {(fact.kind, fact.value) for fact in facts} >= {
        (ProvenanceFactKind.ARTIFACT, "tool-macos.tar.gz"),
        (ProvenanceFactKind.PLATFORM, "macos-aarch64"),
    }
