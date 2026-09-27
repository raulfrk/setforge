"""Tests for the ``setforge lock`` subcommand: stub resolvers, no network."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
from typer.testing import CliRunner

from setforge.cli import app
from setforge.errors import ResolveError
from setforge.lockfile import LockFile, dump_lock, parse_lock
from setforge.provision.resolve import registry
from setforge.provision.resolve.protocol import (
    IntegrityKind,
    PackageType,
    ResolvedPin,
)


@pytest.fixture(autouse=True)
def _isolate_registry() -> object:
    saved = dict(registry._REGISTRY)
    registry._REGISTRY.clear()
    try:
        yield
    finally:
        registry._REGISTRY.clear()
        registry._REGISTRY.update(saved)


def _pin(pkg_type: PackageType, key: str, version: str) -> ResolvedPin:
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

    class _Stub:
        type: ClassVar[PackageType] = pkg_type

        def resolve(self, item: object) -> ResolvedPin:
            if raises:
                raise ResolveError(f"stub resolve failure for {key!r}")
            return _pin(pkg_type, key, version)

    registry.register(pkg_type)(_Stub)


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
  myplugin:
    type: plugin
    plugin: myplugin
  esbenp.prettier-vscode:
    type: extension
    extension: esbenp.prettier-vscode
profiles:
  p:
    tracked_files: [d]
    packages: [ripgrep, black, local-thing, ghr-tool, myplugin, esbenp.prettier-vscode]
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


def test_lock_checksum_mismatch_preserves_existing_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge.provision.resolve import github_release

    registry.register(PackageType.GITHUB_RELEASE)(github_release.GitHubReleaseResolver)
    asset_url = "https://github.com/owner/tool/releases/download/v1/tool"

    def fetch(url: str, *, user_agent: str | None = None) -> bytes:
        assert url == asset_url
        return b"bytes that do not match the manifest checksum"

    monkeypatch.setattr(github_release, "_default_fetch", fetch)
    config = _write_config(
        tmp_path,
        f"""\
version: 1
schema_version: '6.5'
tracked_files: {{}}
packages:
  tool:
    type: github_release
    repo: owner/tool
    tag: v1
    asset: tool
    binary: tool
    install: ~/.local/bin
    extract: false
    checksum: sha256:{"0" * 64}
profiles:
  p:
    packages: [tool]
