"""Regression: cross-major downgrade BELOW 1.2 must not leave a locking floor.

COMPATIBILITY.md guarantees a one-command downgrade across a major: the reverse
chain rewrites the config down to the target schema and the result must LOAD on
the older target engine. The 2.0 -> 1.x reverse is the only chain step that
touches ``minimum_version``; the deeper same-major reverses (1.2 -> 1.1 -> 1.0)
re-stamp ``schema_version`` but never the floor. The prior fix lowered the stale
contract floor only to the reverse's OWN ``to_version`` (1.2), so a downgrade to
1.1 or 1.0 ended carrying ``minimum_version: 1.2`` — and the 1.0 / 1.1 engine the
downgrade exists to serve REFUSES it (its expected schema is below the 1.2
floor). The reverse now lowers a stale contract floor to the registry minimum,
which can never lock out any 1.x target.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ruamel.yaml import YAML

from setforge.migrations import (
    MigrationRoots,
    detect_current_schema,
    find_migration_path,
    parse_schema_version,
)


def _roots(tmp_path: Path) -> MigrationRoots:
    return MigrationRoots(
        cfg_path=tmp_path / "setforge.yaml",
        repo_root=tmp_path,
        home=tmp_path / "home",
    )


def _load(path: Path) -> dict:
    yaml = YAML(typ="rt")
    with path.open("r", encoding="utf-8") as fh:
        return yaml.load(fh)


def _write_2_0_config(tmp_path: Path) -> None:
    """A 2.0 config carrying the contract floor + a 2.0-exclusive deep span."""
    (tmp_path / "setforge.yaml").write_text(
        'minimum_version: "2.0"\n'
        "schema_version: '2.0'\n"
        "tracked_files:\n"
        "  settings:\n"
        "    src: settings.yaml\n"
        "    dst: ~/settings.yaml\n"
        "    spans:\n"
        "      - anchor: editor\n"
        "        kind: pinned\n"
        "        semantics: host-local\n"
        "        deep: true\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("to_v", ["1.1", "1.0"])
def test_cross_major_downgrade_below_1_2_clears_locking_floor(
    tmp_path: Path, to_v: str
) -> None:
    """The production reverse chain to 1.1 / 1.0 must not leave a locking floor.

    Walks the real ``find_migration_path`` chain (which crosses the major) and
    applies every step in order, exactly as the CLI driver does. The final
    config must end at the target schema AND carry a floor that the target
    engine satisfies — otherwise ``load_config`` -> ``_refuse_below_floor``
    would reject the very config the downgrade produced.
    """
    _write_2_0_config(tmp_path)
    roots = _roots(tmp_path)

    chain = find_migration_path(from_v="2.0", to_v=to_v)
    assert chain  # the chain must bridge the major boundary
    for migration in chain:
        migration.apply(roots=roots)

    cfg_path = tmp_path / "setforge.yaml"
    assert detect_current_schema(cfg_path) == to_v

    data = _load(cfg_path)
    floor = data.get("minimum_version")
    # The target engine's expected schema is `to_v`; it must SATISFY the floor
    # (full major.minor compare, >= boundary) or load_config refuses the config.
    assert floor is not None
    assert parse_schema_version(str(floor)) <= parse_schema_version(to_v), (
        f"floor {floor!r} exceeds the {to_v} target engine — it would refuse "
        f"the down-migrated config (the cross-major downgrade guarantee is "
        f"broken)"
    )


def test_cross_major_downgrade_to_1_0_floor_is_registry_min(tmp_path: Path) -> None:
    """A 2.0 -> 1.0 downgrade lands the floor at the registry minimum (1.0)."""
    _write_2_0_config(tmp_path)
    roots = _roots(tmp_path)
    for migration in find_migration_path(from_v="2.0", to_v="1.0"):
        migration.apply(roots=roots)
    data = _load(tmp_path / "setforge.yaml")
    assert str(data["minimum_version"]) == "1.0"
