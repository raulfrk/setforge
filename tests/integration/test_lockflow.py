"""Integration tier: the real ``lock`` → ``install`` → ``install --locked`` flow.

Unlike the per-unit tests (``test_cli_lock`` / ``test_install_locked`` /
``test_lock_apply``), this exercises the WHOLE command chain end-to-end through
the real CLI against a real git-backed config repo, with only the two network
boundaries mocked:

- the resolver registry (so ``setforge lock`` writes a real ``setforge.lock``
  from canned pins without touching crates.io / GitHub / the marketplace), and
- ``GitHubReleaseProvisioner._download`` (so ``install`` verifies the locked
  checksum against known bytes OFFLINE — the drift signal is real, not stubbed).

The load-bearing observables are all POST-PARSE (CLI exit code + the on-disk
``setforge.lock`` bytes + the installed binary), never registered fake stdout:

1. ``lock`` writes a concrete-version, integrity-bearing lock for a config with
   multiple ecosystems (cargo + github_release); no ``latest`` in the file.
2. ``install`` consumes that lock with NO resolver/network call (the registry is
   emptied for the install step — a re-resolve would raise), landing the pinned
   github_release binary whose bytes match the locked checksum.
3. ``install --locked`` exits 0 on a fully-covered lock and non-zero when a
   declared lockable package has no lock entry.
4. A TAMPERED lock checksum makes ``install`` fail non-zero (the github_release
   verify path rejects the mismatched bytes) — drift is caught offline.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import ClassVar

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge.cli import app
from setforge.lockfile import lock_path, parse_lock
from setforge.provision.resolve import registry
from setforge.provision.resolve.protocol import (
    IntegrityKind,
    PackageType,
    ResolvedPin,
)

pytestmark = pytest.mark.integration

# The github_release asset bytes the mocked download returns. ``extract: false``
# in the config means install treats these verbatim as the binary, so the
# locked checksum is simply the sha256 of THESE bytes.
_ASSET_BYTES = b"#!/bin/sh\necho locked-tool\n"
_ASSET_SHA = hashlib.sha256(_ASSET_BYTES).hexdigest()

_CRATE_VERSION = "1.2.3"
_CRATE_SHA = "a" * 64
_GHR_TAG = "v2.0.0"
_PROFILE = "lockflow"


_CONFIG_YAML = f"""\
version: 1
schema_version: '5.0'
tracked_files:
  note:
    src: note.md
    dst: ~/.setforge_lockflow/note.md
packages:
  ripgrep:
    type: cargo
    crate: ripgrep
  toolbin:
    type: github_release
    repo: acme/toolbin
    tag: latest
    asset: toolbin
    binary: toolbin
    install: ~/.setforge_lockflow/bin
    extract: false
profiles:
  {_PROFILE}:
    tracked_files:
      - note
    packages:
      - ripgrep
      - toolbin
