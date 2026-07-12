"""``setforge install --locked``: fail-closed coverage gate scoped to
:func:`enumerate_lock_items`, NOT install's full dispatch item set."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge.cli import app
from setforge.lockfile import LockFile, lock_path, write_lock
from setforge.provision.protocol import Outcome, ProvisionOutcome
from setforge.provision.resolve.protocol import (
    IntegrityKind,
    PackageType,
    ResolvedPin,
)

_PROFILE = "locked-test"


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


def _write_config(repo: Path, *, packages_block: str, profile_body: str) -> Path:
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  note:\n"
        "    src: note.md\n"
        "    dst: ~/.setforge_locked/note.md\n"
        f"{packages_block}"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - note\n"
        f"{profile_body}",
        encoding="utf-8",
    )
    return config


def _install(config: Path, *extra: str) -> Result:
    return CliRunner().invoke(
        app,
        [
            "install",
            f"--profile={_PROFILE}",
            f"--config={config}",
            "--no-secrets-scan",
            "--no-git-check",
            "--yes",
            *extra,
        ],
    )


def _pin(pkg_type: PackageType, key: str, version: str) -> ResolvedPin:
    integrity, kind = "sha256:cafe", IntegrityKind.CHECKSUM
    return ResolvedPin(
        type=pkg_type,
        key=key,
        version=version,
        integrity=integrity,
        integrity_kind=kind,
        profiles=(_PROFILE,),
    )


def test_locked_fails_when_lockable_package_missing_from_lock(
    install_repo: Path,
) -> None:
    config = _write_config(
        install_repo,
        packages_block=("packages:\n  rg:\n    type: cargo\n    crate: ripgrep\n"),
        profile_body="    packages:\n      - rg\n",
    )
    write_lock(LockFile(packages=()), lock_path(config))
    result = _install(config, "--locked")
    assert result.exit_code != 0, result.output
    assert "ripgrep" in result.output


def test_locked_does_not_fail_on_cargo_binaries_absent_from_lock(
    install_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # cargo_binaries is NOT lockable; absent from the lock must NOT fail --locked.
    import setforge.provision.cargo as cargo_prov

    monkeypatch.setattr(cargo_prov.CargoProvisioner, "probe", lambda self: set())
    monkeypatch.setattr(
        cargo_prov.CargoProvisioner,
        "apply_one",
        lambda self, item: ProvisionOutcome(
            item=item, outcome=Outcome.OK, detail="installed"
        ),
    )
    config = _write_config(
        install_repo,
        packages_block="",
        profile_body="    cargo_binaries:\n      - ast-grep\n",
    )
    write_lock(LockFile(packages=()), lock_path(config))
    result = _install(config, "--locked")
    assert result.exit_code == 0, result.output


def test_locked_passes_when_lockable_package_present(
    install_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import setforge.provision.cargo as cargo_prov

    monkeypatch.setattr(cargo_prov.CargoProvisioner, "probe", lambda self: set())
    monkeypatch.setattr(
        cargo_prov.CargoProvisioner,
        "apply_one",
        lambda self, item: ProvisionOutcome(
            item=item, outcome=Outcome.OK, detail="installed"
        ),
    )
    config = _write_config(
        install_repo,
        packages_block=("packages:\n  rg:\n    type: cargo\n    crate: ripgrep\n"),
        profile_body="    packages:\n      - rg\n",
    )
    write_lock(
        LockFile(packages=(_pin(PackageType.CARGO, "ripgrep", "14.0.0"),)),
        lock_path(config),
    )
    result = _install(config, "--locked")
    assert result.exit_code == 0, result.output


def test_no_lock_present_installs_from_spec_unchanged(
    install_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import setforge.provision.cargo as cargo_prov

    monkeypatch.setattr(cargo_prov.CargoProvisioner, "probe", lambda self: set())
    monkeypatch.setattr(
        cargo_prov.CargoProvisioner,
        "apply_one",
        lambda self, item: ProvisionOutcome(
            item=item, outcome=Outcome.OK, detail="installed"
        ),
    )
    config = _write_config(
        install_repo,
        packages_block="",
        profile_body="    cargo_binaries:\n      - ast-grep\n",
    )
    assert not lock_path(config).exists()
    result = _install(config)
    assert result.exit_code == 0, result.output
