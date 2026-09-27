"""The 6.1 ↔ 6.2 managed-directory-tree contract stamp."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from setforge.errors import ConfigError
from setforge.migrations._yaml_ops import (
    _has_tracked_file_field,
    atomic_write_yaml,
    load_yaml_mapping,
)

if TYPE_CHECKING:
    from setforge.migrations import ManifestEntry, MigrationRoots


@dataclass(slots=True, frozen=True)
class DirectoryTreesMigration:
    """Restamp 6.1 as 6.2 without changing existing portable intent."""

    from_version: str = "6.1"
    to_version: str = "6.2"

    @property
    def reverse(self) -> DirectoryTreesReverse:
        return DirectoryTreesReverse()

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        from setforge.migrations import ManifestEntry, ManifestType

        return (
            ManifestEntry(
                type=ManifestType.EDIT,
                description="enable managed directory trees",
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
class DirectoryTreesReverse:
    """Restamp 6.2 as 6.1 only when no tree intent would be lost."""

    from_version: str = "6.2"
    to_version: str = "6.1"

    @property
    def reverse(self) -> DirectoryTreesMigration:
        return DirectoryTreesMigration()

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        from setforge.migrations import ManifestEntry, ManifestType

        return (
            ManifestEntry(
                type=ManifestType.EDIT,
                description="disable managed trees when none are declared",
                affected_path=roots.cfg_path,
            ),
        )

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        return (roots.cfg_path,)

    def apply(self, *, roots: MigrationRoots) -> None:
        data = load_yaml_mapping(roots.cfg_path)
        if _has_tracked_file_field(data, "tree"):
            raise ConfigError(
                "cannot downgrade schema 6.2 while managed tree intent is declared; "
                "remove it before downgrading"
            )
        data["schema_version"] = self.to_version
        minimum = data.get("minimum_version")
        if minimum is not None:
            from setforge.migrations import _meets_floor

            if _meets_floor(str(minimum), "6.2"):
                data["minimum_version"] = "6.1"
        atomic_write_yaml(roots.cfg_path, data)
