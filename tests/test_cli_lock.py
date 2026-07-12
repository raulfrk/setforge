"""Tests for the ``setforge lock`` subcommand (spec §B4 / Task 5).

The resolver boundary is the registry: each test registers STUB resolvers that
return canned pins (or raise) via the autouse ``_isolate_registry`` fixture, so
no test touches the network. Determinism, atomicity, and the (de)serialization
round-trip come from :mod:`setforge.lockfile` (Task 1) and are asserted here at
the verb level (byte-stable output, one pin per ``(type, key)``).
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
from typer.testing import CliRunner

from setforge.cli import app
from setforge.errors import ResolveError
from setforge.lockfile import parse_lock
from setforge.provision.resolve import registry
from setforge.provision.resolve.protocol import (
    IntegrityKind,
    PackageType,
    ResolvedPin,
)

# ---------------------------------------------------------------------------
# Registry isolation + stub resolvers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_registry() -> object:
    """Snapshot + clear + restore the resolver registry around each test.

    Clears the real (network-hitting) resolvers so the stubs below own the
    dispatch surface, then restores the real set on exit.
    """
    saved = dict(registry._REGISTRY)
    registry._REGISTRY.clear()
    try:
        yield
    finally:
        registry._REGISTRY.clear()
        registry._REGISTRY.update(saved)


def _pin(pkg_type: PackageType, key: str, version: str) -> ResolvedPin:
    """A canned pin with an ecosystem-appropriate integrity shape."""
    if pkg_type is PackageType.GO:
        integrity, kind = "h1:deadbeef", IntegrityKind.SUM
    elif pkg_type is PackageType.PLUGIN:
        integrity, kind = "a" * 40, IntegrityKind.SHA
    else:
        integrity, kind = "sha256:cafe", IntegrityKind.CHECKSUM
    return ResolvedPin(
        type=pkg_type,
        key=key,
        version=version,
        integrity=integrity,
        integrity_kind=kind,
    )


def _register_stub(
    pkg_type: PackageType, key: str, version: str, *, raises: bool = False
) -> None:
    """Register a stub resolver for ``pkg_type`` returning ``_pin`` (or raising)."""

    class _Stub:
        type: ClassVar[PackageType] = pkg_type

        def resolve(self, item: object) -> ResolvedPin:
            if raises:
                raise ResolveError(f"stub resolve failure for {key!r}")
            return _pin(pkg_type, key, version)

    registry.register(pkg_type)(_Stub)


# ---------------------------------------------------------------------------
# Config fixture builders
# ---------------------------------------------------------------------------

_FULL_YAML = """\
version: 1
tracked_files:
  d:
    src: tracked_file.txt
    dst: ~/.some-tracked_file
marketplaces:
  my-market:
    source: github
    repo: owner/repo
claude_plugins:
  myplugin:
    marketplace: my-market
packages:
  ripgrep:
    type: cargo
    crate: ripgrep
  black:
    type: python
    package: black
  local-thing:
    type: local
    path: /opt/thing
    binary: thing
    install: "echo hi"
  ghr-tool:
    type: github_release
    repo: owner/tool
    tag: v1.0.0
    asset: tool.tar.gz
    binary: tool
    install: "echo hi"
profiles:
  p:
    tracked_files: [d]
    packages: [ripgrep, black, local-thing, ghr-tool]
    claude_plugins: [myplugin]
    extensions:
      include: [esbenp.prettier-vscode]
"""


def _write_config(tmp_path: Path, content: str = _FULL_YAML) -> Path:
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(content, encoding="utf-8")
    (tmp_path / "tracked").mkdir(exist_ok=True)
    (tmp_path / "tracked" / "tracked_file.txt").write_text("data\n", encoding="utf-8")
    return cfg


def _register_full_stubs() -> None:
    _register_stub(PackageType.CARGO, "ripgrep", "14.0.0")
    _register_stub(PackageType.PYTHON, "black", "24.1.0")
    _register_stub(PackageType.GITHUB_RELEASE, "owner/tool", "v1.0.0")
    _register_stub(PackageType.PLUGIN, "myplugin@my-market", "b" * 40)
    _register_stub(PackageType.EXTENSION, "esbenp.prettier-vscode", "10.1.0")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_lock_writes_one_pin_per_package(tmp_path: Path) -> None:
    """lock resolves cargo+python+github_release+plugin+extension; local skipped."""
    cfg = _write_config(tmp_path)
    _register_full_stubs()

    result = CliRunner().invoke(app, ["lock", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 0, result.output

    lock = parse_lock((tmp_path / "setforge.lock").read_text(encoding="utf-8"))
    keys = sorted((p.type.value, p.key) for p in lock.packages)
    assert keys == [
        ("cargo", "ripgrep"),
        ("extension", "esbenp.prettier-vscode"),
        ("github_release", "owner/tool"),
        ("plugin", "myplugin@my-market"),
        ("python", "black"),
    ], "local package must be skipped; one pin per other declared package"
    assert all(p.profiles == ("p",) for p in lock.packages)


def test_lock_output_is_deterministic(tmp_path: Path) -> None:
    """Two lock runs on the same config produce byte-identical setforge.lock."""
    cfg = _write_config(tmp_path)
    _register_full_stubs()
    lock_file = tmp_path / "setforge.lock"

    CliRunner().invoke(app, ["lock", "--profile=p", f"--config={cfg}"])
    first = lock_file.read_text(encoding="utf-8")
    CliRunner().invoke(app, ["lock", "--profile=p", f"--config={cfg}"])
    second = lock_file.read_text(encoding="utf-8")
    assert first == second


def test_lock_fail_closed_leaves_no_partial_lock(tmp_path: Path) -> None:
    """One resolver raising aborts the whole write: NO setforge.lock is created."""
    cfg = _write_config(tmp_path)
    _register_stub(PackageType.CARGO, "ripgrep", "14.0.0")
    _register_stub(PackageType.PYTHON, "black", "24.1.0", raises=True)
    _register_stub(PackageType.GITHUB_RELEASE, "owner/tool", "v1.0.0")
    _register_stub(PackageType.PLUGIN, "myplugin@my-market", "b" * 40)
    _register_stub(PackageType.EXTENSION, "esbenp.prettier-vscode", "10.1.0")

    result = CliRunner().invoke(app, ["lock", "--profile=p", f"--config={cfg}"])
    assert result.exit_code != 0
    assert not (tmp_path / "setforge.lock").exists(), (
        "fail-closed: a resolver failure must not leave a partial lock"
    )


def test_lock_merges_other_profile_entries(tmp_path: Path) -> None:
    """Locking profile B preserves profile A's entries and unions a shared key."""
    yaml = """\
version: 1
tracked_files:
  d: {src: tracked_file.txt, dst: ~/.some-tracked_file}
packages:
  ripgrep: {type: cargo, crate: ripgrep}
  black: {type: python, package: black}
profiles:
  a:
    packages: [ripgrep]
  b:
    packages: [ripgrep, black]
"""
    cfg = _write_config(tmp_path, yaml)
    _register_stub(PackageType.CARGO, "ripgrep", "14.0.0")
    CliRunner().invoke(app, ["lock", "--profile=a", f"--config={cfg}"])

    _register_stub(PackageType.PYTHON, "black", "24.1.0")
    result = CliRunner().invoke(app, ["lock", "--profile=b", f"--config={cfg}"])
    assert result.exit_code == 0, result.output

    lock = parse_lock((tmp_path / "setforge.lock").read_text(encoding="utf-8"))
    by_key = {(p.type.value, p.key): p for p in lock.packages}
    assert set(by_key) == {("cargo", "ripgrep"), ("python", "black")}
    # ripgrep is shared: both profiles in the back-ref.
    assert by_key[("cargo", "ripgrep")].profiles == ("a", "b")
    assert by_key[("python", "black")].profiles == ("b",)


