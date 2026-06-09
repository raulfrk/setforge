"""Docker e2e tests for ``setforge migrate`` — the schema version-stamp chain.

Exercises the 1.0 → 1.1 → 1.2 → 2.0 migration chain end-to-end against a
real Debian 12 container + the installed ``setforge`` binary:

- ``migrate --check`` lists the full 1.0 → 1.1 → 1.2 → 2.0 chain on a frozen
  1.0 config (the listing never gates, so it shows all three steps).
- ``migrate --apply --yes`` walks the chain to ``schema_version: '2.0'`` (the
  build's current expected) and writes a ``.pre-2.0.bak`` backup sibling. The
  destructive 1.2 → 2.0 contract step is gated on an operator-declared
  ``minimum_version >= 2.0``, so the apply-family configs carry that floor.
- ``migrate --pin=1.0`` round-trips (pins back to the chain's from_version).
- a pre-bump frozen config (no ``schema_version`` key) still ``install``s.

Each test seeds its own minimal ``setforge.yaml`` and drives ``migrate``
with an explicit ``--config=`` so the cases are self-contained and never
depend on the shared fixture's schema state.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import ContainerHandle

pytestmark = pytest.mark.e2e_docker

_CFG_DIR: str = "/tmp/migrate-cfg"
_CFG_PATH: str = f"{_CFG_DIR}/setforge.yaml"
_HOME_LOCAL_YAML: str = "/home/tester/.config/setforge/local.yaml"

# A frozen pre-versioning config: no schema_version key. detect_current_schema
# maps the absence to the "1.0" baseline.
_FROZEN_1_0_YAML: str = (
    "version: 1\n"
    "tracked_files:\n"
    "  foo:\n"
    "    src: foo.md\n"
    "    dst: ~/.foo.md\n"
    "profiles:\n"
    "  base:\n"
    "    tracked_files:\n"
    "      - foo\n"
)


def _seed_frozen_config(c: ContainerHandle) -> None:
    """Write a frozen 1.0 ``setforge.yaml`` (no schema_version) into the container."""
    c.exec(["mkdir", "-p", f"{_CFG_DIR}/tracked"])
    c.write_text(_CFG_PATH, _FROZEN_1_0_YAML)
    c.write_text(f"{_CFG_DIR}/tracked/foo.md", "hello\n")


# A frozen pre-versioning config that ALSO declares the 2.0 contract floor.
# The 1.2 → 2.0 step drops the legacy preserve_* fields irreversibly, so it
# refuses unless minimum_version attests every host is on >= 2.0. The
# apply-family tests need the full chain to run, so they seed this variant;
# the config still detects as the 1.0 baseline (no schema_version key).
_FROZEN_1_0_FLOORED_YAML: str = (
    "version: 1\n"
    'minimum_version: "2.0"\n'
    "tracked_files:\n"
    "  foo:\n"
    "    src: foo.md\n"
    "    dst: ~/.foo.md\n"
    "profiles:\n"
    "  base:\n"
    "    tracked_files:\n"
    "      - foo\n"
)


def _seed_floored_config(c: ContainerHandle) -> None:
    """Write a frozen 1.0 config carrying ``minimum_version: "2.0"``.

    The floor lets the destructive 1.2 → 2.0 contract step run, so the
    apply-family tests can walk the full chain to the build's expected 2.0.
    """
    c.exec(["mkdir", "-p", f"{_CFG_DIR}/tracked"])
    c.write_text(_CFG_PATH, _FROZEN_1_0_FLOORED_YAML)
    c.write_text(f"{_CFG_DIR}/tracked/foo.md", "hello\n")


def test_migrate_check_lists_the_stamp(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``migrate --check`` lists the full 1.0 → 1.1 → 1.2 → 2.0 chain.

    The listing never gates on the contract floor, so a floorless frozen 1.0
    config still shows all three steps (including the 1.2 → 2.0 contract).
    """
    c = docker_container()
    _seed_frozen_config(c)
    result = c.exec(
        ["uv", "run", "setforge", "migrate", "--check", f"--config={_CFG_PATH}"],
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "3 migration(s) available" in combined, combined
    assert "1.0 → 1.1" in combined, combined
    assert "1.1 → 1.2" in combined, combined
    assert "1.2 → 2.0" in combined, combined
    assert "schema_version" in combined, combined


def test_migrate_apply_stamps_schema_version_with_backup(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``migrate --apply --yes`` stamps ``schema_version: '2.0'`` + writes a backup."""
    c = docker_container()
    _seed_floored_config(c)
    result = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "migrate",
            "--apply",
            "--yes",
            f"--config={_CFG_PATH}",
        ],
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    after = c.read_text(_CFG_PATH)
    assert "schema_version" in after, after
    # A frozen-1.0 apply runs the full chain to the build's expected version.
    assert "2.0" in after, after
    # The APPLY_WITH_BACKUP default writes a .pre-<chain-end>.bak sibling.
    backup = c.read_text(f"{_CFG_PATH}.pre-2.0.bak")
    assert "schema_version" not in backup, backup


def test_migrate_apply_is_revertible(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A migrate --apply is revertible: revert restores the pre-migration config.

    The frozen 1.0 config (no schema_version) is stamped through the chain to
    2.0, then ``setforge revert --profile=migrate`` reverses the recorded
    transition, restoring the byte-exact pre-migration setforge.yaml.
    """
    c = docker_container()
    _seed_floored_config(c)
    before = c.read_text(_CFG_PATH)

    apply_res = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "migrate",
            "--apply",
            "--yes",
            f"--config={_CFG_PATH}",
        ],
        check=False,
    )
    assert apply_res.returncode == 0, apply_res.stdout + apply_res.stderr
    assert "2.0" in c.read_text(_CFG_PATH)

    revert_res = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "revert",
            "--profile=migrate",
            f"--config={_CFG_PATH}",
            "--yes",
        ],
        check=False,
    )
    assert revert_res.returncode == 0, revert_res.stdout + revert_res.stderr
    # The pre-migration config is restored byte-for-byte (schema_version gone).
    assert c.read_text(_CFG_PATH) == before, c.read_text(_CFG_PATH)


