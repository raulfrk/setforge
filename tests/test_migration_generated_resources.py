from pathlib import Path

import pytest
from ruamel.yaml import YAML

from setforge.errors import ConfigError
from setforge.migrations import Migration, MigrationRoots
from setforge.migrations.registry import MIGRATIONS


def _step(from_version: str) -> Migration:
    return next(m for m in MIGRATIONS if m.from_version == from_version)


def _roots(tmp_path: Path, body: str) -> MigrationRoots:
    config = tmp_path / "setforge.yaml"
    config.write_text(body, encoding="utf-8")
    return MigrationRoots(config, tmp_path, tmp_path)


def _data(path: Path) -> dict[str, object]:
    return YAML(typ="safe").load(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "migration",
    [
        _step("6.0"),
        _step("6.1"),
        _step("6.2"),
        _step("6.3"),
        _step("6.4"),
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
    migration = _step("6.0")

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
        _step("6.0").reverse.apply(roots=roots)

    assert _data(roots.cfg_path)["schema_version"] == "6.1"


@pytest.mark.parametrize(
    "body",
    [
        "tracked_files:\n  generated: {src: config, dst: ~/config}\n",
        "tracked_files: {}\nbundles:\n  generated:\n    components:\n"
        "      - id: config\n        file: {src: config, dst: ~/config}\n",
    ],
)
def test_generated_reverse_preserves_unrelated_registry_names(
    tmp_path: Path, body: str
) -> None:
    roots = _roots(
        tmp_path,
        "schema_version: '6.1'\nminimum_version: '6.1'\nprofiles: {}\n" + body,
    )
    expected = _data(roots.cfg_path) | {
        "schema_version": "6.0",
        "minimum_version": "6.0",
    }

    _step("6.0").reverse.apply(roots=roots)

    assert _data(roots.cfg_path) == expected


@pytest.mark.parametrize("feature", ["generated", "tree"])
def test_reverse_refuses_feature_on_bundle_file(tmp_path: Path, feature: str) -> None:
    migration = _step("6.0") if feature == "generated" else _step("6.1")
    body = "{inputs: {home: home}}" if feature == "generated" else "{}"
    roots = _roots(
        tmp_path,
        f"schema_version: '{migration.to_version}'\ntracked_files: {{}}\n"
        "bundles:\n  configs:\n    components:\n      - id: config\n"
        "        file:\n          src: config\n          dst: ~/config\n"
        f"          {feature}: {body}\nprofiles: {{}}\n",
    )
    original = roots.cfg_path.read_bytes()

    with pytest.raises(ConfigError, match="cannot downgrade"):
        migration.reverse.apply(roots=roots)

    assert roots.cfg_path.read_bytes() == original
