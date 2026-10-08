"""Docker E2E tests for the local.yaml plugin/extension/marketplace overlay.

Spec: SPEC 2. Exercises the install dry-run output against a real
Debian container with the actual installed ``setforge`` CLI:

- ``install --dry-run`` applies the merged sets so plugin / extension
  reconcile consumes overlay-added entries transparently and drops
  overlay-removed entries; the marketplace cross-ref check fires
  defensively at install time too.

The ``compare`` provenance tags and footer, and the ``validate``
collision / unknown-remove / cross-ref / orphan-overlay diagnostics, run
in-process in ``tests/test_cli_e2e.py`` and
``tests/test_cli_validate_orphan_overlay.py``.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle

pytestmark = pytest.mark.e2e_docker

_HOME_LOCAL_YAML = "/home/tester/.config/setforge/local.yaml"


def _setforge(
    c: ContainerHandle, args: list[str], *, check: bool = False
) -> tuple[int, str, str]:
    """Run ``uv run setforge <args>`` and return (returncode, stdout, stderr)."""
    result = c.exec(["uv", "run", "setforge", *args], check=check)
    return result.returncode, result.stdout, result.stderr


def _write_local_yaml(c: ContainerHandle, body: str) -> None:
    """Write the host-local local.yaml inside the container."""
    c.write_text(_HOME_LOCAL_YAML, body)


# ---------------------------------------------------------------------------
# Install: plugins.add lands a new plugin into the effective set
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_install_plugins_add_appears_in_effective(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """`plugins.add` of ``name@marketplace`` synthesizes the bare-name
    entry into cfg.claude_plugins so install's plugin reconcile picks
    it up. We assert via the dry-run output (which lists the merged
    effective set) — actually running ``claude`` requires network."""
    c = docker_container()
    _write_local_yaml(
        c,
        "plugins:\n  add:\n    - extra-plugin@claude-plugins-official\n",
    )
    rc, stdout, stderr = _setforge(
        c,
        [
            "install",
            "--profile=test-comprehensive",
            f"--config={CONFIG_FIXTURE}",
            "--dry-run",
        ],
    )
    assert rc == 0, stderr
    assert "extra-plugin@claude-plugins-official" in stdout, stdout


# ---------------------------------------------------------------------------
# Install: plugins.remove drops a plugin from the effective set
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_install_plugins_remove_drops_from_effective(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """`plugins.remove` of a profile-declared plugin drops it from the
    resolved set so the dry-run install output does not list it."""
    c = docker_container()
    _write_local_yaml(
        c,
        "plugins:\n  remove:\n    - superpowers\n",
    )
    rc, stdout, stderr = _setforge(
        c,
        [
            "install",
            "--profile=test-comprehensive",
            f"--config={CONFIG_FIXTURE}",
            "--dry-run",
        ],
    )
    assert rc == 0, stderr
    # WOULD install / enable lines should NOT include superpowers.
    assert "WOULD install  superpowers" not in stdout, stdout
    assert "WOULD enable   superpowers" not in stdout, stdout


# ---------------------------------------------------------------------------
# Install: extensions.add + extensions.remove
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_install_extensions_add_remove(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Both extensions.add and extensions.remove take effect together —
    the dry-run output lists ms-toolsai.jupyter (added) and skips
    editorconfig.editorconfig (removed)."""
    c = docker_container()
    _write_local_yaml(
        c,
        (
            "extensions:\n"
            "  add:\n"
            "    - ms-toolsai.jupyter\n"
            "  remove:\n"
            "    - editorconfig.editorconfig\n"
        ),
    )
    rc, stdout, stderr = _setforge(
        c,
        [
            "install",
            "--profile=test-comprehensive",
            f"--config={CONFIG_FIXTURE}",
            "--dry-run",
        ],
    )
    assert rc == 0, stderr
    assert "ms-toolsai.jupyter" in stdout, stdout
    # The removed extension must not appear as a "WOULD install" / "WOULD ..."
    # line in the dry-run reconcile output:
    assert "WOULD install   editorconfig.editorconfig" not in stdout, stdout


# ---------------------------------------------------------------------------
# Install: marketplaces.add lands a new marketplace
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_install_marketplaces_add_lands(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """`marketplaces.add` lands a new marketplace into cfg.marketplaces;
    the dry-run install output lists it under WOULD add-marketplace."""
    c = docker_container()
    _write_local_yaml(
        c,
        (
            "marketplaces:\n"
            "  add:\n"
            "    work-internal:\n"
            "      source: github\n"
            "      repo: work-corp/claude-plugins\n"
        ),
    )
    rc, stdout, stderr = _setforge(
        c,
        [
            "install",
            "--profile=test-comprehensive",
            f"--config={CONFIG_FIXTURE}",
            "--dry-run",
        ],
    )
    assert rc == 0, stderr
    # Pin the full dry-run verb+name line, not a bare-name substring that
    # could match anywhere in stdout (source emits ``WOULD add-marketplace
    # {name}`` at _install_helpers.py:881).
    assert "WOULD add-marketplace work-internal" in stdout, stdout


# ---------------------------------------------------------------------------
# Install: marketplace cross-ref defensive backstop (validate skipped)
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_install_marketplace_cross_ref_failure_defensive(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Even when `setforge validate` was never run, `setforge install`
    fires the same cross-ref check before mutating live state (Q8
    defensive backstop)."""
    c = docker_container()
    _write_local_yaml(
        c,
        "plugins:\n  add:\n    - rogue-tool@nonexistent-marketplace\n",
    )
    rc, stdout, stderr = _setforge(
        c,
        [
            "install",
            "--profile=test-comprehensive",
            f"--config={CONFIG_FIXTURE}",
            "--dry-run",
        ],
    )
    assert rc != 0
    combined = stdout + stderr
    assert "'rogue-tool'" in combined, combined
    assert "'nonexistent-marketplace'" in combined, combined
