"""The reversible 6.4 ↔ 6.5 Codex MCP scope contract stamp."""

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
class CodexMcpScopeMigration:
    from_version: str = "6.4"
    to_version: str = "6.5"

    @property
    def reverse(self) -> CodexMcpScopeReverse:
        return CodexMcpScopeReverse()

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        from setforge.migrations import ManifestEntry, ManifestType

        return (
            ManifestEntry(
                type=ManifestType.EDIT,
                description="enable project-scoped Codex MCP declarations",
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
class CodexMcpScopeReverse:
    from_version: str = "6.5"
    to_version: str = "6.4"

    @property
    def reverse(self) -> CodexMcpScopeMigration:
        return CodexMcpScopeMigration()

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        from setforge.migrations import ManifestEntry, ManifestType

        return (
            ManifestEntry(
                type=ManifestType.EDIT,
                description="disable project-scoped Codex MCP declarations",
                affected_path=roots.cfg_path,
            ),
        )

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        return (roots.cfg_path,)

    def apply(self, *, roots: MigrationRoots) -> None:
        data = _load(roots.cfg_path)
        codex = data.get("codex")
        servers = codex.get("mcp_servers") if isinstance(codex, dict) else None
        if isinstance(servers, dict) and any(
            isinstance(server, dict) and ("scope" in server or "project" in server)
            for server in servers.values()
        ):
            raise ConfigError(
                "cannot downgrade schema 6.5 while scoped Codex MCP "
                "declarations are present"
            )
        data["schema_version"] = self.to_version
        minimum = data.get("minimum_version")
        if minimum is not None:
            from setforge.migrations import _meets_floor

            if _meets_floor(str(minimum), self.from_version):
                data["minimum_version"] = self.to_version
        atomic_write_yaml(roots.cfg_path, data)
