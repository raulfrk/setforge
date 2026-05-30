"""Docker e2e: ``setforge validate`` did-you-mean UX on setforge.yaml.

Mirrors :mod:`tests.docker.test_e2e_docker_validate_did_you_mean` (local.yaml
side) but exercises the engine-config side: typo'd top-level / nested
keys in ``setforge.yaml`` route through the
``format_schema_validation_error`` + ``suggest_close_match`` path
against ``Config.model_fields.keys()`` / ``Profile.model_fields.keys()``
/ ``TrackedFile.model_fields.keys()`` instead of bailing on first
error via ``typer.Exit(1)``.

Each test spins a fresh container, writes a crafted ``setforge.yaml``,
runs ``uv run setforge validate --all`` against it, and asserts on the
captured stdout/stderr substrings.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import ContainerHandle

pytestmark = pytest.mark.e2e_docker

_WORKDIR: str = "/workspace"
_SETFORGE_YAML: str = f"{_WORKDIR}/setforge.yaml"
_TRACKED_FILE: str = f"{_WORKDIR}/tracked/tracked_file.txt"


def _seed_workspace(c: ContainerHandle) -> None:
    """Write a tracked source under the workspace so file-exists checks pass."""
    c.exec(["mkdir", "-p", f"{_WORKDIR}/tracked"], check=True)
    c.write_text(_TRACKED_FILE, "x\n")


def _run_validate(c: ContainerHandle) -> tuple[int, str]:
    """Invoke ``setforge validate --all`` against the in-container setforge.yaml."""
    result = c.exec(
        ["uv", "run", "setforge", "validate", "--all", f"--config={_SETFORGE_YAML}"],
        check=False,
    )
    return result.returncode, result.stdout + result.stderr


def test_validate_setforge_yaml_top_level_typo_suggests(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A typo'd top-level key (``proffiles:``) in setforge.yaml routes
    through the did-you-mean formatter and surfaces a "Did you mean 'profiles'"
    suggestion against the introspected ``Config.model_fields`` list."""
    c = docker_container()
    _seed_workspace(c)
    c.write_text(
        _SETFORGE_YAML,
        "version: 1\n"
        "tracked_files:\n"
        "  d:\n"
        "    src: tracked_file.txt\n"
        "    dst: ~/.some-tracked_file\n"
        "proffiles:\n"
        "  p:\n"
        "    tracked_files: [d]\n",
    )
    rc, out = _run_validate(c)
    assert rc == 1, out
    assert "✗ SCHEMA VALIDATION ERROR" in out
    assert "←─── line" in out
    assert "Did you mean 'profiles'" in out
    assert "Fix:" in out
    assert "validation FAILED" in out


def test_validate_setforge_yaml_profile_nested_typo_suggests(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A typo'd profile-nested key (``extendz``) routes through did-you-mean with
    a close-match against :attr:`Profile.model_fields.keys()`."""
    c = docker_container()
    _seed_workspace(c)
    c.write_text(
        _SETFORGE_YAML,
        "version: 1\n"
        "tracked_files:\n"
        "  d:\n"
        "    src: tracked_file.txt\n"
        "    dst: ~/.some-tracked_file\n"
        "profiles:\n"
        "  p:\n"
        "    tracked_files: [d]\n"
        "    extendz: q\n",
    )
    rc, out = _run_validate(c)
    assert rc == 1, out
    assert "✗ SCHEMA VALIDATION ERROR" in out
    assert "Did you mean 'extends'" in out
    assert "validation FAILED" in out


def test_validate_setforge_yaml_tracked_files_nested_typo_suggests(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A typo'd tracked_files-entry key (``srcc``) routes through did-you-mean
    with a close-match against :attr:`TrackedFile.model_fields.keys()`."""
    c = docker_container()
    _seed_workspace(c)
    c.write_text(
        _SETFORGE_YAML,
        "version: 1\n"
        "tracked_files:\n"
        "  d:\n"
        "    srcc: tracked_file.txt\n"
        "    dst: ~/.some-tracked_file\n"
        "profiles:\n"
        "  p:\n"
        "    tracked_files: [d]\n",
    )
    rc, out = _run_validate(c)
    assert rc == 1, out
    assert "✗ SCHEMA VALIDATION ERROR" in out
    assert "Did you mean 'src'" in out
    assert "validation FAILED" in out


def test_validate_setforge_yaml_cycle_error_not_routed_to_did_you_mean(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A profile-extends cycle (SetforgeError tier) MUST NOT route
    through did-you-mean — the existing ``schema: profile cycle: ...`` phrasing
    is the contract callers key on."""
    c = docker_container()
    _seed_workspace(c)
    c.write_text(
        _SETFORGE_YAML,
        "version: 1\n"
        "tracked_files:\n"
        "  d:\n"
        "    src: tracked_file.txt\n"
        "    dst: ~/.some-tracked_file\n"
        "profiles:\n"
        "  a:\n"
        "    extends: b\n"
        "    tracked_files: [d]\n"
        "  b:\n"
        "    extends: a\n"
        "    tracked_files: [d]\n",
    )
    # --profile=a triggers profile resolution → cycle detection.
    result = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "validate",
            "--profile=a",
            f"--config={_SETFORGE_YAML}",
        ],
        check=False,
    )
    out = result.stdout + result.stderr
    assert result.returncode == 1, out
    assert "profile cycle" in out
    assert "✗ SCHEMA VALIDATION ERROR" not in out


def test_validate_setforge_yaml_missing_profile_not_routed_to_did_you_mean(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A ``--profile=<name>`` that doesn't exist surfaces via the
    existing string-failures path (not did-you-mean). The did-you-mean SCHEMA header
    must be absent from the output."""
    c = docker_container()
    _seed_workspace(c)
    c.write_text(
        _SETFORGE_YAML,
        "version: 1\n"
        "tracked_files:\n"
        "  d:\n"
        "    src: tracked_file.txt\n"
        "    dst: ~/.some-tracked_file\n"
        "profiles:\n"
        "  p:\n"
        "    tracked_files: [d]\n",
    )
    result = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "validate",
            "--profile=does_not_exist",
            f"--config={_SETFORGE_YAML}",
        ],
        check=False,
    )
    out = result.stdout + result.stderr
    assert result.returncode == 1, out
    assert "✗ SCHEMA VALIDATION ERROR" not in out
    assert "does_not_exist" in out
