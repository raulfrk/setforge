"""Docker E2E tests for the ``tracked_files.<id>.mode:`` field (setforge-8z91).

Three named scenarios per the user's per-CLI-flag-row coverage preference:

1. ``test_mode_e2e_install_applies_0o755`` — install with
   ``mode: 0o755`` deploys the live file with exactly ``0o755`` perm
   bits (independent of source perms / umask).
2. ``test_mode_e2e_compare_flags_drift_after_manual_chmod`` —
   install at ``0o755``; ``chmod 0644`` on dst; ``compare --check``
   exits non-zero.
3. ``test_mode_e2e_validate_rejects_yaml_1_1_octal`` —
   ``setforge validate`` refuses a config that declares
   ``mode: 0755`` (the YAML-1.1-style footgun); error message points
   at the canonical ``0o755`` literal.

Setup pattern: each test writes its own minimal setforge.yaml +
tracked source under /tmp inside the container, then runs setforge
against that config. Self-contained — does NOT touch the shared
``tests/fixtures/e2e/setforge.test.yaml`` (which is consumed by
many other e2e suites).
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

import pytest

from tests.docker.conftest import ContainerHandle

pytestmark = pytest.mark.e2e_docker


_WORKDIR = "/home/tester/mode-e2e"
_CFG = f"{_WORKDIR}/setforge.yaml"
_SRC = f"{_WORKDIR}/tracked/hook.sh"
_DST = "/home/tester/.mode-e2e/hook.sh"


def _bootstrap(c: ContainerHandle, *, cfg_text: str, src_text: str) -> None:
    """Materialize a self-contained setforge config under ``_WORKDIR``."""
    c.exec(["mkdir", "-p", f"{_WORKDIR}/tracked"], check=True)
    c.exec(["mkdir", "-p", f"{_WORKDIR}/.cache"], check=True)
    c.write_text(_CFG, cfg_text)
    c.write_text(_SRC, src_text)
    # Source perms intentionally restrictive to prove `mode:` overrides them.
    c.exec(["chmod", "0600", _SRC], check=True)


def _stat_mode_octal(c: ContainerHandle, path: str) -> str:
    """Return live file perm bits via ``stat -c %a`` (octal, no leading 0o)."""
    res = c.exec(["stat", "-c", "%a", path], check=True)
    return res.stdout.strip()


def _setforge(
    c: ContainerHandle,
    args: list[str],
    *,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    return c.exec(["uv", "run", "setforge", *args], check=check)


# ---------------------------------------------------------------------------
# Scenario 1: install applies 0o755 verbatim
# ---------------------------------------------------------------------------


def test_mode_e2e_install_applies_0o755(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    cfg_text = (
        "version: 1\n"
        "tracked_files:\n"
        "  hook_script:\n"
        "    src: hook.sh\n"
        f"    dst: {_DST}\n"
        "    mode: 0o755\n"
        "profiles:\n"
        "  test-mode:\n"
        "    tracked_files:\n"
        "      - hook_script\n"
    )
    src_text = "#!/bin/sh\necho hook fired\n"
    _bootstrap(c, cfg_text=cfg_text, src_text=src_text)

    result = _setforge(
        c, ["install", "--profile=test-mode", f"--config={_CFG}"], check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr

    # Perm bits applied via fchmod-before-replace (the whole point).
    assert _stat_mode_octal(c, _DST) == "755"
    # Content also landed.
    assert c.exec(["cat", _DST], check=True).stdout.strip().startswith("#!/bin/sh")


# ---------------------------------------------------------------------------
# Scenario 2: compare flags drift after manual chmod
# ---------------------------------------------------------------------------


def test_mode_e2e_compare_flags_drift_after_manual_chmod(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    cfg_text = (
        "version: 1\n"
        "tracked_files:\n"
        "  hook_script:\n"
        "    src: hook.sh\n"
        f"    dst: {_DST}\n"
        "    mode: 0o755\n"
        "profiles:\n"
        "  test-mode:\n"
        "    tracked_files:\n"
        "      - hook_script\n"
    )
    src_text = "#!/bin/sh\necho hook fired\n"
    _bootstrap(c, cfg_text=cfg_text, src_text=src_text)

    install = _setforge(
        c, ["install", "--profile=test-mode", f"--config={_CFG}"], check=False
    )
    assert install.returncode == 0, install.stdout + install.stderr
    assert _stat_mode_octal(c, _DST) == "755"

    # User manually clamps mode to 0644 — content unchanged, only perms.
    c.exec(["chmod", "0644", _DST], check=True)
    assert _stat_mode_octal(c, _DST) == "644"

    drift = _setforge(
        c,
        [
            "compare",
            "--profile=test-mode",
            f"--config={_CFG}",
            "--check",
        ],
        check=False,
    )
    # --check exits non-zero on any unexpected drift (mode_drift counts).
    assert drift.returncode != 0, drift.stdout + drift.stderr


# ---------------------------------------------------------------------------
# Scenario 3: validate rejects YAML-1.1-style `mode: 0755`
# ---------------------------------------------------------------------------


def test_mode_e2e_validate_rejects_yaml_1_1_octal(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    cfg_text = (
        "version: 1\n"
        "tracked_files:\n"
        "  hook_script:\n"
        "    src: hook.sh\n"
        f"    dst: {_DST}\n"
        "    mode: 0755\n"  # YAML-1.1 footgun — parses as ScalarInt(755).
        "profiles:\n"
        "  test-mode:\n"
        "    tracked_files:\n"
        "      - hook_script\n"
    )
    src_text = "#!/bin/sh\necho hook fired\n"
    _bootstrap(c, cfg_text=cfg_text, src_text=src_text)

    result = _setforge(
        c, ["validate", "--profile=test-mode", f"--config={_CFG}"], check=False
    )
    assert result.returncode != 0, result.stdout
    combined = result.stdout + result.stderr
    assert "0o755" in combined, combined