def test_lock_version_conflict_across_profiles_errors(tmp_path: Path) -> None:
    """A shared key resolving to a DIFFERENT version is a clean conflict error."""
    yaml = """\
version: 1
tracked_files:
  d: {src: tracked_file.txt, dst: ~/.some-tracked_file}
packages:
  ripgrep: {type: cargo, crate: ripgrep}
profiles:
  a: {packages: [ripgrep]}
  b: {packages: [ripgrep]}
"""
    cfg = _write_config(tmp_path, yaml)
    _register_stub(PackageType.CARGO, "ripgrep", "14.0.0")
    CliRunner().invoke(app, ["lock", "--profile=a", f"--config={cfg}"])
    before = (tmp_path / "setforge.lock").read_text(encoding="utf-8")

    # Profile b resolves the SAME key to a different version.
    registry._REGISTRY.clear()
    _register_stub(PackageType.CARGO, "ripgrep", "99.0.0")
    result = CliRunner().invoke(app, ["lock", "--profile=b", f"--config={cfg}"])
    assert result.exit_code != 0
    # The LockConflict propagates through the app callback; its message names
    # both versions (CliRunner surfaces it as result.exception, not stdout).
    message = str(result.exception)
    assert "14.0.0" in message
    assert "99.0.0" in message
    # The pre-existing lock is left unchanged.
    assert (tmp_path / "setforge.lock").read_text(encoding="utf-8") == before


def test_lock_update_reresolves_only_named_package(tmp_path: Path) -> None:
    """lock --update <key> rewrites just that pin, preserving the rest."""
    cfg = _write_config(tmp_path)
    _register_full_stubs()
    CliRunner().invoke(app, ["lock", "--profile=p", f"--config={cfg}"])

    # Re-register cargo to a new version; --update=ripgrep should pick it up.
    registry._REGISTRY.clear()
    _register_full_stubs()
    registry._REGISTRY.clear()
    _register_stub(PackageType.CARGO, "ripgrep", "15.0.0")
    _register_stub(PackageType.PYTHON, "black", "99.9.9")
    _register_stub(PackageType.GITHUB_RELEASE, "owner/tool", "v9.9.9")
    _register_stub(PackageType.PLUGIN, "myplugin@my-market", "c" * 40)
    _register_stub(PackageType.EXTENSION, "esbenp.prettier-vscode", "99.9.9")

    result = CliRunner().invoke(
        app, ["lock", "--profile=p", "--update=ripgrep", f"--config={cfg}"]
    )
    assert result.exit_code == 0, result.output

    lock = parse_lock((tmp_path / "setforge.lock").read_text(encoding="utf-8"))
    by_key = {p.key: p for p in lock.packages}
    assert by_key["ripgrep"].version == "15.0.0", "updated package re-resolved"
    assert by_key["black"].version == "24.1.0", "other packages preserved verbatim"


def test_lock_update_without_existing_lock_errors(tmp_path: Path) -> None:
    """lock --update with no lock file present exits non-zero cleanly."""
    cfg = _write_config(tmp_path)
    _register_full_stubs()
    result = CliRunner().invoke(
        app, ["lock", "--profile=p", "--update=ripgrep", f"--config={cfg}"]
    )
    assert result.exit_code != 0
    assert not (tmp_path / "setforge.lock").exists()


def test_lock_help_lists_flags(tmp_path: Path) -> None:
    """setforge lock --help renders and mentions --profile and --update."""
    result = CliRunner().invoke(app, ["lock", "--help"])
    assert result.exit_code == 0
    assert "--profile" in result.output
    assert "--update" in result.output
