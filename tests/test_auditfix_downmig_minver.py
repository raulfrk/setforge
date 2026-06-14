"""Regression: the 2.0 -> 1.2 reverse must lower a stale minimum_version floor.

The forward 1.2 -> 2.0 contract GATES on ``minimum_version >= 2.0`` but never
re-stamped the floor, so the reverse used to leave a config carrying
``schema_version: 1.2`` AND ``minimum_version: 2.0`` — unloadable on the very
1.2 engine the downgrade exists to serve (its expected schema 1.2 is below the
2.0 floor). The reverse now lowers a floor strictly above the target version
down to it, while leaving a hand-authored same-major floor untouched.
"""

from __future__ import annotations

from pathlib import Path

from ruamel.yaml import YAML

from setforge.migrations import MigrationRoots, parse_schema_version
from setforge.migrations._contract_2_0 import Contract20Migration


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


def test_reverse_lowers_contract_floor_to_target(tmp_path: Path) -> None:
    """1.2 -> 2.0 -> 1.2 ends with a floor that does NOT exceed the 1.2 target."""
    (tmp_path / "setforge.yaml").write_text(
        'minimum_version: "2.0"\n'
        "schema_version: '1.2'\n"
        "tracked_files:\n"
        "  settings:\n"
        "    src: settings.yaml\n"
        "    dst: ~/settings.yaml\n"
        "    preserve_user_keys_deep:\n"
        "      - editor\n",
        encoding="utf-8",
    )
    roots = _roots(tmp_path)
    fwd = Contract20Migration()
    fwd.apply(roots=roots)
    fwd.reverse.apply(roots=roots)
    data = _load(tmp_path / "setforge.yaml")
    assert data["schema_version"] == "1.2"
    # The stale 2.0 floor must be gone — a 1.2 engine (expected schema 1.2)
    # would refuse against it. The floor, if present, must be <= the target.
    floor = data.get("minimum_version")
    assert floor is not None  # reverse lowers rather than strips
    assert parse_schema_version(str(floor)) <= parse_schema_version("1.2")


def test_reverse_direct_lowers_stale_floor(tmp_path: Path) -> None:
    """A direct reverse on a 2.0 config lowers the stale contract floor.

    The 2.0 floor is the cross-major contract attestation; the reverse lowers
    it all the way to the registry minimum so it can never lock out any 1.x
    target a deeper chain serves (the single-step 1.2 target included).
    """
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
    roots = _roots(tmp_path)
    Contract20Migration().reverse.apply(roots=roots)
    data = _load(tmp_path / "setforge.yaml")
    assert data["schema_version"] == "1.2"
    # Lowered to the registry minimum (1.0) — never above the 1.2 target.
    assert str(data["minimum_version"]) == "1.0"
    assert parse_schema_version(str(data["minimum_version"])) <= parse_schema_version(
        "1.2"
    )


def test_reverse_leaves_low_handauthored_floor_untouched(tmp_path: Path) -> None:
    """A floor at/below the target (e.g. hand-authored 1.0) is left as-is."""
    (tmp_path / "setforge.yaml").write_text(
        'minimum_version: "1.0"\n'
        "schema_version: '2.0'\n"
        "tracked_files:\n"
        "  settings:\n"
        "    src: settings.yaml\n"
        "    dst: ~/settings.yaml\n"
        "    spans:\n"
        "      - anchor: editor\n"
        "        kind: pinned\n"
        "        semantics: host-local\n",
        encoding="utf-8",
    )
    roots = _roots(tmp_path)
    Contract20Migration().reverse.apply(roots=roots)
    data = _load(tmp_path / "setforge.yaml")
    # Below the target — never blocks the 1.2 engine, so untouched.
    assert str(data["minimum_version"]) == "1.0"


def test_reverse_no_floor_stays_absent(tmp_path: Path) -> None:
    """A config with no floor declared keeps it absent after the reverse."""
    (tmp_path / "setforge.yaml").write_text(
        "schema_version: '2.0'\n"
        "tracked_files:\n"
        "  settings:\n"
        "    src: settings.yaml\n"
        "    dst: ~/settings.yaml\n"
        "    spans:\n"
        "      - anchor: editor\n"
        "        kind: pinned\n"
        "        semantics: host-local\n",
        encoding="utf-8",
    )
    roots = _roots(tmp_path)
    Contract20Migration().reverse.apply(roots=roots)
    data = _load(tmp_path / "setforge.yaml")
    assert "minimum_version" not in data
