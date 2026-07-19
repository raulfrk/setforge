"""Tests for the ``packages:`` / ``bundles:`` config surfaces."""

from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from setforge import reconcile_adapter
from setforge.config import (
    BundleComponent,
    BundleSpec,
    CargoPackage,
    Config,
    GitHubReleasePackage,
    GoPackage,
    LocalPackage,
    Package,
    PackageKind,
    Profile,
    PythonPackage,
    ResolvedProfile,
    TrackedFile,
    load_config,
    resolve_profile,
)

_PKG_ADAPTER: TypeAdapter[object] = TypeAdapter(Package)


def _cfg(profiles: dict[str, Profile], **kw: object) -> Config:
    return Config(
        tracked_files={"d": TrackedFile(src=Path("a"), dst="b")},
        profiles=profiles,
        **kw,  # type: ignore[arg-type]
    )


# --- per-type model validation ---------------------------------------------


def test_cargo_package_validates() -> None:
    pkg = CargoPackage(crate="ripgrep")
    assert pkg.type is PackageKind.CARGO
    assert pkg.crate == "ripgrep"


def test_python_package_validates_with_optional_version() -> None:
    pkg = PythonPackage(package="black")
    assert pkg.type is PackageKind.PYTHON
    assert pkg.version is None
    pinned = PythonPackage(package="black", version="24.1.0")
    assert pinned.version == "24.1.0"


def test_go_package_validates() -> None:
    pkg = GoPackage(module="golang.org/x/tools/gopls")
    assert pkg.type is PackageKind.GO
    assert pkg.version is None


def test_github_release_package_defaults() -> None:
    pkg = GitHubReleasePackage(
        repo="sharkdp/fd",
        tag="v10.1.0",
        asset="fd-v10.1.0-x86_64.tar.gz",
        binary="fd",
        install="~/.local/bin/fd",
    )
    assert pkg.type is PackageKind.GITHUB_RELEASE
    assert pkg.extract is True
    assert pkg.chmod == "+x"
    assert pkg.checksum is None
    assert pkg.rename is None


def test_local_package_defaults() -> None:
    pkg = LocalPackage(
        path="blobs/mytool.tar.gz",
        binary="mytool",
        install="~/.local/bin/mytool",
    )
    assert pkg.type is PackageKind.LOCAL
    assert pkg.extract is True
    assert pkg.chmod == "+x"


def test_package_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        CargoPackage(crate="x", bogus="y")  # type: ignore[call-arg]


# --- discriminated-union dispatch ------------------------------------------


def test_union_dispatches_on_type_cargo() -> None:
    pkg = _PKG_ADAPTER.validate_python({"type": "cargo", "crate": "ripgrep"})
    assert isinstance(pkg, CargoPackage)
    assert pkg.crate == "ripgrep"


def test_union_dispatches_on_type_python() -> None:
    pkg = _PKG_ADAPTER.validate_python(
        {"type": "python", "package": "black", "version": "24.1.0"}
    )
    assert isinstance(pkg, PythonPackage)


def test_union_dispatches_on_type_github_release() -> None:
    pkg = _PKG_ADAPTER.validate_python(
        {
            "type": "github_release",
            "repo": "sharkdp/fd",
            "tag": "v10",
            "asset": "fd.tar.gz",
            "binary": "fd",
            "install": "~/.local/bin/fd",
        }
    )
    assert isinstance(pkg, GitHubReleasePackage)


def test_union_rejects_bad_type() -> None:
    with pytest.raises(ValidationError):
        _PKG_ADAPTER.validate_python({"type": "npm", "crate": "x"})


# --- binary/rename path-traversal defense (layer 1) ------------------------


@pytest.mark.parametrize("bad", ["../x", "a/b", "..", "/abs"])
def test_github_release_rejects_bad_binary(bad: str) -> None:
    with pytest.raises(ValidationError):
        GitHubReleasePackage(
            repo="o/r", tag="v1", asset="a.tgz", binary=bad, install="~/bin/x"
        )


@pytest.mark.parametrize("bad", ["../x", "a/b"])
def test_github_release_rejects_bad_rename(bad: str) -> None:
    with pytest.raises(ValidationError):
        GitHubReleasePackage(
            repo="o/r",
            tag="v1",
            asset="a.tgz",
            binary="fd",
            install="~/bin/x",
            rename=bad,
        )


def test_local_rejects_bad_binary() -> None:
    with pytest.raises(ValidationError):
        LocalPackage(path="p.tgz", binary="../evil", install="~/bin/x")


