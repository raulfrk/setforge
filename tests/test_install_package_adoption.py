from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

import setforge.cli.install as install_mod
from setforge import transitions
from setforge.cli import app
from setforge.cli import cleanup as cleanup_mod
from setforge.ownership import Authority, OwnershipStore, read_owner_id
from setforge.provision.cargo import CargoProvisioner
from setforge.provision.go import GoProvisioner
from setforge.provision.ownership import package_resource_id
from setforge.provision.protocol import (
    Identity,
    ObservationOrigin,
    PackageObservation,
    ProvisionItem,
)
from setforge.provision.receipt import ReceiptStore, default_receipt_root


def _write_config(root: Path) -> Path:
    root.mkdir()
    config = root / "setforge.yaml"
    config.write_text(
        "schema_version: '6.0'\n"
        "tracked_files: {}\n"
        "packages:\n"
        "  rg:\n"
        "    type: cargo\n"
        "    crate: ripgrep\n"
        "profiles:\n"
        "  p:\n"
        "    packages: [rg]\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True)
    return config


def test_install_adopts_present_package_without_invoking_provider(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _write_config(tmp_path / "repo")
    identity = Identity("ripgrep", "ripgrep")
    observation = PackageObservation(
        identity,
        ObservationOrigin.EXTERNAL,
        version="14.1.0",
        source="crates.io",
    )
    calls: list[str] = []
    monkeypatch.setattr(CargoProvisioner, "probe", lambda self: {identity})
    monkeypatch.setattr(
        CargoProvisioner, "observations", lambda self, installed: (observation,)
    )

    def unexpected_apply(self, item):
        calls.append(item.identity.key)
        raise AssertionError("metadata-only adoption invoked the package provider")

    monkeypatch.setattr(CargoProvisioner, "apply_one", unexpected_apply)

    result = CliRunner().invoke(
        app,
        [
            "install",
            "--profile=p",
            f"--config={config}",
            "--yes",
            "--no-fetch",
            "--no-git-check",
            "--no-secrets-scan",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == []
    assert "adopted package ownership: ripgrep" in result.output
    resource_id = package_resource_id(ProvisionItem(type="cargo", identity=identity))
    claim = OwnershipStore().read(resource_id)
    assert claim is not None
    assert claim.authority is Authority.MANAGE
    assert claim.owner_id == read_owner_id(config.parent)

    repeated = CliRunner().invoke(
        app,
        [
            "install",
            "--profile=p",
            f"--config={config}",
            "--no-fetch",
            "--no-git-check",
            "--no-secrets-scan",
        ],
    )
    assert repeated.exit_code == 0, repeated.output
    assert calls == []
    assert "adopted package ownership" not in repeated.output


def test_install_refuses_noninteractive_adoption_without_yes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _write_config(tmp_path / "repo")
    identity = Identity("ripgrep", "ripgrep")
    observation = PackageObservation(
        identity,
        ObservationOrigin.EXTERNAL,
        version="14.1.0",
        source="crates.io",
    )
    monkeypatch.setattr(CargoProvisioner, "probe", lambda self: {identity})
    monkeypatch.setattr(
        CargoProvisioner, "observations", lambda self, installed: (observation,)
    )
    result = CliRunner().invoke(
        app,
        [
            "install",
            "--profile=p",
            f"--config={config}",
            "--no-fetch",
            "--no-git-check",
            "--no-secrets-scan",
        ],
    )
    assert result.exit_code == 1
    assert "requires confirmation" in str(result.exception)
    resource_id = package_resource_id(ProvisionItem(type="cargo", identity=identity))
    assert OwnershipStore().read(resource_id) is None


def test_install_transfers_foreign_package_without_invoking_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config_a = _write_config(tmp_path / "repo-a")
    identity = Identity("ripgrep", "ripgrep")
    observation = PackageObservation(
        identity,
        ObservationOrigin.EXTERNAL,
        version="14.1.0",
        source="crates.io",
    )
    monkeypatch.setattr(CargoProvisioner, "probe", lambda self: {identity})
    monkeypatch.setattr(
        CargoProvisioner, "observations", lambda self, installed: (observation,)
    )
    monkeypatch.setattr(
        CargoProvisioner,
        "apply_one",
        lambda self, item: pytest.fail("package transfer invoked provider"),
    )
    args = [
        "install",
        "--profile=p",
        "--yes",
        "--no-fetch",
        "--no-git-check",
        "--no-secrets-scan",
    ]
    adopted = CliRunner().invoke(app, [*args, f"--config={config_a}"])
    assert adopted.exit_code == 0, adopted.output
    resource_id = package_resource_id(ProvisionItem(type="cargo", identity=identity))
    store = OwnershipStore()
    before = store.read(resource_id)
    assert before is not None

    config_b = _write_config(tmp_path / "repo-b")
    transferred = CliRunner().invoke(app, [*args, f"--config={config_b}"])

    assert transferred.exit_code == 0, transferred.output
    assert "transferred package ownership: ripgrep" in transferred.output
    after = store.read(resource_id)
    assert after is not None
    assert after.owner_id == read_owner_id(config_b.parent)
    assert after.generation == before.generation + 1
    transition = transitions.load_latest("p")
    assert transition is not None
    assert transitions.load_ownership_transfers(transition) == (
        transitions.OwnershipTransferDelta(before, after),
    )
    config_b_before = config_b.read_bytes()
    config_b.write_text(
        config_b.read_text(encoding="utf-8").replace("packages: [rg]", "packages: []"),
        encoding="utf-8",
    )
    refused = CliRunner().invoke(
        app, ["revert", "--profile=p", f"--config={config_b}", "--yes"]
    )
    assert refused.exit_code != 0
    assert "ownership declaration changed" in str(refused.exception)
    assert store.read(resource_id) == after
    config_b.write_bytes(config_b_before)

    reverted = CliRunner().invoke(
        app, ["revert", "--profile=p", f"--config={config_b}", "--yes"]
    )
    assert reverted.exit_code == 0, reverted.output
    restored = store.read(resource_id)
    assert restored is not None
    assert restored.owner_id == before.owner_id


def test_later_install_failure_recovers_new_adoption_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _write_config(tmp_path / "repo")
    identity = Identity("ripgrep", "ripgrep")
    observation = PackageObservation(
        identity,
        ObservationOrigin.EXTERNAL,
        version="14.1.0",
        source="crates.io",
    )
    monkeypatch.setattr(CargoProvisioner, "probe", lambda self: {identity})
    monkeypatch.setattr(
        CargoProvisioner, "observations", lambda self, installed: (observation,)
    )

    def fail_after_adoption(*args: object, **kwargs: object) -> None:
        raise RuntimeError("later install phase failed")

    monkeypatch.setattr(install_mod, "_apply_capability_targets", fail_after_adoption)
    result = CliRunner().invoke(
        app,
        [
            "install",
            "--profile=p",
            f"--config={config}",
            "--yes",
            "--no-fetch",
            "--no-git-check",
            "--no-secrets-scan",
        ],
    )
    assert result.exit_code == 1
    resource_id = package_resource_id(ProvisionItem(type="cargo", identity=identity))
    assert OwnershipStore().read(resource_id) is None


def test_legacy_receipt_adoption_migrates_then_cleanup_removes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    repo = tmp_path / "repo"
    repo.mkdir()
    config = repo / "setforge.yaml"
    config.write_text(
        "schema_version: '6.0'\n"
        "tracked_files: {}\n"
        "packages:\n"
        "  tool:\n"
        "    type: go\n"
        "    module: example.com/tool\n"
        "    version: v1\n"
        "profiles:\n"
        "  p:\n"
        "    packages: [tool]\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True)
    identity = Identity("example.com/tool", "example.com/tool")
    binary = tmp_path / "bin" / "tool"
    binary.parent.mkdir()
    binary.write_text("tool", encoding="utf-8")
    receipts = ReceiptStore(default_receipt_root())
    receipts.record(identity, version="v1", checksum=None, path=binary)
    monkeypatch.setattr(GoProvisioner, "probe", lambda _self: {identity})
    monkeypatch.setattr(
        GoProvisioner,
        "apply_one",
        lambda _self, _item: pytest.fail("legacy adoption invoked provider"),
    )

    adopted = CliRunner().invoke(
        app,
        [
            "install",
            "--profile=p",
            f"--config={config}",
            "--yes",
            "--no-fetch",
            "--no-git-check",
            "--no-secrets-scan",
        ],
    )
    assert adopted.exit_code == 0, adopted.output
    migrated = receipts.entry_for(identity, "go")
    assert migrated is not None
    assert migrated.provider == "go"
    assert not receipts.receipt_path(identity, provider=None).exists()

    owner_id = read_owner_id(repo)
    items = cleanup_mod.discover_cleanup_items(
        receipts,
        declared=set(),
        declared_resources=frozenset(),
        console=Console(),
        ownership_store=OwnershipStore(),
        owner_id=owner_id,
    )
    assert [(item.provider, item.managed) for item in items] == [("go", True)]

    class Provider:
        def probe(self) -> set[Identity]:
            return {identity} if binary.exists() else set()

        def observations(
            self, installed: set[Identity]
        ) -> tuple[PackageObservation, ...]:
            if identity not in installed:
                return ()
            return (
                PackageObservation(
                    identity,
                    ObservationOrigin.CURRENT_RECEIPT,
                    version="v1",
                    locator=str(binary),
                ),
            )

        def uninstall_one(self, removed: Identity) -> None:
            assert removed == identity
            binary.unlink()
            receipts.remove(identity, provider="go")

    monkeypatch.setattr(cleanup_mod, "build", lambda _item: Provider())
    monkeypatch.setattr(cleanup_mod, "_confinement_root", lambda: tmp_path)
    monkeypatch.setattr(
        cleanup_mod, "_pick_action", lambda _item: cleanup_mod.CleanupAction.DELETE
    )
    cleanup_mod._apply_cleanup("p", items, receipts, Console())

    assert not binary.exists()
    assert list(receipts.iter_receipts()) == []
    claim = OwnershipStore().read(
        package_resource_id(ProvisionItem(type="go", identity=identity))
    )
    assert claim is not None
    assert claim.authority is Authority.NONE


def test_later_failure_restores_legacy_receipt_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    repo = tmp_path / "repo"
    repo.mkdir()
    config = repo / "setforge.yaml"
    config.write_text(
        "schema_version: '6.0'\n"
        "tracked_files: {}\n"
        "packages:\n"
        "  tool: {type: go, module: example.com/tool, version: v1}\n"
        "profiles:\n"
        "  p: {packages: [tool]}\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True)
    identity = Identity("example.com/tool", "example.com/tool")
    receipts = ReceiptStore(default_receipt_root())
    receipts.record(identity, version="v1", checksum=None, path=tmp_path / "tool")
    monkeypatch.setattr(GoProvisioner, "probe", lambda _self: {identity})

    def later_failure(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("later failure")

    monkeypatch.setattr(
        install_mod,
        "_apply_capability_targets",
        later_failure,
    )

    result = CliRunner().invoke(
        app,
        [
            "install",
            "--profile=p",
            f"--config={config}",
            "--yes",
            "--no-fetch",
            "--no-git-check",
            "--no-secrets-scan",
        ],
    )

    assert result.exit_code == 1
    assert receipts.receipt_path(identity, provider=None).exists()
    assert not receipts.receipt_path(identity, provider="go").exists()
    resource_id = package_resource_id(ProvisionItem(type="go", identity=identity))
    assert OwnershipStore().read(resource_id) is None
