"""The 6.2 ↔ 6.3 platform release-assets contract stamp."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ruamel.yaml.comments import CommentedMap

from setforge.errors import ConfigError
from setforge.migrations._yaml_ops import atomic_write_yaml, yaml_rt

if TYPE_CHECKING:
    from setforge.migrations import ManifestEntry, MigrationRoots


def _load(path: Path) -> CommentedMap:
    yaml = yaml_rt()
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.load(handle)
    if not isinstance(data, CommentedMap):
        raise ConfigError(f"setforge.yaml root must be a mapping: {path}")
    return data


def _uses_platform_assets(data: CommentedMap) -> bool:
    packages = data.get("packages")
    if isinstance(packages, Mapping) and any(
        isinstance(package, Mapping) and "assets" in package
        for package in packages.values()
    ):
        return True
    bundles = data.get("bundles")
    if not isinstance(bundles, Mapping):
        return False
    for bundle in bundles.values():
        if not isinstance(bundle, Mapping):
            continue
        components = bundle.get("components")
        if not isinstance(components, Sequence) or isinstance(components, (str, bytes)):
            continue
        if any(
            isinstance(component, Mapping)
            and isinstance(component.get("github_release"), Mapping)
            and "assets" in component["github_release"]
            for component in components
        ):
            return True
    return False


@dataclass(slots=True, frozen=True)
class PlatformReleaseAssetsMigration:
    from_version: str = "6.2"
    to_version: str = "6.3"

    @property
    def reverse(self) -> PlatformReleaseAssetsReverse:
        return PlatformReleaseAssetsReverse()

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        from setforge.migrations import ManifestEntry, ManifestType

        return (
            ManifestEntry(
                type=ManifestType.EDIT,
                description="enable platform-qualified release assets",
                affected_path=roots.cfg_path,
            ),
        )

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        return (roots.cfg_path,)

    def apply(self, *, roots: MigrationRoots) -> None:
        data = _load(roots.cfg_path)
        data["schema_version"] = self.to_version
        atomic_write_yaml(roots.cfg_path, data)


@dataclass(slots=True, frozen=True)
class PlatformReleaseAssetsReverse:
    from_version: str = "6.3"
    to_version: str = "6.2"

    @property
    def reverse(self) -> PlatformReleaseAssetsMigration:
        return PlatformReleaseAssetsMigration()

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        from setforge.migrations import ManifestEntry, ManifestType

        return (
            ManifestEntry(
                type=ManifestType.EDIT,
                description="disable platform release assets when none are declared",
                affected_path=roots.cfg_path,
            ),
        )

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        return (roots.cfg_path,)

    def apply(self, *, roots: MigrationRoots) -> None:
        data = _load(roots.cfg_path)
        if _uses_platform_assets(data):
            raise ConfigError(
                "cannot downgrade schema 6.3 while platform release assets are "
                "declared; remove them before downgrading"
            )
        data["schema_version"] = self.to_version
        minimum = data.get("minimum_version")
        if minimum is not None:
            from setforge.migrations import _meets_floor

            if _meets_floor(str(minimum), "6.3"):
                data["minimum_version"] = "6.2"
        atomic_write_yaml(roots.cfg_path, data)
