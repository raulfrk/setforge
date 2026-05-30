"""Docker E2E tests for tracked-file orphan detection + cleanup-orphans.

Each scenario runs in a fresh Debian container with real
``setforge`` install/compare/cleanup-orphans/revert side effects.
Gated by ``-m e2e_docker``; skipped when ``docker`` is unavailable.

Named scenarios (per memory ``feedback_docker_e2e_coverage_preference``):

1. ``test_orphan_e2e_compare_surfaces_orphan_after_remove`` —
   install → mutate setforge.yaml to drop the entry → compare
   reports the live file as an orphan.
2. ``test_orphan_e2e_dry_run_default_no_mutation`` — ``cleanup-orphans``
   without ``--apply`` leaves disk untouched, prints ``WOULD delete``.
3. ``test_orphan_e2e_apply_non_tty_no_yes_raises`` — ``--apply``
   without ``--yes`` in a non-TTY exec exits non-zero AND leaves the
   orphan file in place (mutate-gate).
4. ``test_orphan_e2e_apply_yes_deletes_and_writes_transition`` —
   ``--apply --yes`` removes the orphan AND writes a transition record
   under ``~/.local/state/setforge/transitions/``.
5. ``test_orphan_e2e_deploy_remove_cleanup_revert_restored`` — full
   roundtrip: deploy → remove from yaml → cleanup-orphans --apply
   --yes → setforge revert → file restored.
6. ``test_orphan_e2e_ignore_writes_local_yaml_not_tracked`` —
   ``--ignore <id>`` mutates ~/.config/setforge/local.yaml but never
   touches the tracked setforge.yaml.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle

pytestmark = pytest.mark.e2e_docker

_LIVE_MINIMAL = "/home/tester/.setforge_e2e/minimal/text.txt"
_HOST_LOCAL_YAML = "/home/tester/.config/setforge/local.yaml"


def _setforge(
    container: ContainerHandle,
    args: list[str],
    *,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run ``uv run setforge ...`` inside the container."""
    return container.exec(
        ["uv", "run", "setforge", *args],
        check=check,
    )


def _install_minimal(container: ContainerHandle) -> None:
    """Install ``test-minimal`` profile so a tracked deploy exists on disk."""
    result = _setforge(
        container,
        ["install", "--profile=test-minimal", f"--config={CONFIG_FIXTURE}"],
    )
    assert result.returncode == 0, result.stderr


def _orphan_yaml() -> str:
    """A copy of the test config WITHOUT ``minimal_text`` in test-minimal.

    Drops the single tracked_file entry from test-minimal's
    ``tracked_files`` list AND from the top-level ``tracked_files``
    block. Once setforge re-resolves the profile, the previously-
    deployed ``~/.setforge_e2e/minimal/text.txt`` becomes an orphan.
    """
    return (
        "version: 1\n"
        "tracked_files: {}\n"
        "profiles:\n"
        "  test-minimal:\n"
        "    tracked_files: []\n"
    )


# ---------------------------------------------------------------------------
# Scenario 1: compare surfaces orphan
# ---------------------------------------------------------------------------


