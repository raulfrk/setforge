from pathlib import Path

import pytest
from ruamel.yaml import YAML

from setforge.errors import ConfigError
from setforge.migrations import MigrationRoots
from setforge.migrations._platform_release_assets import PlatformReleaseAssetsMigration


def _roots(tmp_path: Path, content: str) -> MigrationRoots:
    config = tmp_path / "setforge.yaml"
    config.write_text(content, encoding="utf-8")
    return MigrationRoots(config, tmp_path, tmp_path)


def _data(path: Path) -> dict[str, object]:
    return YAML(typ="safe").load(path.read_text(encoding="utf-8"))


def test_platform_assets_stamp_round_trip_without_variant_intent(
    tmp_path: Path,
) -> None:
    roots = _roots(
        tmp_path,
        "schema_version: '6.2'\nminimum_version: '6.2'\npackages: {}\n",
    )
    migration = PlatformReleaseAssetsMigration()
    migration.apply(roots=roots)
    assert _data(roots.cfg_path)["schema_version"] == "6.3"
    migration.reverse.apply(roots=roots)
    assert _data(roots.cfg_path) == {
        "schema_version": "6.2",
        "minimum_version": "6.2",
        "packages": {},
    }


@pytest.mark.parametrize(
    "body",
    [
        "packages:\n"
        "  tool:\n"
        "    type: github_release\n"
        "    assets: [{asset: tool.tgz}]\n",
        "bundles:\n"
        "  tools:\n"
        "    components:\n"
        "      - id: tool\n"
        "        github_release:\n"
        "          assets: [{asset: tool.tgz}]\n",
    ],
)
def test_platform_assets_reverse_refuses_variant_intent(
    tmp_path: Path, body: str
) -> None:
    roots = _roots(
        tmp_path,
        "schema_version: '6.3'\nminimum_version: '6.3'\n" + body,
    )
    with pytest.raises(ConfigError, match=r"cannot downgrade schema 6\.3"):
        PlatformReleaseAssetsMigration().reverse.apply(roots=roots)
    assert _data(roots.cfg_path)["schema_version"] == "6.3"


def test_reverse_ignores_unrelated_nested_assets_key(tmp_path: Path) -> None:
    roots = _roots(
        tmp_path,
        "schema_version: '6.3'\nminimum_version: '6.3'\n"
        "bundles:\n"
        "  files:\n"
        "    components:\n"
        "      - id: config\n"
        "        file:\n"
        "          src: assets\n"
        "          dst: ~/.config/assets\n",
    )
    PlatformReleaseAssetsMigration().reverse.apply(roots=roots)
    assert _data(roots.cfg_path)["schema_version"] == "6.2"
