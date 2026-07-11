"""Package-provisioning dispatch and its wiring into ``install``.

Two layers:

- unit tests over :mod:`setforge.provision.dispatch` (item resolution, dedup,
  grouping, ADDITIVE reconcile, bundles-skip notice, unknown-type raise,
  report-only purity);
- integration tests driving the real ``install`` CLI against a sandboxed
  ``$HOME`` with the cargo subprocess monkeypatched, proving the exit gate:
  a HARD outcome exits 1, a SOFT outcome warns but exits 0, ``cargo_binaries``
  now flow through the provisioner (the discarded-failure fix), and a profile
  referencing an undefined package key fails config load.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge.cli import app
from setforge.config import (
    CargoPackage,
    Config,
    Profile,
    ResolvedProfile,
    TrackedFile,
    load_config,
)
from setforge.errors import ConfigError, UnknownProvisionerType
from setforge.provision.dispatch import (
    has_hard_failure,
    resolve_provision_items,
    run_provisioning,
)
from setforge.provision.protocol import (
    Identity,
    Outcome,
    ProvisionDelta,
    Provisioner,
    ProvisionItem,
    ProvisionOutcome,
    ReconcileResult,
)
from setforge.provision.registry import _REGISTRY, register

_PROFILE = "prov-test"


# --------------------------------------------------------------------------
# A HARD-capable stub provisioner, registered under a private type so the
# gate can be exercised (cargo never produces HARD today). Registered once
# per session; removed at teardown so the registry stays clean for siblings.
# --------------------------------------------------------------------------


class _StubProvisioner(Provisioner):
    """Records apply calls; emits HARD/SOFT/OK per the class-level sets."""

    type = "stubprov"
    hard: ClassVar[set[str]] = set()
    soft: ClassVar[set[str]] = set()
    applied: ClassVar[list[str]] = []

    def probe(self) -> set[Identity]:
        return set()

    def plan(
        self, items: Sequence[ProvisionItem], installed: set[Identity]
    ) -> ProvisionDelta:
        return ProvisionDelta(
            installed=tuple(i.identity for i in items if i.identity not in installed)
        )

    def apply_one(self, item: ProvisionItem) -> ProvisionOutcome:
        key = item.identity.key
        type(self).applied.append(key)
        if key in type(self).hard:
            return ProvisionOutcome(item=item, outcome=Outcome.HARD, detail="boom")
        if key in type(self).soft:
            return ProvisionOutcome(item=item, outcome=Outcome.SOFT, detail="skipped")
        return ProvisionOutcome(item=item, outcome=Outcome.OK, detail="installed")

    def uninstall_one(self, identity: Identity) -> None:  # pragma: no cover
        pass


@pytest.fixture
def stub_provisioner() -> object:
    """Register ``_StubProvisioner`` under ``stubprov`` for the test's lifetime."""
    _StubProvisioner.hard = set()
    _StubProvisioner.soft = set()
    _StubProvisioner.applied = []
    if "stubprov" not in _REGISTRY:
        register("stubprov")(_StubProvisioner)
    yield _StubProvisioner
    _REGISTRY.pop("stubprov", None)


