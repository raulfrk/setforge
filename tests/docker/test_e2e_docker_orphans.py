"""Docker E2E tests for tracked-file orphan cleanup.

Each scenario runs in a fresh Debian container with real
``setforge`` install/cleanup-orphans/revert side effects.
Gated by ``-m e2e_docker``; skipped when ``docker`` is unavailable.

Only the behaviour that needs a real process boundary stays here
(per memory ``feedback_docker_e2e_coverage_preference``); the orphan
detection, guard, dry-run, ``--apply --yes`` and ``--ignore`` semantics
are covered by ``tests/test_orphans.py``, ``tests/test_orphans_native.py``
and ``tests/test_orphan_scan_cli.py``.

1. ``test_orphan_e2e_apply_non_tty_no_yes_raises`` — ``--apply``
   without ``--yes`` in a non-TTY exec exits non-zero AND leaves the
   orphan file in place (mutate-gate).
2. ``test_orphan_e2e_deploy_remove_cleanup_revert_restored`` — full
   roundtrip across separate processes: deploy → remove from yaml →
   cleanup-orphans --apply --yes → setforge revert → file restored.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle

pytestmark = pytest.mark.e2e_docker

_LIVE_MINIMAL = "/home/tester/.setforge_e2e/minimal/text.txt"


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
    """Config that DROPS ``minimal_text`` but keeps a sibling tracked_file
    under ``~/.setforge_e2e/``.

    Dropping ``minimal_text`` makes the previously-deployed
    ``~/.setforge_e2e/minimal/text.txt`` an orphan. The retained
    ``keeper`` entry (deploying elsewhere under ``~/.setforge_e2e/``)
    keeps that tree a MANAGED destination root, so orphan detection's
    managed-scope guard still surfaces the orphan — this mirrors the
    realistic "removed one entry, others remain" case (which is exactly
    how the over-reach bug arose). An empty ``tracked_files`` would leave
    setforge managing nothing, in which case the guard correctly declines
    to surface anything. ``keeper`` is absent from the resolved profile
    and was never deployed, so it is not itself an orphan.
    """
    return (
        "schema_version: '6.0'\n"
        "tracked_files:\n"
        "  keeper:\n"
        "    src: json/settings.json\n"
        "    dst: ~/.setforge_e2e/json/settings.json\n"
        "profiles:\n"
        "  test-minimal:\n"
        "    tracked_files: []\n"
    )


# ---------------------------------------------------------------------------
# Scenario 1: mutate-gate — non-TTY + no --yes raises
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
# Scenario 2: deploy → remove → cleanup → revert → restored
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
