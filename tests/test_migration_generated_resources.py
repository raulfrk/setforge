from pathlib import Path

import pytest
from ruamel.yaml import YAML

from setforge.errors import ConfigError
from setforge.migrations import Migration, MigrationRoots
from setforge.migrations._codex_contract import CodexContractMigration
from setforge.migrations._codex_mcp_scope import CodexMcpScopeMigration
from setforge.migrations._directory_trees import DirectoryTreesMigration
from setforge.migrations._generated_resources import GeneratedResourcesMigration
from setforge.migrations._platform_release_assets import PlatformReleaseAssetsMigration


def _roots(tmp_path: Path, body: str) -> MigrationRoots:
    config = tmp_path / "setforge.yaml"
    config.write_text(body, encoding="utf-8")
    return MigrationRoots(config, tmp_path, tmp_path)


def _data(path: Path) -> dict[str, object]:
    return YAML(typ="safe").load(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "migration",
    [
        GeneratedResourcesMigration(),
        DirectoryTreesMigration(),
        PlatformReleaseAssetsMigration(),
        CodexContractMigration(),
        CodexMcpScopeMigration(),
    ],
)
def test_contract_stamp_migrations_preserve_mapping_root_diagnostic(
    tmp_path: Path, migration: Migration
) -> None:
    roots = _roots(tmp_path, "[]\n")

    with pytest.raises(ConfigError) as exc_info:
        migration.apply(roots=roots)

    assert str(exc_info.value) == (
        f"setforge.yaml root must be a mapping: {roots.cfg_path}"
    )


def test_generated_resources_stamp_round_trip_without_generated_intent(
    tmp_path: Path,
) -> None:
    roots = _roots(
        tmp_path,
        "schema_version: '6.0'\nminimum_version: '6.0'\nprofiles: {}\n",
    )
    migration = GeneratedResourcesMigration()

    migration.apply(roots=roots)
    assert _data(roots.cfg_path)["schema_version"] == "6.1"
    migration.reverse.apply(roots=roots)

    data = _data(roots.cfg_path)
    assert data["schema_version"] == "6.0"
    assert data["minimum_version"] == "6.0"


def test_generated_resources_reverse_refuses_lossy_downgrade(tmp_path: Path) -> None:
    roots = _roots(
        tmp_path,
        "schema_version: '6.1'\n"
        "minimum_version: '6.1'\n"
        "tracked_files:\n"
        "  x:\n"
        "    src: x.j2\n"
        "    dst: ~/x\n"
        "    generated:\n"
        "      inputs: {home: home}\n"
        "profiles: {}\n",
    )

    with pytest.raises(ConfigError, match="cannot downgrade"):
        GeneratedResourcesMigration().reverse.apply(roots=roots)

    assert _data(roots.cfg_path)["schema_version"] == "6.1"
