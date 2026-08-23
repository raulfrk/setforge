"""Docker e2e tests for ``setforge migrate`` — the schema version-stamp chain.

Exercises the 1.0 → 1.1 → 1.2 → 2.0 → 2.1 → 3.0 → 4.0 → 5.0 → 6.0 → 6.1
→ 6.2 → 6.3 → 6.4 → 6.5 migration chain end-to-end against a real Debian 12
container + the installed
``setforge`` binary:

- ``migrate --check`` lists the full 1.0 → … → 6.4 → 6.5 chain
  on a frozen 1.0 config (the listing never gates, so it shows all steps).
- ``migrate --apply --yes`` walks the chain to ``schema_version: '6.5'`` (the
  build's current expected) and writes a ``.pre-6.5.bak`` backup sibling. The
  destructive 1.2 → 2.0 contract step is gated on an operator-declared
  ``minimum_version >= 2.0`` AND the chain-terminal 5.0 → 6.0
  profile-fields-retire step is gated on ``minimum_version >= 6.0``, so the
  apply-family configs carry the 6.5 floor (which satisfies all gates).
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


# A frozen pre-versioning config that ALSO declares the contract floor.
# The 1.2 → 2.0 step drops the legacy preserve_* fields irreversibly and the
# chain-terminal 5.0 → 6.0 step drops the legacy per-profile package/reconcile
# fields irreversibly, so each refuses unless minimum_version attests every
# host is upgraded. A 6.5 floor clears all gates (>= is a full compare). The
# apply-family tests need the full chain to run, so they seed this variant;
# the config still detects as the 1.0 baseline (no schema_version key).
_FROZEN_1_0_FLOORED_YAML: str = (
    "version: 1\n"
    'minimum_version: "6.5"\n'
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
    """Write a frozen 1.0 config carrying ``minimum_version: "6.5"``.

    The floor lets the destructive 1.2 → 2.0 contract step AND the terminal
    5.0 → 6.0 profile-fields-retire step run, so the apply-family tests can
    walk the full chain to the build's expected 6.5.
    """
    c.exec(["mkdir", "-p", f"{_CFG_DIR}/tracked"])
    c.write_text(_CFG_PATH, _FROZEN_1_0_FLOORED_YAML)
    c.write_text(f"{_CFG_DIR}/tracked/foo.md", "hello\n")


@pytest.mark.smoke
def test_migrate_check_lists_the_stamp(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``migrate --check`` lists the full 1.0 → … → 6.4 → 6.5 chain.

    The listing never gates on the contract floor, so a floorless frozen 1.0
    config still shows all steps (including the 1.2 → 2.0 contract, the
    2.0 → 2.1 marker-retire step, the 3.0 → 4.0 span-surface-retire cutover, the
    4.0 → 5.0 span-types-retire restamp, and the 5.0 → 6.0 profile-fields-retire
    cutover).
    """
    c = docker_container()
    _seed_frozen_config(c)
    result = c.exec(
        ["uv", "run", "setforge", "migrate", "--check", f"--config={_CFG_PATH}"],
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "13 migration(s) available" in combined, combined
    assert "1.0 → 1.1" in combined, combined
    assert "1.1 → 1.2" in combined, combined
    assert "1.2 → 2.0" in combined, combined
    assert "2.0 → 2.1" in combined, combined
    assert "2.1 → 3.0" in combined, combined
    assert "3.0 → 4.0" in combined, combined
    assert "4.0 → 5.0" in combined, combined
    assert "5.0 → 6.0" in combined, combined
    assert "6.0 → 6.1" in combined, combined
    assert "6.1 → 6.2" in combined, combined
    assert "6.2 → 6.3" in combined, combined
    assert "6.3 → 6.4" in combined, combined
    assert "6.4 → 6.5" in combined, combined
    assert "schema_version" in combined, combined


def test_migrate_apply_stamps_schema_version_with_backup(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``migrate --apply --yes`` stamps ``schema_version: '6.5'`` + writes a backup."""
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
    # A frozen-1.0 apply runs the full chain to the build's expected version (6.5).
    assert "schema_version: '6.5'" in after, after
    # The APPLY_WITH_BACKUP default writes a .pre-<chain-end>.bak sibling.
    backup = c.read_text(f"{_CFG_PATH}.pre-6.5.bak")
    assert "schema_version" not in backup, backup


def test_migrate_apply_is_revertible(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A frozen-1.0 migrate --apply reverts to the byte-exact origin (INV-5).

    The frozen 1.0 config (no schema_version) is stamped through the full chain
    to 6.0. The chain-terminal profile-fields-retire cutover records the single
    durable transition threading the migrate driver's pre-chain frozen image, so
    ONE ``setforge revert --profile=migrate`` walks the config all the way back
    to the frozen-1.0 origin — not merely an intermediate schema state.
    """
    c = docker_container()
    _seed_floored_config(c)
    pre = c.read_text(_CFG_PATH)
    assert "schema_version" not in pre, pre  # frozen 1.0 baseline

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
    assert "schema_version: '6.5'" in c.read_text(_CFG_PATH)

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
    # ONE revert restores the frozen-1.0 origin byte-exact (INV-5) — not an
    # intermediate schema state.
    assert c.read_text(_CFG_PATH) == pre, c.read_text(_CFG_PATH)


_CFG_2_1_DISPOSITION_YAML: str = (
    "schema_version: '2.1'\n"
    "version: 1\n"
    # The full chain now terminates at the 5.0 → 6.0 profile-fields-retire
    # cutover, which is gated on minimum_version >= 6.0; declare the floor so
    # the apply walks all the way to the build's current 6.5.
    'minimum_version: "6.5"\n'
    "tracked_files:\n"
    "  foo:\n"
    "    src: foo.md\n"
    "    dst: ~/.foo.md\n"
    "    disposition: forked\n"
    "profiles:\n"
    "  base:\n"
    "    tracked_files:\n"
    "      - foo\n"
)


def test_migrate_2_1_to_3_0_strips_disposition_and_reverts(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """The 2.1 config migrates forward through the full chain to a clean, valid
    6.0 config and is revertible.

    A 2.1 config declaring ``disposition:`` is migrated to the build's current
    6.0: the retired ``disposition`` key is stripped at the 2.1 → 3.0 step so
    ``validate`` accepts the result, then ``setforge revert --profile=migrate``
    byte-restores the pre-migration 2.1 config (disposition key back).
    """
    c = docker_container()
    c.exec(["mkdir", "-p", f"{_CFG_DIR}/tracked"])
    c.write_text(_CFG_PATH, _CFG_2_1_DISPOSITION_YAML)
    c.write_text(f"{_CFG_DIR}/tracked/foo.md", "hello\n")
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
    after = c.read_text(_CFG_PATH)
    assert "schema_version: '6.5'" in after, after
    assert "disposition" not in after, after

    validate_res = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "validate",
            f"--config={_CFG_PATH}",
            "--profile=base",
        ],
        check=False,
    )
    assert validate_res.returncode == 0, validate_res.stdout + validate_res.stderr

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
    assert c.read_text(_CFG_PATH) == before, c.read_text(_CFG_PATH)


def test_migrate_pin_round_trips_to_from_version(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``migrate --pin=1.0`` writes the from_version back into setforge.yaml."""
    c = docker_container()
    _seed_floored_config(c)
    # First stamp it through the chain to 6.5, then pin back to 1.0.
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
    assert "schema_version: '6.5'" in c.read_text(_CFG_PATH)

    pin_res = c.exec(
        ["uv", "run", "setforge", "migrate", "--pin=1.0", f"--config={_CFG_PATH}"],
        check=False,
    )
    assert pin_res.returncode == 0, pin_res.stdout + pin_res.stderr
    after = c.read_text(_CFG_PATH)
    assert "schema_version" in after, after
    assert "1.0" in after, after
    # The pin overwrote the applied 6.5 stamp in place.
    assert "schema_version: '6.5'" not in after, after


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
    # The schema-mismatch warning fires (1.0 declared vs 6.5 expected) but
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
    """1.0 -> apply (--to=2.0) -> migrate --to=1.0 walks back to the 1.0 baseline.

    The forward hop targets 2.0 explicitly: the 2.0 → 2.1 marker-retire step is
    ONE-WAY (a stateless ``--to`` reverse cannot regenerate the retired
    user-section marker syntax — only ``setforge revert --profile=migrate``
    byte-restores it), so the reversible round-trip is exercised on the
    1.0 ↔ 2.0 chain. The downgrade
    is a real reverse walk: 2.0 -> 1.2 (the contract reverse) then 1.2 -> 1.1
    (RestampMigration restamps the older version) then 1.1 -> 1.0
    (VersionStampMigration's reverse strips the key), leaving the key-absent 1.0
    baseline.
    """
    c = docker_container()
    _seed_floored_config(c)
    up = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "migrate",
            "--to=2.0",
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


def test_downgrade_across_marker_retire_refuses(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A ``--to`` downgrade across the 4.0 → 3.0 span-surface-retire cutover refuses.

    The 4.0 span-surface retirement is ONE-WAY: a stateless reverse cannot
    regenerate the retired host-local span-declaration surface from the per-unit
    SHARED/LOCAL store it folded into. Applying forward to the build's current
    6.0 then requesting ``--to=2.0`` walks the reachable reverse hops (6.0 → 5.0
    profile-fields unfold, then 5.0 → 4.0 span-types restamp) first, then hits
    that irreversible 4.0 → 3.0 reverse step (before ever reaching the 2.0 → 2.1
    marker boundary), so the chain refuses with the irreversibility message and
    ATOMICALLY rolls back the completed reverse hops, leaving the config at
    6.0 — the reversible window is served by ``setforge revert
    --profile=migrate`` (byte-restore), not by a stateless reverse migration.
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
    assert "schema_version: '6.5'" in c.read_text(_CFG_PATH)

    down = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "migrate",
            "--to=2.0",
            "--apply",
            "--yes",
            f"--config={_CFG_PATH}",
        ],
        check=False,
    )
    assert down.returncode != 0, down.stdout + down.stderr
    combined = down.stdout + down.stderr
    assert "cannot down-migrate from schema 4.0 to 3.0" in combined, combined
    assert "cannot be regenerated" in combined, combined
    assert "Traceback (most recent call last)" not in combined, combined
    # The refused downgrade atomically rolled back the completed reverse hops
    # (6.5 → 6.4 → 6.3 → 6.2 → 6.1 → 6.0 → 5.0 → 4.0): still 6.5.
    assert "schema_version: '6.5'" in c.read_text(_CFG_PATH)


def test_install_cross_major_config_refuses_clean(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A 7.0 config on this (6.x) engine refuses cleanly — no traceback."""
    c = docker_container()
    _seed_cfg(c, _cfg_with_schema('schema_version: "7.0"\n'))
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
    _seed_cfg(c, _cfg_with_schema('schema_version: "6.9"\nfuture_field: 42\n'))
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

    minimum_version 6.6 puts this schema-6.5 engine below the floor, so the
    floor fires and refuses every config-reading verb. ``--version`` (no config
    read) stays usable.
    """
    c = docker_container()
    _seed_cfg(c, _cfg_with_schema('schema_version: "2.0"\nminimum_version: "6.6"\n'))
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
    stripped = c.read_text(f"{_CFG_DIR}/tracked/foo.md")
    assert stripped == _HL_MD_STRIPPED
    # Acceptance #7: NO setforge user-section markers survive --finalize.
    assert "setforge:user-section" not in stripped, stripped


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