@pytest.mark.parametrize("bad", ["", ".", "   "])
def test_github_release_rejects_degenerate_binary(bad: str) -> None:
    with pytest.raises(ValidationError):
        GitHubReleasePackage(
            repo="o/r", tag="v1", asset="a.tgz", binary=bad, install="~/bin/x"
        )


@pytest.mark.parametrize("bad", ["", ".", "   "])
def test_github_release_rejects_degenerate_rename(bad: str) -> None:
    with pytest.raises(ValidationError):
        GitHubReleasePackage(
            repo="o/r",
            tag="v1",
            asset="a.tgz",
            binary="fd",
            install="~/bin/x",
            rename=bad,
        )


@pytest.mark.parametrize("bad", ["", ".", "   "])
def test_local_rejects_degenerate_binary(bad: str) -> None:
    with pytest.raises(ValidationError):
        LocalPackage(path="p.tgz", binary=bad, install="~/bin/x")


@pytest.mark.parametrize("bad", ["", ".", "   "])
def test_local_rejects_degenerate_rename(bad: str) -> None:
    with pytest.raises(ValidationError):
        LocalPackage(path="p.tgz", binary="mytool", install="~/bin/x", rename=bad)


# --- chmod file-mode security policy (mirrors tracked_files mode) -----------


@pytest.mark.parametrize("bad", ["4755", "2755", "6755", "07777"])
def test_github_release_rejects_setuid_setgid_chmod(bad: str) -> None:
    """setuid/setgid chmod is refused at config load — symmetric with mode."""
    with pytest.raises(ValidationError, match="setuid/setgid"):
        GitHubReleasePackage(
            repo="o/r",
            tag="v1",
            asset="a.tgz",
            binary="fd",
            install="~/bin/x",
            chmod=bad,
        )


@pytest.mark.parametrize("bad", ["4755", "2755", "07777"])
def test_local_rejects_setuid_setgid_chmod(bad: str) -> None:
    with pytest.raises(ValidationError, match="setuid/setgid"):
        LocalPackage(path="p.tgz", binary="mytool", install="~/bin/x", chmod=bad)


def test_github_release_rejects_out_of_range_chmod() -> None:
    """A chmod above 0o7777 is refused for range, matching mode policy."""
    with pytest.raises(ValidationError, match="out of range"):
        GitHubReleasePackage(
            repo="o/r",
            tag="v1",
            asset="a.tgz",
            binary="fd",
            install="~/bin/x",
            chmod="17777",
        )


@pytest.mark.parametrize("good", ["+x", "0755", "0644", "755", "1755"])
def test_package_accepts_safe_chmod(good: str) -> None:
    """'+x', plain octals, and the sticky bit (0o1000) stay valid."""
    gh = GitHubReleasePackage(
        repo="o/r",
        tag="v1",
        asset="a.tgz",
        binary="fd",
        install="~/bin/x",
        chmod=good,
    )
    lo = LocalPackage(path="p.tgz", binary="mytool", install="~/bin/x", chmod=good)
    assert gh.chmod == good
    assert lo.chmod == good


def test_bare_binary_name_accepted() -> None:
    pkg = LocalPackage(path="p.tgz", binary="my-tool", install="~/bin/x")
    assert pkg.binary == "my-tool"


# --- Config.version file-format guard --------------------------------------


def test_config_version_default_accepted() -> None:
    """The default file-format version (1) is the one this engine understands."""
    assert _cfg({"only": Profile()}).version == 1


@pytest.mark.parametrize("bad", [0, 2, 99])
def test_config_rejects_unknown_file_format_version(bad: int) -> None:
    """Any file-format version other than 1 is refused, not silently loaded."""
    with pytest.raises(ValidationError, match="file-format version"):
        _cfg({"only": Profile()}, version=bad)


# --- Config-level packages registry ----------------------------------------


def test_config_packages_registry_discriminates() -> None:
    cfg = _cfg(
        {"only": Profile()},
        packages={
            "rg": {"type": "cargo", "crate": "ripgrep"},
            "black": {"type": "python", "package": "black"},
        },
    )
    assert isinstance(cfg.packages["rg"], CargoPackage)
    assert isinstance(cfg.packages["black"], PythonPackage)


# --- bundles ---------------------------------------------------------------


def test_bundle_component_reference_form() -> None:
    comp = BundleComponent(id="c1", package="rg")
    assert comp.package == "rg"
    assert comp.depends_on == []


def test_bundle_component_inline_cargo() -> None:
    comp = BundleComponent(id="c1", cargo=CargoPackage(crate="ripgrep"))
    assert comp.cargo is not None
    assert comp.package is None


