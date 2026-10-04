from pathlib import Path

import pytest
from ruamel.yaml import YAML

from setforge.errors import ConfigError
from setforge.migrations import Migration, MigrationRoots
from setforge.migrations.registry import MIGRATIONS


def _step(from_version: str) -> Migration:
    return next(m for m in MIGRATIONS if m.from_version == from_version)


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
    migration = _step("6.1")
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
        _step("6.1").reverse.apply(roots=roots)
    assert _data(roots.cfg_path)["schema_version"] == "6.2"


@pytest.mark.parametrize(
    "body",
    [
        "tracked_files:\n  tree: {src: config, dst: ~/config}\n",
        "tracked_files: {}\nbundles:\n  tree:\n    components:\n"
        "      - id: config\n        file: {src: config, dst: ~/config}\n",
        "tracked_files:\n  config:\n    src: config.j2\n    dst: ~/config\n"
        "    generated: {inputs: {tree: home}}\n",
    ],
)
def test_tree_reverse_preserves_unrelated_names(tmp_path: Path, body: str) -> None:
    roots = _roots(
        tmp_path,
        "schema_version: '6.2'\nminimum_version: '6.2'\nprofiles: {}\n" + body,
    )
    expected = _data(roots.cfg_path) | {
        "schema_version": "6.1",
        "minimum_version": "6.1",
    }

    _step("6.1").reverse.apply(roots=roots)

    assert _data(roots.cfg_path) == expected