def test_migrate_pin_round_trips_to_from_version(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``migrate --pin=1.0`` writes the from_version back into setforge.yaml."""
    c = docker_container()
    _seed_floored_config(c)
    # First stamp it through the chain to 2.0, then pin back to 1.0.
    apply_res = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "migrate",
            "--apply",
            "--yes",
            f"--config={_CFG_PATH}",
        ],
        check=False,
    )
    assert apply_res.returncode == 0, apply_res.stdout + apply_res.stderr
    assert "schema_version: '2.0'" in c.read_text(_CFG_PATH)

    pin_res = c.exec(
        ["uv", "run", "setforge", "migrate", "--pin=1.0", f"--config={_CFG_PATH}"],
        check=False,
    )
    assert pin_res.returncode == 0, pin_res.stdout + pin_res.stderr
    after = c.read_text(_CFG_PATH)
    assert "schema_version" in after, after
    assert "1.0" in after, after
    # The pin overwrote the applied 2.0 stamp in place.
    assert "schema_version: '2.0'" not in after, after


def test_frozen_pre_bump_config_still_installs(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A frozen 1.0 config (no schema_version) still ``install``s — non-fatal warn."""
    c = docker_container()
    _seed_frozen_config(c)
    c.write_text(
        _HOME_LOCAL_YAML,
        f"source:\n  kind: path\n  path: {_CFG_DIR}\n",
    )
    result = c.exec(
        ["uv", "run", "setforge", "install", "--profile=base"],
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    # The schema-mismatch warning fires (1.0 declared vs 1.1 expected) but
    # install proceeds and deploys the tracked file.
    combined = result.stdout + result.stderr
    assert "schema_version" in combined, combined
    assert c.read_text("/home/tester/.foo.md") == "hello\n"


# ---------------------------------------------------------------------------
# version-switching: downgrade round-trip + forward-tolerant reads
# ---------------------------------------------------------------------------


def _cfg_with_schema(extra: str = "") -> str:
    """A minimal valid config; ``extra`` injects top-level lines (e.g. a stamp)."""
    return (
        "version: 1\n"
        f"{extra}"
        "tracked_files:\n"
        "  foo:\n"
        "    src: foo.md\n"
        "    dst: ~/.foo.md\n"
        "profiles:\n"
        "  base:\n"
        "    tracked_files:\n"
        "      - foo\n"
    )


def _seed_cfg(c: ContainerHandle, body: str) -> None:
    c.exec(["mkdir", "-p", f"{_CFG_DIR}/tracked"])
    c.write_text(_CFG_PATH, body)
    c.write_text(f"{_CFG_DIR}/tracked/foo.md", "hello\n")
    c.write_text(_HOME_LOCAL_YAML, f"source:\n  kind: path\n  path: {_CFG_DIR}\n")


def test_migrate_to_downgrade_round_trip(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """1.0 -> apply (chain to 2.0) -> migrate --to=1.0 walks back to the 1.0 baseline.

    The downgrade is a real reverse walk: 2.0 -> 1.2 (the contract reverse)
    then 1.2 -> 1.1 (RestampMigration restamps the older version) then
    1.1 -> 1.0 (VersionStampMigration's reverse strips the key), leaving the
    key-absent 1.0 baseline.
    """
    c = docker_container()
    _seed_floored_config(c)
    up = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "migrate",
            "--apply",
            "--yes",
            f"--config={_CFG_PATH}",
        ],
        check=False,
    )
    assert up.returncode == 0, up.stdout + up.stderr
    assert "schema_version: '2.0'" in c.read_text(_CFG_PATH)
    down = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "migrate",
            "--to=1.0",
            "--apply",
            "--yes",
            f"--config={_CFG_PATH}",
        ],
        check=False,
    )
    assert down.returncode == 0, down.stdout + down.stderr
    # stamp removed -> declared schema is the 1.0 baseline again
    assert "schema_version" not in c.read_text(_CFG_PATH)
    check = c.exec(
        ["uv", "run", "setforge", "migrate", "--check", f"--config={_CFG_PATH}"],
        check=False,
    )
    assert "1.0" in (check.stdout + check.stderr)