def test_bundle_component_inline_plugin_string() -> None:
    comp = BundleComponent(id="c1", plugin="serena@official")
    assert comp.plugin == "serena@official"


def test_bundle_component_rejects_both_package_and_inline() -> None:
    with pytest.raises(ValidationError):
        BundleComponent(id="c1", package="rg", cargo=CargoPackage(crate="ripgrep"))


def test_bundle_component_rejects_neither() -> None:
    with pytest.raises(ValidationError):
        BundleComponent(id="c1")


def test_bundle_component_requires_id() -> None:
    with pytest.raises(ValidationError):
        BundleComponent(package="rg")  # type: ignore[call-arg]


def test_bundle_spec_holds_components() -> None:
    spec = BundleSpec(
        components=[
            BundleComponent(id="a", package="rg"),
            BundleComponent(id="b", cargo=CargoPackage(crate="fd"), depends_on=["a"]),
        ]
    )
    assert len(spec.components) == 2
    assert spec.components[1].depends_on == ["a"]


def test_config_bundles_registry() -> None:
    cfg = _cfg(
        {"only": Profile()},
        bundles={
            "dev": {
                "components": [
                    {"id": "a", "package": "rg"},
                    {"id": "b", "cargo": {"crate": "fd"}},
                ]
            }
        },
    )
    assert isinstance(cfg.bundles["dev"], BundleSpec)
    assert isinstance(cfg.bundles["dev"].components[1].cargo, CargoPackage)


# --- profile opt-in + merge ------------------------------------------------


def test_profile_packages_bundles_default_empty() -> None:
    p = Profile()
    assert p.packages == []
    assert p.bundles == []


def test_resolved_profile_packages_bundles_default_empty() -> None:
    r = ResolvedProfile()
    assert r.packages == []
    assert r.bundles == []


def test_resolve_merges_packages_and_bundles() -> None:
    cfg = _cfg(
        {
            "parent": Profile(packages=["a", "b"], bundles=["x"]),
            "child": Profile(extends="parent", packages=["b", "c"], bundles=["y"]),
        }
    )
    resolved = resolve_profile(cfg, "child")
    assert resolved.packages == ["a", "b", "c"]
    assert resolved.bundles == ["x", "y"]


# --- cargo crates resolve to provision items via the packages surface ------


def test_cargo_crates_still_resolve() -> None:
    """Cargo crates now flow through ``packages`` CargoPackage refs; the
    parent/child merge preserves parent-first order with dedup, and the
    adapter projects the refs back to bare crate names."""
    cfg = _cfg(
        {
            "parent": Profile(packages=["ast_grep"]),
            "child": Profile(extends="parent", packages=["ripgrep"]),
        },
        packages={
            "ast_grep": {"type": "cargo", "crate": "ast-grep"},
            "ripgrep": {"type": "cargo", "crate": "ripgrep"},
        },
    )
    resolved = resolve_profile(cfg, "child")
    assert resolved.packages == ["ast_grep", "ripgrep"]
    assert reconcile_adapter.cargo_crates(cfg, resolved) == ["ast-grep", "ripgrep"]


# --- validate accepts a config carrying the new fields ---------------------

_CONFIG_WITH_PACKAGES = """\
schema_version: '5.0'
version: 1

tracked_files:
  claude_md:
    src: claude/CLAUDE.md
    dst: ~/.claude/CLAUDE.md

packages:
  rg:
    type: cargo
    crate: ripgrep
  fd:
    type: github_release
    repo: sharkdp/fd
    tag: v10.1.0
    asset: fd.tar.gz
    binary: fd
    install: ~/.local/bin/fd

bundles:
  dev:
    components:
      - id: a
        package: rg
      - id: b
        cargo:
          crate: fd
        depends_on:
          - a

profiles:
  base:
    tracked_files:
      - claude_md
    packages:
      - rg
    bundles:
      - dev
"""


def test_validate_accepts_config_with_new_fields(tmp_path: Path) -> None:
    """The strict ``validate`` load path (tolerate_unknown=False) accepts
    a config declaring packages/bundles — install must accept what validate
    accepts."""
    cfg_path = tmp_path / "setforge.yaml"
    cfg_path.write_text(_CONFIG_WITH_PACKAGES)
    cfg = load_config(cfg_path, tolerate_unknown=False)
    assert isinstance(cfg.packages["rg"], CargoPackage)
    assert isinstance(cfg.packages["fd"], GitHubReleasePackage)
    assert isinstance(cfg.bundles["dev"], BundleSpec)
    resolved = resolve_profile(cfg, "base")
    assert resolved.packages == ["rg"]
    assert resolved.bundles == ["dev"]