def _cfg(**kw: object) -> Config:
    return Config(
        tracked_files={"d": TrackedFile(src=Path("a"), dst="b")},
        profiles={_PROFILE: Profile()},
        **kw,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------
# Item resolution + dedup.
# --------------------------------------------------------------------------


def test_resolves_cargo_package_to_item() -> None:
    cfg = _cfg(packages={"rg": CargoPackage(crate="ripgrep")})
    resolved = ResolvedProfile(packages=["rg"])
    items = resolve_provision_items(cfg, resolved)
    assert len(items) == 1
    assert items[0].type == "cargo"
    assert items[0].identity == Identity(key="ripgrep", display="ripgrep")


def test_resolves_cargo_binaries_to_items() -> None:
    # The :294 fix: cargo_binaries now flow through the provisioner as items.
    cfg = _cfg()
    resolved = ResolvedProfile(cargo_binaries=["ast-grep", "just"])
    items = resolve_provision_items(cfg, resolved)
    assert {i.identity.key for i in items} == {"ast-grep", "just"}
    assert all(i.type == "cargo" for i in items)


def test_dedup_crate_in_both_packages_and_cargo_binaries() -> None:
    cfg = _cfg(packages={"rg": CargoPackage(crate="ripgrep")})
    resolved = ResolvedProfile(packages=["rg"], cargo_binaries=["ripgrep"])
    items = resolve_provision_items(cfg, resolved)
    assert len(items) == 1
    assert items[0].identity.key == "ripgrep"


def test_blank_cargo_binary_skipped() -> None:
    cfg = _cfg()
    resolved = ResolvedProfile(cargo_binaries=["", "  "])
    assert resolve_provision_items(cfg, resolved) == []


# --------------------------------------------------------------------------
# run_provisioning: grouping, ADDITIVE apply, report-only, bundles, unknown.
# --------------------------------------------------------------------------


def test_run_provisioning_applies_cargo_binaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # run_provisioning groups cargo_binaries under type=cargo, builds the real
    # CargoProvisioner, and reconciles them ADDITIVE. Stub apply_one so no real
    # cargo runs; assert both crates were applied through the driver.
    import setforge.provision.cargo as cargo_prov

    applied: list[str] = []

    def _apply(self: object, item: ProvisionItem) -> ProvisionOutcome:
        applied.append(item.identity.key)
        return ProvisionOutcome(item=item, outcome=Outcome.OK, detail="installed")

    monkeypatch.setattr(cargo_prov.CargoProvisioner, "probe", lambda self: set())
    monkeypatch.setattr(cargo_prov.CargoProvisioner, "apply_one", _apply)
    cfg = _cfg()
    resolved = ResolvedProfile(cargo_binaries=["ast-grep", "just"])
    results = run_provisioning(cfg, resolved)
    assert len(results) == 1  # one type group (cargo)
    assert set(applied) == {"ast-grep", "just"}


def test_report_only_applies_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    # report_only threads to reconcile: delta computed, apply_one never called.
    import setforge.provision.cargo as cargo_prov

    def _boom(self: object, item: ProvisionItem) -> ProvisionOutcome:
        raise AssertionError("report_only must not apply")

    monkeypatch.setattr(cargo_prov.CargoProvisioner, "probe", lambda self: set())
    monkeypatch.setattr(cargo_prov.CargoProvisioner, "apply_one", _boom)
    cfg = _cfg()
    resolved = ResolvedProfile(cargo_binaries=["ast-grep"])
    results = run_provisioning(cfg, resolved, report_only=True)
    assert len(results) == 1
    assert results[0].reported is True
    assert results[0].outcomes == ()
    # The delta still names the planned crate.
    assert Identity(key="ast-grep", display="ast-grep") in results[0].delta.installed


def test_bundles_skipped_with_notice(capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _cfg()
    resolved = ResolvedProfile(bundles=["dev"])
    results = run_provisioning(cfg, resolved)
    assert results == []
    err = capsys.readouterr().err
    assert "bundles not yet supported" in err
    assert "dev" in err


def test_unknown_type_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # A declared package whose type has no registered provisioner surfaces as
    # UnknownProvisionerType from run_provisioning's build() call — a real
    # config error, never a silent skip. Simulate the not-yet-wired case by
    # making resolve_provision_items yield an item of an unregistered type.
    import setforge.provision.dispatch as dispatch

    monkeypatch.setattr(
        dispatch,
        "resolve_provision_items",
        lambda cfg, resolved: [
            ProvisionItem(type="npm-not-real", identity=Identity(key="x", display="x"))
        ],
    )
    with pytest.raises(UnknownProvisionerType):
        run_provisioning(_cfg(), ResolvedProfile())


def test_has_hard_failure_detects_hard() -> None:
    from setforge.config import ReconcilePolicy
    from setforge.provision.driver import reconcile

    class _P(_StubProvisioner):
        hard: ClassVar[set[str]] = {"b"}

    items = [
        ProvisionItem(type="stubprov", identity=Identity(key="a", display="a")),
        ProvisionItem(type="stubprov", identity=Identity(key="b", display="b")),
    ]
    result = reconcile(_P(), items, policy=ReconcilePolicy.ADDITIVE)
    assert has_hard_failure([result]) is True


# --------------------------------------------------------------------------
# Config-load reference validation.
# --------------------------------------------------------------------------


def test_undefined_package_reference_fails_load(tmp_path: Path) -> None:
    config = tmp_path / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  d:\n"
        "    src: a\n"
        "    dst: b\n"
        "packages:\n"
        "  rg:\n"
        "    type: cargo\n"
        "    crate: ripgrep\n"
        "profiles:\n"
        "  p:\n"
        "    packages:\n"
        "      - typo\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="undeclared name"):
        load_config(config)


def test_undefined_bundle_reference_fails_load(tmp_path: Path) -> None:
    config = tmp_path / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  d:\n"
        "    src: a\n"
        "    dst: b\n"
        "profiles:\n"
        "  p:\n"
        "    bundles:\n"
        "      - nope\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="undeclared name"):
        load_config(config)


def test_valid_package_reference_loads(tmp_path: Path) -> None:
    config = tmp_path / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  d:\n"
        "    src: a\n"
        "    dst: b\n"
        "packages:\n"
        "  rg:\n"
        "    type: cargo\n"
        "    crate: ripgrep\n"
        "profiles:\n"
        "  p:\n"
        "    packages:\n"
        "      - rg\n",
        encoding="utf-8",
    )
    cfg = load_config(config)
    assert cfg.profiles["p"].packages == ["rg"]


# --------------------------------------------------------------------------
# Integration through the real install CLI (cargo subprocess monkeypatched).
# --------------------------------------------------------------------------


@pytest.fixture
def install_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    repo = tmp_path / "repo"
    (repo / "tracked").mkdir(parents=True)
    (repo / "tracked" / "note.md").write_text("v1\n", encoding="utf-8")
    return repo


def _write_install_config(repo: Path, *, packages_block: str = "") -> Path:
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  note:\n"
        "    src: note.md\n"
        "    dst: ~/.setforge_prov/note.md\n"
        f"{packages_block}"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - note\n"
        "    cargo_binaries:\n"
        "      - ripgrep\n",
        encoding="utf-8",
    )
    return config