"""


def _pin(pkg_type: PackageType, key: str, version: str, integrity: str) -> ResolvedPin:
    kind = (
        IntegrityKind.SUM
        if pkg_type is PackageType.GO
        else IntegrityKind.SHA
        if pkg_type is PackageType.PLUGIN
        else IntegrityKind.CHECKSUM
    )
    return ResolvedPin(
        type=pkg_type,
        key=key,
        version=version,
        integrity=integrity,
        integrity_kind=kind,
    )


def _register_lock_stubs() -> None:
    """Register canned resolvers so ``lock`` resolves without the network.

    cargo → the crate name + a sha256 checksum; github_release → the CONCRETE
    tag (never the ``latest`` spec alias) + the sha256 of the bytes the mocked
    download will return, so the written lock and the install path agree.
    """

    class _CargoStub:
        type: ClassVar[PackageType] = PackageType.CARGO

        def resolve(self, item: object) -> ResolvedPin:
            return _pin(
                PackageType.CARGO, "ripgrep", _CRATE_VERSION, f"sha256:{_CRATE_SHA}"
            )

    class _GhrStub:
        type: ClassVar[PackageType] = PackageType.GITHUB_RELEASE

        def resolve(self, item: object) -> ResolvedPin:
            return _pin(
                PackageType.GITHUB_RELEASE,
                "acme/toolbin",
                _GHR_TAG,
                f"sha256:{_ASSET_SHA}",
            )

    registry.register(PackageType.CARGO)(_CargoStub)
    registry.register(PackageType.GITHUB_RELEASE)(_GhrStub)


@pytest.fixture
def lockflow_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[Callable[..., Result]]:
    """A git-backed config repo + sandboxed HOME/state + a CLI runner.

    Mirrors ``tests/integration/conftest.py``'s hardening (host HOME/state
    redirected, welcome suppressed), but the config carries real ``packages:``
    entries. The resolver registry is snapshotted and restored around the test;
    the github_release download is mocked to return fixed bytes so the whole
    install runs offline.
    """
    home = tmp_path / "home"
    state = tmp_path / "state"
    repo = tmp_path / "repo"
    for d in (home, state, repo / "tracked"):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    monkeypatch.setenv("SETFORGE_NO_WELCOME", "1")
    monkeypatch.setattr(Path, "home", lambda: Path(os.environ["HOME"]))

    (repo / "tracked" / "note.md").write_text("locked\n", encoding="utf-8")
    config = repo / "setforge.yaml"
    config.write_text(_CONFIG_YAML, encoding="utf-8")

    monkeypatch.setattr(
        "setforge.provision.github_release.GitHubReleaseProvisioner._download",
        lambda self, url: _ASSET_BYTES,
    )

    saved = dict(registry._REGISTRY)
    registry._REGISTRY.clear()

    def run(argv: list[str]) -> Result:
        args = [*argv, f"--config={config}", f"--profile={_PROFILE}"]
        return CliRunner().invoke(app, args)

    try:
        yield run
    finally:
        registry._REGISTRY.clear()
        registry._REGISTRY.update(saved)


def test_lock_write_offline_consume_locked_pass_and_drift(
    lockflow_env: Callable[..., Result], tmp_path: Path
) -> None:
    config = tmp_path / "repo" / "setforge.yaml"
    lock_file = lock_path(config)

    # --- lock: resolve (stubbed) → write a concrete, integrity-bearing lock ---
    _register_lock_stubs()
    lock_res = lockflow_env(["lock"])
    assert lock_res.exit_code == 0, lock_res.output
    assert lock_file.exists(), "lock verb did not write setforge.lock"

    text = lock_file.read_text(encoding="utf-8")
    assert "latest" not in text, f"lock must never record 'latest':\n{text}"
    lock = parse_lock(text)
    by_key = {(p.type.value, p.key): p for p in lock.packages}
    assert by_key[("cargo", "ripgrep")].version == _CRATE_VERSION
    assert by_key[("cargo", "ripgrep")].integrity == f"sha256:{_CRATE_SHA}"
    ghr = by_key[("github_release", "acme/toolbin")]
    assert ghr.version == _GHR_TAG, "concrete tag must replace the 'latest' spec alias"
    assert ghr.integrity == f"sha256:{_ASSET_SHA}"

    # --- install: consume the lock OFFLINE (empty registry ⇒ re-resolve raises) ---
    registry._REGISTRY.clear()
    install_res = lockflow_env(["install", "--yes"])
    assert install_res.exit_code == 0, install_res.output
    installed = Path(os.environ["HOME"]) / ".setforge_lockflow" / "bin" / "toolbin"
    assert installed.exists(), (
        f"pinned github_release binary not installed:\n{install_res.output}"
    )
    assert installed.read_bytes() == _ASSET_BYTES

    # --- install --locked: passes on a fully-covered lock (idempotent reinstall) ---
    locked_ok = lockflow_env(["install", "--locked", "--yes"])
    assert locked_ok.exit_code == 0, locked_ok.output

    # --- install --locked: FAILS when a declared lockable package is missing ---
    trimmed = parse_lock(text)
    kept = tuple(p for p in trimmed.packages if p.key != "acme/toolbin")
    lock_file.write_text(_dump_pins(kept, trimmed.version), encoding="utf-8")
    locked_missing = lockflow_env(["install", "--locked", "--yes"])
    assert locked_missing.exit_code != 0, locked_missing.output
    assert "no setforge.lock entry" in locked_missing.output

    # --- install: a TAMPERED checksum makes install FAIL offline (drift caught) ---
    tampered = text.replace(_ASSET_SHA, "b" * 64)
    assert tampered != text, "tamper did not alter the lock"
    lock_file.write_text(tampered, encoding="utf-8")
    # Fresh receipt store so the github_release item is not SKIP-present.
    for receipt in (Path(os.environ["SETFORGE_STATE_DIR"]) / "receipts").glob("*"):
        receipt.unlink()
    installed.unlink()
    drift_res = lockflow_env(["install", "--yes"])
    assert drift_res.exit_code != 0, (
        f"install must fail on a drifted (tampered) checksum:\n{drift_res.output}"
    )
    assert not installed.exists(), "tampered install must not land the binary"


def _dump_pins(pins: tuple[ResolvedPin, ...], version: int) -> str:
    """Serialize a hand-trimmed pin set the same way the writer does."""
    from setforge.lockfile import LockFile, dump_lock

    return dump_lock(LockFile(version=version, packages=pins))
