"""``setforge install --locked`` — spec→lock coverage gate (Task 6, spec §B4).

``--locked`` is FAIL-CLOSED: every package in the LOCKABLE surface
(:func:`setforge.cli.lock.enumerate_lock_items`) MUST have a matching lock
entry, else the install exits non-zero naming the missing package(s). It does
NOT re-resolve (no network) — it is a spec↔lock coverage check + the normal
locked install.

Two invariants under test:

- a lockable package (a top-level cargo package here) with no lock entry ⇒
  non-zero exit that names it;
- the KEY scoping regression: a ``cargo_binaries`` entry (NOT lockable — B10
  never writes it) absent from the lock MUST NOT fail ``--locked``. The gate is
  scoped to ``enumerate_lock_items``, not install's full dispatch item set.
"""

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
    # A top-level cargo package (lockable) with an EMPTY lock ⇒ non-zero exit
    # that names the missing package.
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
    # THE scoping regression: cargo_binaries is NOT lockable — B10 never writes
    # it. A config with a cargo_binaries entry + a lock that LACKS it must still
    # pass --locked (the gate is scoped to enumerate_lock_items). Stub cargo
    # apply so no real cargo runs.
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
    # Lock lacks any entry for the cargo_binary — must NOT false-fail.
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
    # Regression: with NO lock file, install behaves as today (resolves from
    # spec, no --locked). No lock ⇒ the lock override is a no-op.
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
    result = _install(config)  # no --locked
    assert result.exit_code == 0, result.output
