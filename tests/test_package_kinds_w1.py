"""Tests for the W1 additive ``plugin``/``extension`` package kinds."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from setforge.config import (
    Config,
    ExtensionPackage,
    PackageKind,
    PluginPackage,
    Profile,
    TrackedFile,
)


def _cfg(profiles: dict[str, Profile], **kw: object) -> Config:
    return Config(
        tracked_files={"d": TrackedFile(src=Path("a"), dst="b")},
        profiles=profiles,
        **kw,  # type: ignore[arg-type]
    )


def test_plugin_and_extension_packages_validate_and_discriminate() -> None:
    cfg = _cfg(
        {"only": Profile()},
        packages={
            "p": {"type": "plugin", "plugin": "p"},
            "e": {"type": "extension", "extension": "e"},
        },
    )
    assert isinstance(cfg.packages["p"], PluginPackage)
    assert cfg.packages["p"].type is PackageKind.PLUGIN
    assert cfg.packages["p"].plugin == "p"
    assert isinstance(cfg.packages["e"], ExtensionPackage)
    assert cfg.packages["e"].type is PackageKind.EXTENSION
    assert cfg.packages["e"].extension == "e"


def test_plugin_package_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        PluginPackage(plugin="p", bogus="y")  # type: ignore[call-arg]


def test_extension_package_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        ExtensionPackage(extension="e", bogus="y")  # type: ignore[call-arg]


def test_config_rejects_plugin_package_with_extra_key() -> None:
    with pytest.raises(ValidationError):
        _cfg(
            {"only": Profile()},
            packages={"p": {"type": "plugin", "plugin": "p", "bogus": "y"}},
        )
