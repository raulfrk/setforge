"""The reversible 6.3 ↔ 6.4 Codex configuration-contract stamp."""

from __future__ import annotations

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


@dataclass(slots=True, frozen=True)
class CodexContractMigration:
    from_version: str = "6.3"
    to_version: str = "6.4"

    @property
    def reverse(self) -> CodexContractReverse:
        return CodexContractReverse()

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        from setforge.migrations import ManifestEntry, ManifestType

        return (
            ManifestEntry(
                type=ManifestType.EDIT,
                description="enable product-aware Codex declarations",
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
class CodexContractReverse:
    from_version: str = "6.4"
    to_version: str = "6.3"

    @property
    def reverse(self) -> CodexContractMigration:
        return CodexContractMigration()

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        from setforge.migrations import ManifestEntry, ManifestType

        return (
            ManifestEntry(
                type=ManifestType.EDIT,
                description="disable the Codex contract when unused",
                affected_path=roots.cfg_path,
            ),
        )

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        return (roots.cfg_path,)

    def apply(self, *, roots: MigrationRoots) -> None:
        data = _load(roots.cfg_path)
        profiles = data.get("profiles")
        profile_uses_codex = isinstance(profiles, dict) and any(
            isinstance(profile, dict) and "codex" in profile
            for profile in profiles.values()
        )
        if "codex" in data or profile_uses_codex:
            raise ConfigError(
                "cannot downgrade schema 6.4 while Codex declarations are "
                "present; remove them before downgrading"
            )
        data["schema_version"] = self.to_version
        minimum = data.get("minimum_version")
        if minimum is not None:
            from setforge.migrations import _meets_floor

            if _meets_floor(str(minimum), self.from_version):
                data["minimum_version"] = self.to_version
        atomic_write_yaml(roots.cfg_path, data)