""",
    )
    path = tmp_path / "setforge.lock"
    before = dump_lock(LockFile(packages=())).encode("utf-8")
    path.write_bytes(before)

    result = CliRunner().invoke(app, ["lock", "--profile=p", f"--config={config}"])

    assert result.exit_code != 0
    assert isinstance(result.exception, ResolveError)
    assert "checksum mismatch" in str(result.exception)
    assert path.read_bytes() == before


def test_lock_writes_one_pin_per_package(tmp_path: Path) -> None:
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
    cfg = _write_config(tmp_path)
    _register_full_stubs()
    lock_file = tmp_path / "setforge.lock"

    CliRunner().invoke(app, ["lock", "--profile=p", f"--config={cfg}"])
    first = lock_file.read_text(encoding="utf-8")
    CliRunner().invoke(app, ["lock", "--profile=p", f"--config={cfg}"])
    second = lock_file.read_text(encoding="utf-8")
    assert first == second


def test_lock_fail_closed_leaves_no_partial_lock(tmp_path: Path) -> None:
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
    assert by_key[("cargo", "ripgrep")].profiles == ("a", "b")
    assert by_key[("python", "black")].profiles == ("b",)


@pytest.mark.parametrize("keep_python", [False, True])
def test_relock_prunes_only_obsolete_target_profile_memberships(
    tmp_path: Path, keep_python: bool
) -> None:
    text = (
        "tracked_files: {}\npackages:\n"
        "  native: {type: cargo, crate: tool}\n"
        "  py: {type: python, package: pytool}\n"
        "profiles:\n  a: {packages: [native, py]}\n  b: {packages: [native]}\n"
    )
    config = _write_config(tmp_path, text)
    _register_stub(PackageType.CARGO, "tool", "1.0")
    _register_stub(PackageType.PYTHON, "pytool", "1.0")
    runner = CliRunner()
    for profile in ("a", "b"):
        result = runner.invoke(
            app, ["lock", f"--profile={profile}", f"--config={config}"]
        )
        assert result.exit_code == 0, result.output
    path = tmp_path / "setforge.lock"
    previous = parse_lock(path.read_text())
    path.write_text(
        dump_lock(
            LockFile(
                packages=(
                    *previous.packages,
                    _pin(PackageType.CARGO, "legacy-unscoped", "1.0"),
                )
            )
        )
    )
    selection = "[py]" if keep_python else "[]"
    config.write_text(text.replace("[native, py]", selection))

    result = runner.invoke(app, ["lock", "--profile=a", f"--config={config}"])

    assert result.exit_code == 0, result.output
    memberships = {
        pin.key: pin.profiles for pin in parse_lock(path.read_text()).packages
    }
    expected = {"tool": ("b",), "legacy-unscoped": ()}
    if keep_python:
        expected["pytool"] = ("a",)
    assert memberships == expected


def test_lock_version_conflict_across_profiles_errors(tmp_path: Path) -> None:
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

    registry._REGISTRY.clear()
    _register_stub(PackageType.CARGO, "ripgrep", "99.0.0")
    result = CliRunner().invoke(app, ["lock", "--profile=b", f"--config={cfg}"])
    assert result.exit_code != 0
    message = str(result.exception)
    assert "14.0.0" in message
    assert "99.0.0" in message
    assert (tmp_path / "setforge.lock").read_text(encoding="utf-8") == before


def test_lock_update_reresolves_only_named_package(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    _register_full_stubs()
    CliRunner().invoke(app, ["lock", "--profile=p", f"--config={cfg}"])

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


@pytest.mark.parametrize("selector", ["Some__Tool", "some.tool", "some-tool"])
def test_python_lock_update_accepts_normalized_aliases(
    tmp_path: Path, selector: str
) -> None:
    config = _write_config(
        tmp_path,
        "tracked_files: {}\npackages:\n"
        "  tool: {type: python, package: Some__Tool}\n"
        "profiles:\n  p: {packages: [tool]}\n",
    )
    _register_stub(PackageType.PYTHON, "Some__Tool", "1.0")
    runner = CliRunner()
    initial = runner.invoke(app, ["lock", "--profile=p", f"--config={config}"])
    assert initial.exit_code == 0, initial.output
    registry._REGISTRY.clear()
    _register_stub(PackageType.PYTHON, "Some__Tool", "2.0")

    updated = runner.invoke(
        app, ["lock", "--profile=p", f"--config={config}", f"--update={selector}"]
    )

    assert updated.exit_code == 0, updated.output
    lock = parse_lock((tmp_path / "setforge.lock").read_text())
    assert [(pin.key, pin.version) for pin in lock.packages] == [("some-tool", "2.0")]


@pytest.mark.parametrize("selector", ["tool", "cargo:tool", "python:Tool"])
def test_lock_update_requires_qualified_colliding_identity(
    tmp_path: Path, selector: str
) -> None:
    config = _write_config(
        tmp_path,
        "tracked_files: {}\npackages:\n"
        "  native: {type: cargo, crate: tool}\n"
        "  py: {type: python, package: tool}\n"
        "profiles:\n  p: {packages: [native, py]}\n",
    )
    for kind in (PackageType.CARGO, PackageType.PYTHON):
        _register_stub(kind, "tool", "1.0")
    runner = CliRunner()
    initial = runner.invoke(app, ["lock", "--profile=p", f"--config={config}"])
    assert initial.exit_code == 0, initial.output
    lock_path = tmp_path / "setforge.lock"
    before = lock_path.read_bytes()
    registry._REGISTRY.clear()
    for kind in (PackageType.CARGO, PackageType.PYTHON):
        _register_stub(kind, "tool", "2.0", raises=selector == "tool")

    updated = runner.invoke(
        app, ["lock", "--profile=p", f"--config={config}", f"--update={selector}"]
    )

    if selector == "tool":
        assert updated.exit_code != 0
        assert "ambiguous" in str(updated.exception)
        assert "cargo:tool" in str(updated.exception)
        assert "python:tool" in str(updated.exception)
        assert lock_path.read_bytes() == before
    else:
        assert updated.exit_code == 0, updated.output
        versions = {
            pin.type.value: pin.version
            for pin in parse_lock(lock_path.read_text()).packages
        }
        selected_type = selector.partition(":")[0]
        assert versions == {
            kind: "2.0" if kind == selected_type else "1.0"
            for kind in ("cargo", "python")
        }


def test_lock_update_without_existing_lock_errors(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    _register_full_stubs()
    result = CliRunner().invoke(
        app, ["lock", "--profile=p", "--update=ripgrep", f"--config={cfg}"]
    )
    assert result.exit_code != 0
    assert not (tmp_path / "setforge.lock").exists()


def test_lock_update_skips_unrelated_broken_package(tmp_path: Path) -> None:
    # black raises but is enumerated BEFORE ripgrep; --update must filter by key first.
    yaml = """\
version: 1
tracked_files:
  d: {src: tracked_file.txt, dst: ~/.some-tracked_file}
packages:
  black: {type: python, package: black}
  ripgrep: {type: cargo, crate: ripgrep}
profiles:
  p:
    packages: [black, ripgrep]
"""
    cfg = _write_config(tmp_path, yaml)
    _register_stub(PackageType.PYTHON, "black", "24.1.0")
    _register_stub(PackageType.CARGO, "ripgrep", "14.0.0")
    CliRunner().invoke(app, ["lock", "--profile=p", f"--config={cfg}"])

    registry._REGISTRY.clear()
    _register_stub(PackageType.PYTHON, "black", "0", raises=True)
    _register_stub(PackageType.CARGO, "ripgrep", "15.0.0")
    result = CliRunner().invoke(
        app, ["lock", "--profile=p", "--update=ripgrep", f"--config={cfg}"]
    )
    assert result.exit_code == 0, result.output
    lock = parse_lock((tmp_path / "setforge.lock").read_text(encoding="utf-8"))
    by_key = {p.key: p for p in lock.packages}
    assert by_key["ripgrep"].version == "15.0.0"
    assert by_key["black"].version == "24.1.0", "broken earlier package untouched"


def test_merge_lock_same_version_different_integrity_conflicts() -> None:
    from setforge.cli.lock import merge_lock
    from setforge.errors import LockConflict
    from setforge.lockfile import LockFile

    def _cargo_pin(integrity: str, profile: str) -> ResolvedPin:
        return ResolvedPin(
            type=PackageType.CARGO,
            key="ripgrep",
            version="14.0.0",
            integrity=integrity,
            integrity_kind=IntegrityKind.CHECKSUM,
            profiles=(profile,),
        )

    existing = LockFile(packages=(_cargo_pin("sha256:aaaa", "a"),))
    with pytest.raises(LockConflict) as exc:
        merge_lock(existing, [_cargo_pin("sha256:bbbb", "b")], profile="b")
    assert "sha256:aaaa" in str(exc.value)
    assert "sha256:bbbb" in str(exc.value)


def test_lock_help_lists_flags(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["lock", "--help"])
    assert result.exit_code == 0
    assert "--profile" in result.output
    assert "--update" in result.output
