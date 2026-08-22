from pathlib import Path

import pytest
from ruamel.yaml import YAML

from setforge.errors import ConfigError
from setforge.migrations import MigrationRoots
from setforge.migrations._directory_trees import DirectoryTreesMigration


def _roots(tmp_path: Path, content: str) -> MigrationRoots:
    config = tmp_path / "setforge.yaml"
    config.write_text(content, encoding="utf-8")
    return MigrationRoots(config, tmp_path, tmp_path)


def _data(path: Path) -> dict[str, object]:
    return YAML(typ="safe").load(path.read_text(encoding="utf-8"))


def test_directory_tree_stamp_round_trip_without_tree_intent(tmp_path: Path) -> None:
    roots = _roots(
        tmp_path,
        "schema_version: '6.1'\nminimum_version: '6.1'\ntracked_files: {}\n",
    )
    migration = DirectoryTreesMigration()
    migration.apply(roots=roots)
    assert _data(roots.cfg_path)["schema_version"] == "6.2"

    migration.reverse.apply(roots=roots)
    assert _data(roots.cfg_path) == {
        "schema_version": "6.1",
        "minimum_version": "6.1",
        "tracked_files": {},
    }


def test_directory_tree_reverse_refuses_lossy_downgrade(tmp_path: Path) -> None:
    roots = _roots(
        tmp_path,
        "schema_version: '6.2'\n"
        "minimum_version: '6.2'\n"
        "tracked_files:\n"
        "  tools:\n"
        "    src: tools\n"
        "    dst: ~/.tools\n"
        "    tree: {}\n",
    )
    with pytest.raises(ConfigError, match=r"cannot downgrade schema 6\.2"):
        DirectoryTreesMigration().reverse.apply(roots=roots)
    assert _data(roots.cfg_path)["schema_version"] == "6.2"
