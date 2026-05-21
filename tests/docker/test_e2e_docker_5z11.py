"""Docker E2E tests for the local.yaml plugin/extension/marketplace overlay.

Spec: ``setforge-5z11`` / SPEC 2. Exercises mockup output end-to-end
against a real Debian container with the actual installed ``setforge``
CLI:

- ``compare`` emits the per-axis effective-set block with
  ``[from local.yaml]`` / U+2212-prefixed remove tags, plus the
  ``[Host overlay summary: ...]`` footer line.
- ``install`` applies the merged sets so plugin / extension reconcile
  consumes overlay-added entries transparently and drops
  overlay-removed entries; cross-ref check fires defensively at
  install time too.
- ``validate`` surfaces collision / unknown-remove / marketplace
  cross-ref failures with the SPEC 2 message wording.

The eleven required test names enumerated in the spec's acceptance
section live here verbatim so the acceptance grep walk in the spec
finds them by name.
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
    assert "work-internal" in stdout, stdout


# ---------------------------------------------------------------------------
# Validate: add ∩ remove collision -> exit 1 with canonical phrase
# ---------------------------------------------------------------------------


def test_validate_add_intersect_remove_collision_errors(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    _write_local_yaml(
        c,
        ("plugins:\n  add:\n    - superpowers\n  remove:\n    - superpowers\n"),
    )
    rc, stdout, stderr = _setforge(
        c,
        [
            "validate",
            "--profile=test-comprehensive",
            f"--config={CONFIG_FIXTURE}",
        ],
    )
    assert rc != 0
    combined = stdout + stderr
    assert "in both add and remove" in combined, combined
    assert "'superpowers'" in combined, combined


# ---------------------------------------------------------------------------
# Validate: remove not in profile -> exit 1 with canonical phrase
# ---------------------------------------------------------------------------


def test_validate_remove_not_in_profile_errors(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    _write_local_yaml(
        c,
        "plugins:\n  remove:\n    - never-was-there\n",
    )
    rc, stdout, stderr = _setforge(
        c,
        [
            "validate",
            "--profile=test-comprehensive",
            f"--config={CONFIG_FIXTURE}",
        ],
    )
    assert rc != 0
    combined = stdout + stderr
    assert "not in profile-resolved set" in combined, combined
    assert "'never-was-there'" in combined, combined


# ---------------------------------------------------------------------------
# Validate: marketplace cross-ref failure (offline)
# ---------------------------------------------------------------------------


def test_validate_marketplace_cross_ref_failure_offline(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """`validate` runs without docker / claude / code; it must still
    catch the cross-ref failure where plugins.add references a
    marketplace that exists in neither profile nor local.add."""
    c = docker_container()
    _write_local_yaml(
        c,
        "plugins:\n  add:\n    - leaked-tool@undefined-mp\n",
    )
    rc, stdout, stderr = _setforge(
        c,
        [
            "validate",
            "--profile=test-comprehensive",
            f"--config={CONFIG_FIXTURE}",
        ],
    )
    assert rc != 0
    combined = stdout + stderr
    assert "'leaked-tool'" in combined, combined
    assert "'undefined-mp'" in combined, combined
    assert "Available marketplaces" in combined, combined


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


# ---------------------------------------------------------------------------
# Compare: [from local.yaml] tag on adds
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_compare_shows_from_local_yaml_tag_on_adds(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    _write_local_yaml(
        c,
        (
            "plugins:\n"
            "  add:\n"
            "    - some-extra@claude-plugins-official\n"
            "extensions:\n"
            "  add:\n"
            "    - ms-toolsai.jupyter\n"
        ),
    )
    rc, stdout, stderr = _setforge(
        c,
        [
            "compare",
            "--profile=test-comprehensive",
            f"--config={CONFIG_FIXTURE}",
        ],
    )
    assert rc == 0, stderr
    assert "[from local.yaml]" in stdout, stdout
    assert "some-extra@claude-plugins-official [from local.yaml]" in stdout, stdout
    assert "ms-toolsai.jupyter [from local.yaml]" in stdout, stdout


# ---------------------------------------------------------------------------
# Compare: U+2212 remove tag on removes
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_compare_shows_removed_via_local_yaml_tag_on_removes(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    _write_local_yaml(
        c,
        (
            "plugins:\n"
            "  remove:\n"
            "    - superpowers\n"
            "extensions:\n"
            "  remove:\n"
            "    - editorconfig.editorconfig\n"
        ),
    )
    rc, stdout, stderr = _setforge(
        c,
        [
            "compare",
            "--profile=test-comprehensive",
            f"--config={CONFIG_FIXTURE}",
        ],
    )
    assert rc == 0, stderr
    minus = chr(0x2212)
    expected_remove_tag = f"[{minus} removed via local.yaml]"
    assert expected_remove_tag in stdout, stdout
    assert f"{minus} superpowers {expected_remove_tag}" in stdout, stdout
    assert f"{minus} editorconfig.editorconfig {expected_remove_tag}" in stdout, stdout


# ---------------------------------------------------------------------------
# Compare: footer summary line carries correct counts
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_compare_footer_summary_correct_counts(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    _write_local_yaml(
        c,
        (
            "plugins:\n"
            "  add:\n"
            "    - p-add@claude-plugins-official\n"
            "  remove:\n"
            "    - superpowers\n"
            "extensions:\n"
            "  add:\n"
            "    - ms-toolsai.jupyter\n"
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
            "compare",
            "--profile=test-comprehensive",
            f"--config={CONFIG_FIXTURE}",
        ],
    )
    assert rc == 0, stderr
    minus = chr(0x2212)
    expected_summary = (
        f"[Host overlay summary: plugins 1+/1{minus}; "
        f"extensions 1+/0{minus}; marketplaces 1+/0{minus} via local.yaml]"
    )
    assert expected_summary in stdout, stdout