def _install(config: Path) -> Result:
    return CliRunner().invoke(
        app,
        [
            "install",
            f"--profile={_PROFILE}",
            f"--config={config}",
            "--no-secrets-scan",
            "--no-git-check",
            "--yes",
        ],
    )


def test_install_cargo_binary_soft_failure_warns_exits_zero(
    install_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A build failure is SOFT: the provisioner records it, install warns, exit 0.
    # Patch apply_one directly (a narrow seam) so the fake never leaks into the
    # plugin / mcp / transition subprocesses the way a module-level
    # subprocess.run patch would.
    import setforge.provision.cargo as cargo_prov

    def _soft_apply(self: object, item: ProvisionItem) -> ProvisionOutcome:
        return ProvisionOutcome(item=item, outcome=Outcome.SOFT, detail="build failed")

    monkeypatch.setattr(cargo_prov.CargoProvisioner, "apply_one", _soft_apply)
    config = _write_install_config(install_repo)
    result = _install(config)
    assert result.exit_code == 0, result.output
    assert "ripgrep" in result.output


def test_install_missing_cargo_is_soft_exits_zero(
    install_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import setforge.provision.cargo as cargo_prov

    monkeypatch.setattr(
        cargo_prov.CargoProvisioner, "_resolve", staticmethod(lambda: None)
    )
    config = _write_install_config(install_repo)
    result = _install(config)
    assert result.exit_code == 0, result.output


def test_install_hard_failure_gates_exit_one(
    install_repo: Path, monkeypatch: pytest.MonkeyPatch, stub_provisioner: object
) -> None:
    # Route the cargo package through a HARD-capable provisioner to prove the
    # gate: patch reconcile_packages to reconcile the crate under the stub.
    _StubProvisioner.hard = {"ripgrep"}
    import setforge.cli._provision_helpers as ph
    from setforge.config import ReconcilePolicy
    from setforge.provision.driver import reconcile

    def _fake_reconcile_packages(
        cfg: Config, resolved: ResolvedProfile
    ) -> list[ReconcileResult]:
        items = [
            ProvisionItem(
                type="stubprov", identity=Identity(key="ripgrep", display="ripgrep")
            )
        ]
        return [reconcile(_StubProvisioner(), items, policy=ReconcilePolicy.ADDITIVE)]

    monkeypatch.setattr(ph, "reconcile_packages", _fake_reconcile_packages)
    import setforge.cli.install as install_mod

    monkeypatch.setattr(install_mod, "reconcile_packages", _fake_reconcile_packages)
    config = _write_install_config(install_repo)
    result = _install(config)
    assert result.exit_code == 1, result.output
    assert "package-provisioning failures" in result.output


def test_install_dry_run_provisions_nothing(
    install_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import setforge.provision.cargo as cargo_prov

    applied: list[str] = []

    def _record_apply(self: object, item: ProvisionItem) -> object:  # pragma: no cover
        applied.append(item.identity.key)
        raise AssertionError("dry-run must not apply (install) any package")

    # probe() is read-only and may run in report mode; stub it so a real cargo
    # is not required. apply_one MUST never fire under --dry-run (report_only).
    monkeypatch.setattr(cargo_prov.CargoProvisioner, "probe", lambda self: set())
    monkeypatch.setattr(cargo_prov.CargoProvisioner, "apply_one", _record_apply)
    config = _write_install_config(install_repo)
    result = CliRunner().invoke(
        app,
        [
            "install",
            f"--profile={_PROFILE}",
            f"--config={config}",
            "--no-secrets-scan",
            "--no-git-check",
            "--yes",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "would-be package provision" in result.output
    assert "WOULD provision ripgrep" in result.output
    assert applied == []
