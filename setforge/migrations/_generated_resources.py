"""The 6.0 ↔ 6.1 generated-resource contract stamp."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ruamel.yaml.comments import CommentedMap

from setforge.errors import ConfigError
from setforge.migrations._yaml_ops import atomic_write_yaml, load_yaml_mapping

if TYPE_CHECKING:
    from setforge.migrations import ManifestEntry, MigrationRoots


def _uses_generated(data: CommentedMap) -> bool:
    def contains(value: object) -> bool:
        if isinstance(value, Mapping):
            return "generated" in value or any(
                contains(item) for item in value.values()
            )
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return any(contains(item) for item in value)
        return False

    return contains(data.get("tracked_files")) or contains(data.get("bundles"))


@dataclass(slots=True, frozen=True)
class GeneratedResourcesMigration:
    """Restamp 6.0 as 6.1 without changing existing portable intent."""

    from_version: str = "6.0"
    to_version: str = "6.1"

    @property
    def reverse(self) -> GeneratedResourcesReverse:
        return GeneratedResourcesReverse()

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        from setforge.migrations import ManifestEntry, ManifestType

        return (
            ManifestEntry(
                type=ManifestType.EDIT,
                description="enable typed generated tracked-file resources",
                affected_path=roots.cfg_path,
            ),
        )

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        return (roots.cfg_path,)

    def apply(self, *, roots: MigrationRoots) -> None:
        data = load_yaml_mapping(roots.cfg_path)
        data["schema_version"] = self.to_version
        atomic_write_yaml(roots.cfg_path, data)


@dataclass(slots=True, frozen=True)
class GeneratedResourcesReverse:
    """Restamp 6.1 as 6.0 only when no generated intent would be lost."""

    from_version: str = "6.1"
    to_version: str = "6.0"

    @property
    def reverse(self) -> GeneratedResourcesMigration:
        return GeneratedResourcesMigration()

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        from setforge.migrations import ManifestEntry, ManifestType

        return (
            ManifestEntry(
                type=ManifestType.EDIT,
                description="disable generated resources when none are declared",
                affected_path=roots.cfg_path,
            ),
        )

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        return (roots.cfg_path,)

    def apply(self, *, roots: MigrationRoots) -> None:
        data = load_yaml_mapping(roots.cfg_path)
        if _uses_generated(data):
            raise ConfigError(
                "cannot downgrade schema 6.1 while generated tracked-file "
                "intent is declared; remove it before downgrading"
            )
        data["schema_version"] = self.to_version
        minimum = data.get("minimum_version")
        if minimum is not None:
            from setforge.migrations import _meets_floor

            if _meets_floor(str(minimum), "6.1"):
                data["minimum_version"] = "6.0"
        atomic_write_yaml(roots.cfg_path, data)
