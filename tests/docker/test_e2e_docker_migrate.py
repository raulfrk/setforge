"""Docker e2e tests for ``setforge migrate`` — the first schema migration.

Exercises the 1.0 → 1.1 version-stamp migration end-to-end against a
real Debian 12 container + the installed ``setforge`` binary:

- ``migrate --check`` lists the 1.0 → 1.1 stamp on a frozen 1.0 config.
- ``migrate --apply --yes`` stamps ``schema_version: '1.1'`` and writes a
  ``.pre-1.1.bak`` backup sibling.
- ``migrate --pin=1.0`` round-trips (pins back to the from_version).
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


def test_migrate_check_lists_the_stamp(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``migrate --check`` lists the 1.0 → 1.1 version-stamp migration."""
    c = docker_container()
    _seed_frozen_config(c)
    result = c.exec(
        ["uv", "run", "setforge", "migrate", "--check", f"--config={_CFG_PATH}"],
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "1 migration(s) available" in combined, combined
    assert "1.0 → 1.1" in combined, combined
    assert "schema_version" in combined, combined


def test_migrate_apply_stamps_schema_version_with_backup(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``migrate --apply --yes`` stamps ``schema_version: '1.1'`` + writes a backup."""
    c = docker_container()
    _seed_frozen_config(c)
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
    assert "1.1" in after, after
    # The APPLY_WITH_BACKUP default writes a .pre-1.1.bak sibling.
    backup = c.read_text(f"{_CFG_PATH}.pre-1.1.bak")
    assert "schema_version" not in backup, backup


def test_migrate_apply_is_revertible(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A migrate --apply is revertible: revert restores the pre-migration config.

    The frozen 1.0 config (no schema_version) is stamped to 1.1, then
    ``setforge revert --profile=migrate`` reverses the recorded transition,
    restoring the byte-exact pre-migration setforge.yaml (schema_version gone).
    """
    c = docker_container()
    _seed_frozen_config(c)
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
    assert "1.1" in c.read_text(_CFG_PATH)

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
    _seed_frozen_config(c)
    # First stamp it to 1.1, then pin back to 1.0.
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
    assert "1.1" in c.read_text(_CFG_PATH)

    pin_res = c.exec(
        ["uv", "run", "setforge", "migrate", "--pin=1.0", f"--config={_CFG_PATH}"],
        check=False,
    )
    assert pin_res.returncode == 0, pin_res.stdout + pin_res.stderr
    after = c.read_text(_CFG_PATH)
    assert "schema_version" in after, after
    assert "1.0" in after, after
    assert "1.1" not in after, after


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
    """1.0 -> apply (1.1) -> migrate --to=1.0 strips the stamp back to 1.0."""
    c = docker_container()
    _seed_frozen_config(c)
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
    assert "schema_version: '1.1'" in c.read_text(_CFG_PATH)
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
    """A 2.0 config on this (1.x) engine refuses cleanly — no traceback."""
    c = docker_container()
    _seed_cfg(c, _cfg_with_schema('schema_version: "2.0"\n'))
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