def test_orphan_e2e_compare_surfaces_orphan_after_remove(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    _install_minimal(c)
    assert c.exec(["test", "-f", _LIVE_MINIMAL], check=False).returncode == 0

    # Install creates a transitions/*/meta.json with the file in paths.
    # Now overwrite setforge.yaml to drop the entry → orphan emerges.
    c.write_text("/workspace/setforge.yaml.orphan", _orphan_yaml())
    result = _setforge(
        c,
        [
            "compare",
            "--profile=test-minimal",
            "--config=/workspace/setforge.yaml.orphan",
        ],
    )
    assert result.returncode == 0, result.stderr
    assert "Orphans" in result.stdout
    assert "text.txt" in result.stdout


# ---------------------------------------------------------------------------
# Scenario 2: dry-run default leaves disk untouched
# ---------------------------------------------------------------------------


def test_orphan_e2e_dry_run_default_no_mutation(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    _install_minimal(c)
    c.write_text("/workspace/setforge.yaml.orphan", _orphan_yaml())

    result = _setforge(
        c,
        [
            "cleanup-orphans",
            "--profile=test-minimal",
            "--config=/workspace/setforge.yaml.orphan",
        ],
    )
    assert result.returncode == 0, result.stderr
    assert "WOULD delete" in result.stdout
    assert "DRY-RUN" in result.stdout
    # Live file untouched.
    assert c.exec(["test", "-f", _LIVE_MINIMAL], check=False).returncode == 0


# ---------------------------------------------------------------------------
# Scenario 3: mutate-gate — non-TTY + no --yes raises
# ---------------------------------------------------------------------------


def test_orphan_e2e_apply_non_tty_no_yes_raises(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    _install_minimal(c)
    c.write_text("/workspace/setforge.yaml.orphan", _orphan_yaml())

    result = _setforge(
        c,
        [
            "cleanup-orphans",
            "--profile=test-minimal",
            "--config=/workspace/setforge.yaml.orphan",
            "--apply",
        ],
    )
    assert result.returncode != 0, result.stdout
    assert "requires --yes" in (result.stderr + result.stdout)
    # Mutate-gate: file remains.
    assert c.exec(["test", "-f", _LIVE_MINIMAL], check=False).returncode == 0


# ---------------------------------------------------------------------------
# Scenario 4: --apply --yes deletes AND writes a transition
# ---------------------------------------------------------------------------


def test_orphan_e2e_apply_yes_deletes_and_writes_transition(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    _install_minimal(c)
    c.write_text("/workspace/setforge.yaml.orphan", _orphan_yaml())

    result = _setforge(
        c,
        [
            "cleanup-orphans",
            "--profile=test-minimal",
            "--config=/workspace/setforge.yaml.orphan",
            "--apply",
            "--yes",
        ],
    )
    assert result.returncode == 0, result.stderr + result.stdout
    # Orphan gone.
    assert c.exec(["test", "-f", _LIVE_MINIMAL], check=False).returncode != 0
    # Transition record present — find a cleanup-orphans transition dir.
    ls = c.exec(
        [
            "bash",
            "-c",
            "ls /home/tester/.local/state/setforge/transitions/ | "
            "grep cleanup-orphans || true",
        ],
        check=False,
    )
    assert "cleanup-orphans" in ls.stdout, ls.stdout


# ---------------------------------------------------------------------------
# Scenario 5: deploy → remove → cleanup → revert → restored
# ---------------------------------------------------------------------------


def test_orphan_e2e_deploy_remove_cleanup_revert_restored(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    _install_minimal(c)
    original_content = c.read_text(_LIVE_MINIMAL)
    assert original_content  # sanity

    c.write_text("/workspace/setforge.yaml.orphan", _orphan_yaml())
    cleanup_result = _setforge(
        c,
        [
            "cleanup-orphans",
            "--profile=test-minimal",
            "--config=/workspace/setforge.yaml.orphan",
            "--apply",
            "--yes",
        ],
    )
    assert cleanup_result.returncode == 0, cleanup_result.stderr
    assert c.exec(["test", "-f", _LIVE_MINIMAL], check=False).returncode != 0

    # Revert the cleanup transition; file restored.
    revert_result = _setforge(
        c,
        [
            "revert",
            "--profile=test-minimal",
            "--config=/workspace/setforge.yaml.orphan",
            "--yes",
        ],
    )
    assert revert_result.returncode == 0, revert_result.stderr + revert_result.stdout
    assert c.exec(["test", "-f", _LIVE_MINIMAL], check=False).returncode == 0
    restored = c.read_text(_LIVE_MINIMAL)
    assert restored == original_content, (
        "content mismatch after revert: "
        f"original={original_content!r}, restored={restored!r}"
    )


# ---------------------------------------------------------------------------
# Scenario 6: --ignore writes to local.yaml only
# ---------------------------------------------------------------------------


def test_orphan_e2e_ignore_writes_local_yaml_not_tracked(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    _install_minimal(c)
    # Snapshot tracked setforge.yaml before --ignore.
    tracked_before = c.read_text("/workspace/tests/fixtures/e2e/setforge.test.yaml")

    result = _setforge(
        c,
        [
            "cleanup-orphans",
            "--profile=test-minimal",
            f"--config={CONFIG_FIXTURE}",
            "--ignore",
            "some_old_id",
        ],
    )
    assert result.returncode == 0, result.stderr

    # Tracked setforge.yaml: byte-identical.
    tracked_after = c.read_text("/workspace/tests/fixtures/e2e/setforge.test.yaml")
    assert tracked_after == tracked_before, (
        "tracked setforge.yaml mutated by --ignore (must be host-local only)"
    )

    # Host-local local.yaml mentions orphan_ignore.
    local_yaml = c.read_text(_HOST_LOCAL_YAML)
    assert "orphan_ignore" in local_yaml
    assert "some_old_id" in local_yaml
