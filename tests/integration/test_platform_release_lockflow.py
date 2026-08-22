"""Offline real-CLI coverage for portable platform release-asset locks."""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge.cli import app
from setforge.lockfile import lock_path, parse_lock
from setforge.ownership import (
    OwnershipStore,
    ProvenanceFactKind,
    load_or_create_owner_id,
)
from setforge.provision.protocol import Identity
from setforge.provision.receipt import ReceiptStore, default_receipt_root

pytestmark = pytest.mark.integration

_PROFILE = "platform-lockflow"
_TAG = "v4.2.0"
_LINUX_ASSET = "tool-linux-amd64"
_MACOS_ASSET = "tool-macos-arm64"
_LINUX_BYTES = b"#!/bin/sh\necho linux-x86_64\n"
_MACOS_BYTES = b"#!/bin/sh\necho macos-aarch64\n"
_ASSETS = {_LINUX_ASSET: _LINUX_BYTES, _MACOS_ASSET: _MACOS_BYTES}

_CONFIG_YAML = f"""\
version: 1
schema_version: '6.3'
minimum_version: '6.3'
tracked_files: {{}}
packages:
  tool:
    type: github_release
    repo: acme/platform-tool
    tag: {_TAG}
    assets:
      - asset: {_LINUX_ASSET}
        os: linux
        arch: x86_64
      - asset: {_MACOS_ASSET}
        os: macos
        arch: aarch64
    binary: tool
    install: ~/.setforge-platform/bin
    extract: false
profiles:
  {_PROFILE}:
    packages: [tool]
"""


@pytest.fixture
def platform_lockflow_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Callable[[list[str]], Result], Path, list[str]]:
    """Sandbox HOME/state and replace both network boundaries with fixtures."""
    home = tmp_path / "home"
    state = tmp_path / "state"
    repo = tmp_path / "repo"
    for directory in (home, state, repo):
        directory.mkdir(parents=True, exist_ok=True)
    config = repo / "setforge.yaml"
    config.write_text(_CONFIG_YAML, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    load_or_create_owner_id(repo)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    monkeypatch.setenv("SETFORGE_NO_WELCOME", "1")
    monkeypatch.setattr(Path, "home", lambda: Path(os.environ["HOME"]))
    fetched: list[str] = []

    def fixture_fetch(url: str, *, user_agent: str | None = None) -> bytes:
        del user_agent
        fetched.append(url)
        for asset, payload in _ASSETS.items():
            if url.endswith(f"/{asset}"):
                return payload
        raise AssertionError(f"unexpected network URL: {url}")

    monkeypatch.setattr(
        "setforge.provision.resolve.github_release._default_fetch", fixture_fetch
    )
    monkeypatch.setattr(
        "setforge.provision.github_release.GitHubReleaseProvisioner._download",
        lambda self, url: fixture_fetch(url),
    )

    def run(argv: list[str]) -> Result:
        return CliRunner().invoke(
            app, [*argv, f"--config={config}", f"--profile={_PROFILE}"]
        )

    return run, config, fetched


def test_portable_lock_contains_all_variants_and_installs_selected_asset(
    platform_lockflow_env: tuple[Callable[[list[str]], Result], Path, list[str]],
) -> None:
    run, config, fetched = platform_lockflow_env

    locked = run(["lock"])
    assert locked.exit_code == 0, locked.output
    lock_file = lock_path(config)
    lock_text = lock_file.read_text(encoding="utf-8")
    parsed = parse_lock(lock_text)
    assert parsed.version == 2
    assert len(parsed.packages) == 1
    pin = parsed.packages[0]
    assert pin.version == _TAG
    assert [(row.os, row.arch, row.asset, row.checksum) for row in pin.artifacts] == [
        (
            "linux",
            "x86_64",
            _LINUX_ASSET,
            f"sha256:{hashlib.sha256(_LINUX_BYTES).hexdigest()}",
        ),
        (
            "macos",
            "aarch64",
            _MACOS_ASSET,
            f"sha256:{hashlib.sha256(_MACOS_BYTES).hexdigest()}",
        ),
    ]
    assert any(url.endswith(f"/{_LINUX_ASSET}") for url in fetched)
    assert any(url.endswith(f"/{_MACOS_ASSET}") for url in fetched)
    assert "host =" not in lock_text.casefold()

    fetched.clear()
    installed = run(["install", "--locked", "--yes", "--no-git-check"])
    assert installed.exit_code == 0, installed.output
    binary = Path(os.environ["HOME"]) / ".setforge-platform" / "bin" / "tool"
    assert binary.read_bytes() == _LINUX_BYTES
    assert any(url.endswith(f"/{_LINUX_ASSET}") for url in fetched)
    assert not any(url.endswith(f"/{_MACOS_ASSET}") for url in fetched)

    expected_checksum = f"sha256:{hashlib.sha256(_LINUX_BYTES).hexdigest()}"
    receipt = ReceiptStore(default_receipt_root()).entry_for(
        Identity("acme/platform-tool", "acme/platform-tool"), "github_release"
    )
    assert receipt is not None
    assert (receipt.artifact, receipt.platform, receipt.checksum) == (
        _LINUX_ASSET,
        "linux-x86_64",
        expected_checksum,
    )
    claims = OwnershipStore().list_claims()
    assert len(claims) == 1
    provenance = {(fact.kind, fact.value) for fact in claims[0].provenance}
    assert (ProvenanceFactKind.ARTIFACT, _LINUX_ASSET) in provenance
    assert (ProvenanceFactKind.PLATFORM, "linux-x86_64") in provenance

    fetched.clear()
    repeated = run(["install", "--locked", "--yes", "--no-git-check"])
    assert repeated.exit_code == 0, repeated.output
    assert fetched == []
    assert OwnershipStore().list_claims() == claims
