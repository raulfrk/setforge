"""Docker e2e tests for ``setforge validate`` mockup-D UX (setforge-tmln).

Asserts on the literal output strings of the mockup-D validate-error
UX against a fresh container running the actual ``setforge`` CLI —
the unit suite covers shape and integration; this ring confirms the
formatter rides through the real Typer command boundary into a real
terminal capture under a real venv.

Each test spins a fresh container, writes a crafted
``~/.config/setforge/local.yaml``, runs ``uv run setforge validate
--all`` against the shared e2e config fixture, and asserts on the
captured stdout/stderr substrings.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle

pytestmark = pytest.mark.e2e_docker

_HOME_LOCAL_YAML: str = "/home/tester/.config/setforge/local.yaml"


def _run_validate(c: ContainerHandle) -> tuple[int, str]:
    """Invoke ``setforge validate --all`` and return (returncode, combined output)."""
    result = c.exec(
        ["uv", "run", "setforge", "validate", "--all", f"--config={CONFIG_FIXTURE}"],
        check=False,
    )
    return result.returncode, result.stdout + result.stderr


def test_validate_local_yaml_clean_exits_zero(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A well-formed local.yaml (binaries override only) → validate exits 0."""
    c = docker_container()
    c.write_text(_HOME_LOCAL_YAML, "binaries:\n  uv: /usr/bin/uv\n")
    rc, out = _run_validate(c)
    assert rc == 0, out
    assert "ok" in out


def test_validate_local_yaml_typo_top_level_key_emits_mockup_d(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A typo'd top-level key surfaces all four mockup-D UX elements:
    ``← line``, ``Did you mean``, ``Fix:``, and ``validation FAILED``."""
    c = docker_container()
    # 'binares' is Levenshtein distance 2 from 'binaries' (insert 'i' + reorder).
    c.write_text(_HOME_LOCAL_YAML, "binares:\n  uv: /usr/bin/uv\n")
    rc, out = _run_validate(c)
    assert rc == 1, out
    assert "←─── line" in out
    assert "Did you mean 'binaries'" in out
    assert "Fix:" in out
    assert "validation FAILED" in out


def test_validate_local_yaml_parse_error_emits_yaml_parse_category(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A malformed local.yaml surfaces in the YAML PARSE category (NOT
    the schema-validation category), and the final summary still
    reports the failure count."""
    c = docker_container()
    # Tab-indented mapping value is a YAML parse error in ruamel's safe loader.
    c.write_text(_HOME_LOCAL_YAML, "source:\n\tpath: /tmp\n")
    rc, out = _run_validate(c)
    assert rc == 1, out
    assert "✗ YAML PARSE ERROR" in out
    assert "local.yaml" in out
    assert "validation FAILED" in out


def test_validate_local_yaml_multi_error_reports_all_then_refuses(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Two schema errors both land in the output; the summary names
    the count; the refusal line trails the summary (mockup D's
    report-all-then-refuse contract)."""
    c = docker_container()
    c.write_text(_HOME_LOCAL_YAML, "unknown_a: 1\nunknown_b: 2\n")
    rc, out = _run_validate(c)
    assert rc == 1, out
    assert out.count("✗ SCHEMA VALIDATION ERROR") >= 2
    assert "validation FAILED:" in out
    assert "no changes will be made" in out
