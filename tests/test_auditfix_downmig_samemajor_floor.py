"""Regression: a same-major floor must not survive a cross-major downgrade.

Companion to ``test_auditfix_downmig_below_1_2`` (which covers the stale CONTRACT
floor, ``>= 2.0``). This covers the narrower variant: a hand-authored
``minimum_version`` strictly BETWEEN the chain's ultimate target and the contract
floor (e.g. ``1.5`` on a valid 2.0 config). The 2.0 -> 1.2 reverse is the only
chain step that touches the floor; the deeper same-major reverses (1.2 -> 1.1 ->
1.0) never do. The prior fix lowered such a floor only to THIS step's
``to_version`` (1.2), so a downgrade to 1.1 / 1.0 ended carrying
``minimum_version: 1.2`` and the target engine refused the very config the
downgrade produced. The reverse now lowers it to the registry minimum, which can
never lock out any reachable 1.x target.
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


def _write_2_0_config_with_floor(tmp_path: Path, floor: str) -> None:
    """A valid 2.0 config carrying a hand-authored same-major floor."""
    (tmp_path / "setforge.yaml").write_text(
        f'minimum_version: "{floor}"\n'
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
def test_same_major_floor_does_not_lock_out_sub_1_2_target(
    tmp_path: Path, to_v: str
) -> None:
    """A 2.0 config with floor 1.5 downgraded to 1.1 / 1.0 must stay loadable.

    Before the fix the floor was lowered only to 1.2 (this step's to_version),
    so the 1.1 / 1.0 target engine's ``_refuse_below_floor`` rejected the result.
    """
    _write_2_0_config_with_floor(tmp_path, "1.5")
    roots = _roots(tmp_path)

    chain = find_migration_path(from_v="2.0", to_v=to_v)
    assert chain
    for migration in chain:
        migration.apply(roots=roots)

    cfg_path = tmp_path / "setforge.yaml"
    assert detect_current_schema(cfg_path) == to_v

    floor = _load(cfg_path).get("minimum_version")
    assert floor is not None
    assert parse_schema_version(str(floor)) <= parse_schema_version(to_v), (
        f"floor {floor!r} (from a hand-authored 1.5) exceeds the {to_v} target "
        f"engine — it would refuse the down-migrated config"
    )


def test_same_major_low_floor_left_untouched(tmp_path: Path) -> None:
    """A floor already at/below the step's to_version is not disturbed."""
    _write_2_0_config_with_floor(tmp_path, "1.1")
    roots = _roots(tmp_path)
    # Downgrade only to 1.2: a 1.1 floor already satisfies a 1.2 target, so the
    # reverse must leave it untouched (it never blocks the target).
    for migration in find_migration_path(from_v="2.0", to_v="1.2"):
        migration.apply(roots=roots)
    floor = _load(tmp_path / "setforge.yaml").get("minimum_version")
    assert str(floor) == "1.1"
