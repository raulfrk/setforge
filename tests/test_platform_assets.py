from pathlib import Path

import pytest
from pydantic import ValidationError

from setforge.config import GitHubReleasePackage, PlatformAssetVariant, load_config
from setforge.errors import ConfigError
from setforge.platform_assets import (
    HostPlatform,
    current_host_platform,
    select_platform_asset,
)


def _variant(
    asset: str, os: str | None = None, arch: str | None = None
) -> PlatformAssetVariant:
    return PlatformAssetVariant(asset=asset, os=os, arch=arch)


def test_platform_aliases_are_normalized_at_validation() -> None:
    variant = _variant("tool.tgz", os="Darwin", arch="AMD64")
    assert (variant.os, variant.arch) == ("macos", "x86_64")


def test_current_host_platform_normalizes_platform_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("setforge.platform_assets.platform.system", lambda: "Darwin")
    monkeypatch.setattr("setforge.platform_assets.platform.machine", lambda: "arm64")
    host = current_host_platform()
    assert host == HostPlatform(os="macos", arch="aarch64")
    assert host.key == "macos-aarch64"


def test_current_host_platform_refuses_unknown_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("setforge.platform_assets.platform.system", lambda: "Windows")
    with pytest.raises(ConfigError, match="unsupported platform operating system"):
        current_host_platform()


@pytest.mark.parametrize(("field", "value"), [("os", "windows"), ("arch", "riscv64")])
def test_unknown_platform_tokens_are_rejected(field: str, value: str) -> None:
    with pytest.raises(ValidationError, match="unsupported platform"):
        PlatformAssetVariant.model_validate({"asset": "tool.tgz", field: value})


def test_legacy_and_variant_asset_forms_are_mutually_exclusive() -> None:
    legacy = GitHubReleasePackage(
        repo="o/r",
        tag="v1",
        asset="tool.tgz",
        binary="tool",
        install="~/bin/tool",
        checksum="sha256:abc",
    )
    assert legacy.asset == "tool.tgz"
    variants = (_variant("tool-linux.tgz", os="linux"),)
    assert (
        GitHubReleasePackage(
            repo="o/r", tag="v1", assets=variants, binary="tool", install="~/bin/tool"
        ).assets
        == variants
    )
    with pytest.raises(ValidationError, match="mutually exclusive"):
        GitHubReleasePackage(
            repo="o/r",
            tag="v1",
            asset="tool.tgz",
            assets=variants,
            binary="tool",
            install="~/bin/tool",
        )
    with pytest.raises(ValidationError, match="must not be empty"):
        GitHubReleasePackage(
            repo="o/r", tag="v1", assets=(), binary="tool", install="~/bin/tool"
        )
    with pytest.raises(ValidationError, match="requires asset or assets"):
        GitHubReleasePackage(repo="o/r", tag="v1", binary="tool", install="~/bin/tool")


def test_selector_uses_declared_specificity_order() -> None:
    variants = (
        _variant("fallback"),
        _variant("arch", arch="arm64"),
        _variant("os", os="linux"),
        _variant("exact", os="linux", arch="aarch64"),
    )
    assert select_platform_asset(variants, os="linux", arch="arm64").asset == "exact"
    assert select_platform_asset(variants[:-1], os="linux", arch="arm64").asset == "os"
    assert select_platform_asset(variants[:2], os="linux", arch="arm64").asset == "arch"
    fallback = select_platform_asset(variants[:1], os="linux", arch="arm64")
    assert fallback.asset == "fallback"


def test_selector_refuses_ambiguous_or_missing_match() -> None:
    with pytest.raises(ConfigError, match="ambiguous"):
        select_platform_asset((_variant("a"), _variant("b")), os="linux", arch="amd64")
    with pytest.raises(ConfigError, match="no release asset"):
        select_platform_asset((_variant("mac", os="macos"),), os="linux", arch="amd64")


def test_variant_assets_require_schema_63_reader_floor(tmp_path: Path) -> None:
    config = tmp_path / "setforge.yaml"
    config.write_text(
        "schema_version: '6.3'\n"
        "minimum_version: '6.2'\n"
        "tracked_files: {}\n"
        "profiles: {}\n"
        "packages:\n"
        "  tool:\n"
        "    type: github_release\n"
        "    repo: o/r\n"
        "    tag: v1\n"
        "    assets: [{asset: tool.tgz}]\n"
        "    binary: tool\n"
        "    install: ~/bin/tool\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match=r"minimum_version >= '6\.3'"):
        load_config(config)
