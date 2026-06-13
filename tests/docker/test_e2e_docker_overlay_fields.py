"""Docker E2E tests for the host-local ``mode`` / ``dst`` /
``symlink_target`` overlay on ``_LocalTrackedFileOverlay``.

Eight named scenarios, one per overlay behavior surface — plus two
follow-up scenarios (9, 10) lifting the sync no-capture and revert
contracts
from implied (no-diff on sync/revert) to explicitly asserted.

Setup pattern mirrors :mod:`tests.docker.test_e2e_docker_file_mode`:
each test writes its own minimal setforge.yaml + tracked source under
``/tmp`` inside the container, then writes a ``local.yaml`` with the
overlay block and runs setforge against that config. Self-contained —
does NOT touch the shared ``tests/fixtures/e2e/setforge.test.yaml``
(consumed by many other e2e suites).

All tests are tagged ``@pytest.mark.xdist_group("docker_daemon")`` so
the parallel xdist runner serializes them onto the single docker
daemon shared across workers (matches the convention adopted by
``test_e2e_docker_local_overlay.py`` / ``test_e2e_docker_auto_promote.py``).
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

import pytest

from tests.docker.conftest import ContainerHandle

pytestmark = [
    pytest.mark.e2e_docker,
    pytest.mark.xdist_group("docker_daemon"),
]


_WORKDIR = "/home/tester/overlay-fields-e2e"
_CFG = f"{_WORKDIR}/setforge.yaml"
_SRC = f"{_WORKDIR}/tracked/hook.sh"
_DEFAULT_DST = "/home/tester/.overlay-fields-e2e/hook.sh"
_LOCAL_YAML = "/home/tester/.config/setforge/local.yaml"


_BASE_CFG = (
    "version: 1\n"
    "tracked_files:\n"
    "  hook_script:\n"
    "    src: hook.sh\n"
    f"    dst: {_DEFAULT_DST}\n"
    "profiles:\n"
    "  test-overlay-fields:\n"
    "    tracked_files:\n"
    "      - hook_script\n"
)
_BASE_SRC = "#!/bin/sh\necho hook fired\n"


def _bootstrap(
    c: ContainerHandle, *, cfg_text: str = _BASE_CFG, src_text: str = _BASE_SRC
) -> None:
    """Materialize a self-contained setforge config under ``_WORKDIR``."""
    c.exec(["mkdir", "-p", f"{_WORKDIR}/tracked"], check=True)
    c.exec(["mkdir", "-p", f"{_WORKDIR}/.cache"], check=True)
    c.write_text(_CFG, cfg_text)
    c.write_text(_SRC, src_text)
    # Source perms intentionally restrictive to prove the overlay-fields mode
    # override actually drives the chmod (not source perms).
    c.exec(["chmod", "0600", _SRC], check=True)


def _write_local_yaml(c: ContainerHandle, body: str) -> None:
    c.write_text(_LOCAL_YAML, body)


def _stat_mode_octal(c: ContainerHandle, path: str) -> str:
    """Return live file perm bits via ``stat -c %a`` (octal, no leading 0o)."""
    res = c.exec(["stat", "-c", "%a", path], check=True)
    return res.stdout.strip()


def _sha256(c: ContainerHandle, path: str) -> str:
    """Return the hex sha256 of a container file (byte-identity probe)."""
    res = c.exec(["sha256sum", path], check=True)
    return res.stdout.split()[0]


def _setforge(
    c: ContainerHandle, args: list[str], *, check: bool = False
) -> subprocess.CompletedProcess[str]:
    return c.exec(["uv", "run", "setforge", *args], check=check)


# ---------------------------------------------------------------------------
# Scenario 1: install applies host-local chmod
# ---------------------------------------------------------------------------


def test_install_applies_host_local_mode_chmod(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``local.yaml`` declares ``mode: 0o755``; install chmods the live dst."""
    c = docker_container()
    _bootstrap(c)
    _write_local_yaml(
        c,
        "tracked_files:\n  hook_script:\n    mode: 0o755\n",
    )
    result = _setforge(
        c, ["install", "--profile=test-overlay-fields", f"--config={_CFG}"]
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _stat_mode_octal(c, _DEFAULT_DST) == "755"


# ---------------------------------------------------------------------------
# Scenario 2: install retargets dst via host-local override
# ---------------------------------------------------------------------------


def test_install_applies_host_local_dst_retarget(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``local.yaml`` declares ``dst: /home/tester/.overlay-fields-alt/hook.sh``;
    install lands content there instead of the profile-side dst."""
    c = docker_container()
    _bootstrap(c)
    retarget_dst = "/home/tester/.overlay-fields-alt/hook.sh"
    _write_local_yaml(
        c,
        f"tracked_files:\n  hook_script:\n    dst: {retarget_dst}\n",
    )
    result = _setforge(
        c, ["install", "--profile=test-overlay-fields", f"--config={_CFG}"]
    )
    assert result.returncode == 0, result.stdout + result.stderr

    # Override lands at the retargeted path; the profile-side dst stays absent.
    body = c.exec(["cat", retarget_dst], check=True).stdout
    assert body.startswith("#!/bin/sh")
    missing = c.exec(["test", "-e", _DEFAULT_DST], check=False)
    assert missing.returncode != 0, "profile-side dst should not have been written"


# ---------------------------------------------------------------------------
# Scenario 3: install creates symlink when symlink_target is set
# ---------------------------------------------------------------------------


def test_install_creates_symlink_when_symlink_target_set(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``local.yaml`` declares ``symlink_target: <path>``; install
    creates a symlink at the tracked dst pointing at that target."""
    c = docker_container()
    _bootstrap(c)
    target = "/home/tester/.overlay-fields-target/hook.sh"
    _write_local_yaml(
        c,
        f"tracked_files:\n  hook_script:\n    symlink_target: {target}\n",
    )
    result = _setforge(
        c, ["install", "--profile=test-overlay-fields", f"--config={_CFG}"]
    )
    assert result.returncode == 0, result.stdout + result.stderr

    # dst is a symlink; readlink yields the raw target string.
    link = c.exec(["readlink", _DEFAULT_DST], check=True).stdout.strip()
    assert link == target, link
    # Tracked content lives at the target path (existing symlink: contract).
    body = c.exec(["cat", _DEFAULT_DST], check=True).stdout
    assert body.startswith("#!/bin/sh"), body


# ---------------------------------------------------------------------------
# Scenario 4: install refuses when symlink dst is a directory
# ---------------------------------------------------------------------------


def test_install_fails_when_symlink_target_dst_is_directory(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Pre-existing directory at the tracked dst refuses symlink
    install with a targeted error (no silent clobber, no recursion)."""
    c = docker_container()
    _bootstrap(c)
    # Pre-create a directory at the dst path so install hits the
    # directory-at-dst refusal branch in deploy_symlinked_file.
    c.exec(["rm", "-f", _DEFAULT_DST], check=False)
    c.exec(["mkdir", "-p", _DEFAULT_DST], check=True)

    target = "/home/tester/.overlay-fields-target/hook.sh"
    _write_local_yaml(
        c,
        f"tracked_files:\n  hook_script:\n    symlink_target: {target}\n",
    )
    result = _setforge(
        c, ["install", "--profile=test-overlay-fields", f"--config={_CFG}"], check=False
    )
    assert result.returncode != 0, result.stdout
    combined = result.stdout + result.stderr
    assert "directory" in combined, combined


# ---------------------------------------------------------------------------
# Scenario 5: install refuses regular file collision when deploying symlink
# ---------------------------------------------------------------------------


def test_install_refuses_regular_file_collision_with_symlink(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Pre-existing regular file at the tracked dst is refused —
    matches the existing ``symlink:`` field's refusal contract
    (move-aside-first discipline; no silent clobber)."""
    c = docker_container()
    _bootstrap(c)
    # Pre-stage a regular file at the dst path. Per deploy_symlinked_file's
    # contract, this refuses with a regular-file-collision error (NOT a
    # silent clobber). The overlay-fields symlink_target overlay rides through the
    # same code path, so it inherits this safety.
    c.exec(["mkdir", "-p", "/home/tester/.overlay-fields-e2e"], check=True)
    c.write_text(_DEFAULT_DST, "PREEXISTING USER CONTENT\n")

    target = "/home/tester/.overlay-fields-target/hook.sh"
    _write_local_yaml(
        c,
        f"tracked_files:\n  hook_script:\n    symlink_target: {target}\n",
    )
    result = _setforge(
        c, ["install", "--profile=test-overlay-fields", f"--config={_CFG}"], check=False
    )
    assert result.returncode != 0, result.stdout
    combined = result.stdout + result.stderr
    assert "regular file" in combined, combined
    # The pre-existing user content survives the refusal.
    body = c.exec(["cat", _DEFAULT_DST], check=True).stdout
    assert "PREEXISTING USER CONTENT" in body


# ---------------------------------------------------------------------------
# Scenario 6: dangling symlink target — install accepted, content
# written at target, symlink lands
# ---------------------------------------------------------------------------


def test_install_accepts_dangling_symlink_target(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``symlink_target`` pointing into a not-yet-existing parent
    directory is accepted at install — deploy creates the parent and
    writes the tracked content there, then drops the symlink at dst.

    This is the existing ``symlink:`` contract carried through the
    new overlay-fields host-local channel: deploy WRITES the target so the link
    is never dangling immediately after install (the "dangling at
    install time" worry in the spec is informational — by the end
    of install the target carries the tracked bytes)."""
    c = docker_container()
    _bootstrap(c)
    target = "/home/tester/.overlay-fields-fresh/nested/hook.sh"
    _write_local_yaml(
        c,
        f"tracked_files:\n  hook_script:\n    symlink_target: {target}\n",
    )
    result = _setforge(
        c, ["install", "--profile=test-overlay-fields", f"--config={_CFG}"]
    )
    assert result.returncode == 0, result.stdout + result.stderr

    # The symlink lands at dst; the target now exists with tracked content.
    link = c.exec(["readlink", _DEFAULT_DST], check=True).stdout.strip()
    assert link == target
    body = c.exec(["cat", target], check=True).stdout
    assert body.startswith("#!/bin/sh"), body


# ---------------------------------------------------------------------------
# Scenario 7: typo'd local.yaml field rejected by extra=forbid
# ---------------------------------------------------------------------------


def test_validate_rejects_typo_in_host_local_overlay(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``mode`` → ``modee`` typo in ``local.yaml`` is caught by the
    overlay's ``_STRICT`` (``extra='forbid'``) — validate surfaces a
    pydantic-style "Extra inputs are not permitted" message."""
    c = docker_container()
    _bootstrap(c)
    _write_local_yaml(
        c,
        "tracked_files:\n  hook_script:\n    modee: 0o755\n",  # typo
    )
    result = _setforge(
        c,
        ["validate", "--profile=test-overlay-fields", f"--config={_CFG}"],
        check=False,
    )
    assert result.returncode != 0, result.stdout
    combined = result.stdout + result.stderr
    # Pydantic v2 phrase for extra='forbid'.
    assert "Extra inputs are not permitted" in combined or "modee" in combined, combined


# ---------------------------------------------------------------------------
# Scenario 8: validate rejects mode + symlink_target combination
# ---------------------------------------------------------------------------


def test_validate_rejects_mode_and_symlink_target_together(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """The overlay's ``_validate_host_local_overrides`` model-validator
    refuses ``mode`` + ``symlink_target`` together (chmod-on-symlink
    follows the link — footgun semantics)."""
    c = docker_container()
    _bootstrap(c)
    _write_local_yaml(
        c,
        "tracked_files:\n"
        "  hook_script:\n"
        "    mode: 0o755\n"
        "    symlink_target: /home/tester/.overlay-fields-other/hook.sh\n",
    )
    result = _setforge(
        c,
        ["validate", "--profile=test-overlay-fields", f"--config={_CFG}"],
        check=False,
    )
    assert result.returncode != 0, result.stdout
    combined = result.stdout + result.stderr
    assert "mutually exclusive" in combined, combined


# ---------------------------------------------------------------------------
# Scenario 9: sync does NOT capture the host-local mode into tracked
# ---------------------------------------------------------------------------


def test_sync_does_not_capture_host_local_mode_into_tracked(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Install with a host-local ``mode: 0o755`` overlay, then sync —
    nothing on the tracked side absorbs the host-local override.

    The overlay is per-host by design: sync's capture pass folds the
    overlay (so it reads the right live state) but must never write the
    override back into the shared config repo. Asserted by byte
    identity: the config repo's ``setforge.yaml`` hash, the tracked
    src's hash, AND the tracked src's perm bits (0600 from
    ``_bootstrap``) are all unchanged after sync, while the live dst
    carries the overlay's 755.
    """
    c = docker_container()
    _bootstrap(c)
    _write_local_yaml(
        c,
        "tracked_files:\n  hook_script:\n    mode: 0o755\n",
    )
    result = _setforge(
        c, ["install", "--profile=test-overlay-fields", f"--config={_CFG}"]
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _stat_mode_octal(c, _DEFAULT_DST) == "755"

    yaml_pre = _sha256(c, _CFG)
    src_pre = _sha256(c, _SRC)
    assert _stat_mode_octal(c, _SRC) == "600"

    result = _setforge(
        c,
        [
            "sync",
            "--profile=test-overlay-fields",
            f"--config={_CFG}",
            "--auto=use-live",
            "--yes",
        ],
    )
    assert result.returncode == 0, result.stdout + result.stderr

    # The host-local mode did NOT leak into the config repo: the
    # tracked YAML and the tracked src are byte-identical, and the
    # src's perm bits did not absorb the live 755.
    assert _sha256(c, _CFG) == yaml_pre
    assert _sha256(c, _SRC) == src_pre
    assert _stat_mode_octal(c, _SRC) == "600"


# ---------------------------------------------------------------------------
# Scenario 10: revert rolls back the deploy state of host-local overrides
# ---------------------------------------------------------------------------


_REVERT_TOOL_SRC = f"{_WORKDIR}/tracked/tool.sh"
_REVERT_TOOL_DST = "/home/tester/.overlay-fields-e2e/tool.sh"

_REVERT_CFG = (
    "version: 1\n"
    "tracked_files:\n"
    "  hook_script:\n"
    "    src: hook.sh\n"
    f"    dst: {_DEFAULT_DST}\n"
    "  tool_script:\n"
    "    src: tool.sh\n"
    f"    dst: {_REVERT_TOOL_DST}\n"
    "profiles:\n"
    "  test-overlay-fields:\n"
    "    tracked_files:\n"
    "      - hook_script\n"
    "      - tool_script\n"
)


def test_revert_after_install_with_host_local_overrides(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Install with host-local ``mode`` + ``symlink_target`` overrides,
    then revert — the live deploy state rolls back.

    Pins the CURRENT revert contract (live bytes / mode / symlink):

    - ``tool_script`` (``mode: 0o755`` overlay, dst absent pre-install):
      revert removes the created file, which IS the chmod rollback —
      ``stat`` on the dst fails because the 755 file is gone.
    - ``hook_script`` (``symlink_target`` overlay, dst absent
      pre-install): the transition records the symlink's TARGET as the
      touched path, so ``patch -R`` removes the target file's content.
      The link OBJECT at dst is ALSO removed: revert folds the
      host-local overlay (``apply_host_local_tracked_file_overrides``)
      before its symlink-unlink pass, so the overlay-declared link is
      visible and gets unlinked — symmetric with a tracked-side
      ``symlink:`` declaration (see ``test_e2e_docker_symlinks``).
    """
    c = docker_container()
    _bootstrap(c, cfg_text=_REVERT_CFG)
    c.write_text(_REVERT_TOOL_SRC, "#!/bin/sh\necho tool fired\n")
    c.exec(["chmod", "0600", _REVERT_TOOL_SRC], check=True)

    target = "/home/tester/.overlay-fields-target/hook.sh"
    _write_local_yaml(
        c,
        "tracked_files:\n"
        "  hook_script:\n"
        f"    symlink_target: {target}\n"
        "  tool_script:\n"
        "    mode: 0o755\n",
    )
    result = _setforge(
        c, ["install", "--profile=test-overlay-fields", f"--config={_CFG}"]
    )
    assert result.returncode == 0, result.stdout + result.stderr

    # Post-install sanity: both overrides landed.
    assert _stat_mode_octal(c, _REVERT_TOOL_DST) == "755"
    link = c.exec(["readlink", _DEFAULT_DST], check=True).stdout.strip()
    assert link == target, link
    assert c.exec(["cat", target], check=True).stdout.startswith("#!/bin/sh")

    result = _setforge(
        c,
        [
            "revert",
            "--profile=test-overlay-fields",
            f"--config={_CFG}",
            "--yes",
        ],
    )
    assert result.returncode == 0, result.stdout + result.stderr

    # The mode-overlaid file was created from absence: revert removes
    # it outright (the 755 file no longer exists to stat).
    assert c.exec(["test", "-e", _REVERT_TOOL_DST], check=False).returncode != 0

    # The symlink TARGET's content rolled back to absence...
    assert c.exec(["test", "-e", target], check=False).returncode != 0
    # ...and the link object at dst is removed too: the revert path now
    # folds the host-local overlay, so the overlay-declared link is
    # unlinked rather than left dangling.
    assert c.exec(["test", "-e", _DEFAULT_DST], check=False).returncode != 0
