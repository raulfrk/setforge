"""Real files and receipts across shared-package dependency recovery."""

import io
import uuid
import zipfile
from pathlib import Path

import pytest

from setforge.config import (
    BundleComponent,
    BundleSpec,
    Config,
    LocalPackage,
    Package,
    ResolvedProfile,
)
from setforge.locking import mutation_locks
from setforge.ownership import Authority, OwnershipStore, ResourceId
from setforge.provision.dispatch import (
    apply_provisioning,
    has_hard_failure,
    plan_provisioning,
    publish_installed_package_claims_locked,
)
from setforge.provision.local import LocalProvisioner
from setforge.provision.protocol import Outcome
from setforge.provision.receipt import ReceiptStore


def test_shared_package_waits_for_dependency_then_settles_with_receipts_and_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracked = tmp_path / "tracked"
    tracked.mkdir()
    (tracked / "before.zip").write_bytes(b"invalid archive")
    (tracked / "shared").write_bytes(b"shared executable")
    (tracked / "independent").write_bytes(b"independent executable")
    destination = tmp_path / "bin"
    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    monkeypatch.setattr(
        LocalProvisioner, "_resolve_tracked_root", lambda _self: tracked
    )
    packages: dict[str, Package] = {
        name: LocalPackage(
            path="before.zip" if name == "before" else name,
            binary=name,
            install=str(destination),
            extract=name == "before",
        )
        for name in ("before", "shared", "independent")
    }
    config = Config(
        tracked_files={},
        profiles={},
        packages=packages,
        bundles={
            "tools": BundleSpec(
                components=[
                    BundleComponent(id="before", package="before"),
                    BundleComponent(
                        id="shared", package="shared", depends_on=["before"]
                    ),
                ]
            )
        },
    )
    resolved = ResolvedProfile(packages=["shared", "independent"], bundles=["tools"])
    owner = uuid.uuid4()
    ownership = OwnershipStore(state / "ownership")
    receipts = ReceiptStore(state / "receipts")

    plan = plan_provisioning(
        config, resolved, ownership_store=ownership, owner_id=owner
    )
    results = apply_provisioning(plan)
    with mutation_locks(resources=True):
        publish_installed_package_claims_locked(plan, results, owner_id=owner)

    assert has_hard_failure(results)
    assert (destination / "independent").read_bytes() == b"independent executable"
    assert not (destination / "before").exists()
    assert not (destination / "shared").exists()
    assert {identity.key for identity in receipts.installed_for("local")} == {
        "independent"
    }
    assert ownership.read(ResourceId.package("local", "shared")) is None
    assert ownership.read(ResourceId.package("local", "before")) is None

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as payload:
        payload.writestr("before", b"prerequisite executable")
    (tracked / "before.zip").write_bytes(archive.getvalue())

    plan = plan_provisioning(
        config, resolved, ownership_store=ownership, owner_id=owner
    )
    results = apply_provisioning(plan)
    with mutation_locks(resources=True):
        publish_installed_package_claims_locked(plan, results, owner_id=owner)

    assert not has_hard_failure(results)
    installed = [
        identity.key for result in results for identity in result.delta.installed
    ]
    assert installed == ["before", "shared"]
    assert (destination / "before").read_bytes() == b"prerequisite executable"
    assert (destination / "shared").read_bytes() == b"shared executable"
    for name in packages:
        claim = ownership.read(ResourceId.package("local", name))
        assert claim is not None
        assert claim.owner_id == owner
        assert claim.authority is Authority.MANAGE

    before = {
        path.name: path.read_bytes() for path in (state / "receipts").glob("*.json")
    }
    plan = plan_provisioning(
        config, resolved, ownership_store=ownership, owner_id=owner
    )
    results = apply_provisioning(plan)

    assert all(result.delta.is_empty() for result in results)
    assert all(
        outcome.outcome is Outcome.SKIP
        for result in results
        for outcome in result.outcomes
    )
    assert {
        path.name: path.read_bytes() for path in (state / "receipts").glob("*.json")
    } == before
