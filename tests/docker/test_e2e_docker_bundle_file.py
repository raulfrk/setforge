"""Docker E2E tests for a bundle ``file`` component.

A bundle ``file`` component expands into a synthetic tracked-file and
deploys via the normal tracked-file path: it lands at its ``dst`` with
the declared ``mode`` (``+x`` for ``0o755``) and rides the keep-live
reconcile default so a live hand-edit survives a re-install.

Two happy-path scenarios (the confinement / collision rejection paths
are already covered by the CLI-level tests
``tests/test_install_bundle_file.py`` and
``tests/test_cli_validate_bundle_file.py`` — the container-level value
here is the real chmod + real re-install keep-live round-trip):

1. ``test_bundle_file_e2e_deploys_executable`` — install lays the
   launcher down at its plugin-data ``dst`` with the executable bit set.

2. ``test_bundle_file_e2e_hand_edit_survives_reinstall`` — a live edit
   to the deployed launcher survives a second ``install`` (host-local
   keep-live default for the synthetic tracked-file).

Self-contained — does NOT touch the shared
``tests/fixtures/e2e/setforge.test.yaml``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

import pytest

from tests.docker.conftest import ContainerHandle

pytestmark = pytest.mark.e2e_docker


_WORKDIR = "/home/tester/bundle-file-e2e"
_CFG = f"{_WORKDIR}/setforge.yaml"
_SRC = f"{_WORKDIR}/tracked/launch.sh"
# A plugin data dir path — the motivating real-world dst for a launcher.
_DST = "/home/tester/.claude/plugins/data/revdiff/scripts/launch.sh"

_LAUNCHER_BODY = "#!/bin/sh\necho revdiff launcher\n"

# The ``file`` component src is repo-relative to the config's ``tracked/``
# tree (``launch.sh`` → ``{_WORKDIR}/tracked/launch.sh``), exactly as a
# plain tracked-file src resolves.
_CFG_TEXT = (
    "version: 1\n"
    "tracked_files: {}\n"
    "bundles:\n"
    "  revdiff:\n"
    "    components:\n"
    "      - id: launcher\n"
    "        file:\n"
    "          src: launch.sh\n"
    f"          dst: {_DST}\n"
    "          mode: 0o755\n"
    "profiles:\n"
    "  test-bundle-file:\n"
    "    bundles:\n"
    "      - revdiff\n"
)


def _bootstrap(c: ContainerHandle) -> None:
    """Materialize a self-contained setforge config under ``_WORKDIR``."""
    c.exec(["mkdir", "-p", f"{_WORKDIR}/tracked"], check=True)
    c.write_text(_CFG, _CFG_TEXT)
    c.write_text(_SRC, _LAUNCHER_BODY)


def _setforge(
    c: ContainerHandle,
    args: list[str],
    *,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    return c.exec(["uv", "run", "setforge", *args], check=check)


def _install(c: ContainerHandle) -> subprocess.CompletedProcess[str]:
    return _setforge(
        c, ["install", "--profile=test-bundle-file", f"--config={_CFG}"], check=False
    )


# ---------------------------------------------------------------------------
# Scenario 1: install deploys the launcher executable at its dst
# ---------------------------------------------------------------------------


def test_bundle_file_e2e_deploys_executable(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    _bootstrap(c)

    result = _install(c)
    assert result.returncode == 0, result.stdout + result.stderr

    # The launcher lands at its plugin-data dst with the tracked content.
    assert c.exec(["test", "-f", _DST], check=False).returncode == 0, "dst missing"
    assert c.exec(["cat", _DST], check=True).stdout == _LAUNCHER_BODY

    # mode 0o755 → the executable bit must be set (``test -x``).
    assert c.exec(["test", "-x", _DST], check=False).returncode == 0, (
        "launcher is not executable"
    )
    # Pin the exact permission bits, not just the +x class.
    perms = c.exec(["stat", "-c", "%a", _DST], check=True).stdout.strip()
    assert perms == "755", f"expected mode 755, got {perms}"


# ---------------------------------------------------------------------------
# Scenario 2: a live hand-edit survives re-install (keep-live default)
# ---------------------------------------------------------------------------


def test_bundle_file_e2e_hand_edit_survives_reinstall(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    _bootstrap(c)

    first = _install(c)
    assert first.returncode == 0, first.stdout + first.stderr

    # User hand-edits the deployed launcher.
    edited_body = "#!/bin/sh\necho EDITED launcher\n"
    c.write_text(_DST, edited_body)

    # A second install must NOT clobber the live edit (the synthetic
    # tracked-file rides the host-local keep-live reconcile default).
    second = _install(c)
    assert second.returncode == 0, second.stdout + second.stderr

    survived = c.exec(["cat", _DST], check=True).stdout
    assert "EDITED" in survived, f"hand-edit clobbered by re-install: {survived!r}"