def test_install_cross_major_config_refuses_clean(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A 3.0 config on this (2.x) engine refuses cleanly — no traceback."""
    c = docker_container()
    _seed_cfg(c, _cfg_with_schema('schema_version: "3.0"\n'))
    result = c.exec(
        ["uv", "run", "setforge", "install", "--profile=base"],
        check=False,
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "upgrade setforge" in combined, combined
    assert "Traceback (most recent call last)" not in combined, combined


def test_install_forward_tolerant_warns_on_unknown_key(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A same-major-newer config with an extra field loads + warns, not refuses."""
    c = docker_container()
    _seed_cfg(c, _cfg_with_schema('schema_version: "1.9"\nfuture_field: 42\n'))
    result = c.exec(
        ["uv", "run", "setforge", "install", "--profile=base"],
        check=False,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "future_field" in combined, combined
    assert "upgrade setforge" not in combined, combined
    assert c.read_text("/home/tester/.foo.md") == "hello\n"


# ---------------------------------------------------------------------------
# minimum_version floor + migrate --finalize tracked-marker strip
# ---------------------------------------------------------------------------

# A tracked markdown source carrying a host-local marker pair (the vestigial
# inline form that --finalize strips).
_HL_MD: str = (
    "intro\n"
    "<!-- setforge:user-section start host-local HL -->\n"
    "host body\n"
    "<!-- setforge:user-section end host-local HL -->\n"
    "outro\n"
)
_HL_MD_STRIPPED: str = "intro\noutro\n"


def _seed_cfg_with_md(c: ContainerHandle, body: str, md: str) -> None:
    """Seed a config + a tracked ``foo.md`` carrying ``md`` content."""
    _seed_cfg(c, body)
    c.write_text(f"{_CFG_DIR}/tracked/foo.md", md)


def test_sub_floor_engine_refuses_all_config_verbs(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A floor above this build's schema refuses every config-reading verb.

    minimum_version 3.0 puts this (schema-2.0) engine below the floor, so the
    floor fires and refuses every config-reading verb. ``--version`` (no config
    read) stays usable.
    """
    c = docker_container()
    _seed_cfg(c, _cfg_with_schema('schema_version: "2.0"\nminimum_version: "3.0"\n'))
    for verb in (
        ["install", "--profile=base"],
        ["compare", "--profile=base"],
        ["validate", "--all"],
        ["migrate", "--check", f"--config={_CFG_PATH}"],
    ):
        result = c.exec(["uv", "run", "setforge", *verb], check=False)
        combined = result.stdout + result.stderr
        assert result.returncode != 0, (verb, combined)
        assert "minimum_version" in combined, (verb, combined)
        assert "upgrade setforge" in combined, (verb, combined)
        assert "Traceback (most recent call last)" not in combined, (verb, combined)
    # A verb that never reads the config is unaffected by the floor.
    ver = c.exec(["uv", "run", "setforge", "--version"], check=False)
    assert ver.returncode == 0, ver.stdout + ver.stderr


def test_finalize_blocked_below_floor(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``migrate --finalize`` refuses when the floor is below the conversion version."""
    c = docker_container()
    _seed_cfg_with_md(c, _cfg_with_schema('schema_version: "1.2"\n'), _HL_MD)
    result = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "migrate",
            "--finalize",
            "--yes",
            f"--config={_CFG_PATH}",
        ],
        check=False,
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "minimum_version" in combined, combined
    # The tracked source is untouched.
    assert c.read_text(f"{_CFG_DIR}/tracked/foo.md") == _HL_MD


def test_finalize_permitted_above_floor_strips_markers(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """With minimum_version >= conversion version, --finalize strips host-local."""
    c = docker_container()
    _seed_cfg_with_md(
        c, _cfg_with_schema('schema_version: "1.2"\nminimum_version: "1.2"\n'), _HL_MD
    )
    result = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "migrate",
            "--finalize",
            "--yes",
            f"--config={_CFG_PATH}",
        ],
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert c.read_text(f"{_CFG_DIR}/tracked/foo.md") == _HL_MD_STRIPPED


def test_finalize_round_trip_revert_restores_markers(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``setforge revert --profile=migrate`` restores the stripped markers."""
    c = docker_container()
    _seed_cfg_with_md(
        c, _cfg_with_schema('schema_version: "1.2"\nminimum_version: "1.2"\n'), _HL_MD
    )
    fin = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "migrate",
            "--finalize",
            "--yes",
            f"--config={_CFG_PATH}",
        ],
        check=False,
    )
    assert fin.returncode == 0, fin.stdout + fin.stderr
    assert c.read_text(f"{_CFG_DIR}/tracked/foo.md") == _HL_MD_STRIPPED
    rev = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "revert",
            "--profile=migrate",
            "--yes",
            f"--config={_CFG_PATH}",
        ],
        check=False,
    )
    assert rev.returncode == 0, rev.stdout + rev.stderr
    assert c.read_text(f"{_CFG_DIR}/tracked/foo.md") == _HL_MD
