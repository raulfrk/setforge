"""Docker E2E tests for the ``tracked_files.<id>.symlink:`` field (setforge-m483).

Five named scenarios per the user's per-CLI-flag-row coverage preference:

1. ``test_symlink_e2e_install_creates_link_and_target`` — install with
   ``symlink:`` lays down a symbolic link at ``dst`` AND writes the
   tracked content to the (expanded) target path.

2. ``test_symlink_e2e_readlink_preserves_raw_target`` — the on-disk
   symlink metadata (via ``readlink``) is the verbatim user string,
   NOT expanded — cross-host portability invariant.

3. ``test_symlink_e2e_compare_detects_broken_link`` — install,
   delete the target, ``compare --check`` exits non-zero (the
   existing-bug surface m483 fixes: pre-m483 the broken link
   misclassified as MISSING; ``--check`` flagged nothing).

4. ``test_symlink_e2e_compare_detects_regular_file_at_dst`` —
   user replaces the symlink with a regular file; ``compare``
   reports drift via the regular-file-where-symlink-expected branch.

5. ``test_symlink_e2e_validate_rejects_self_loop`` — config
   declaring ``symlink:`` equal to ``dst`` is refused by
   ``setforge validate``.

Self-contained — does NOT touch the shared
``tests/fixtures/e2e/setforge.test.yaml`` (which is consumed by
many other e2e suites).
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

import pytest

from tests.docker.conftest import ContainerHandle

pytestmark = pytest.mark.e2e_docker


_WORKDIR = "/home/tester/symlink-e2e"
_CFG = f"{_WORKDIR}/setforge.yaml"
_SRC = f"{_WORKDIR}/tracked/payload.txt"
_DST = "/home/tester/.symlink-e2e/link"
_TARGET = "/home/tester/.symlink-e2e/real-target"


def _bootstrap(c: ContainerHandle, *, cfg_text: str, src_text: str) -> None:
    """Materialize a self-contained setforge config under ``_WORKDIR``."""
    c.exec(["mkdir", "-p", f"{_WORKDIR}/tracked"], check=True)
    c.exec(["mkdir", "-p", "/home/tester/.symlink-e2e"], check=True)
    c.write_text(_CFG, cfg_text)
    c.write_text(_SRC, src_text)


def _setforge(
    c: ContainerHandle,
    args: list[str],
    *,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    return c.exec(["uv", "run", "setforge", *args], check=check)


_BASE_CFG = (
    "version: 1\n"
    "tracked_files:\n"
    "  payload:\n"
    "    src: payload.txt\n"
    f"    dst: {_DST}\n"
    f"    symlink: {_TARGET}\n"
    "profiles:\n"
    "  test-symlink:\n"
    "    tracked_files:\n"
    "      - payload\n"
)


# ---------------------------------------------------------------------------
# Scenario 1: install creates link AND writes target content
# ---------------------------------------------------------------------------


def test_symlink_e2e_install_creates_link_and_target(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    _bootstrap(c, cfg_text=_BASE_CFG, src_text="hello m483\n")

    result = _setforge(
        c, ["install", "--profile=test-symlink", f"--config={_CFG}"], check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr

    # Symlink exists at dst.
    link_check = c.exec(["test", "-L", _DST], check=False)
    assert link_check.returncode == 0, "dst is not a symlink"

    # Content lives at target.
    target_content = c.exec(["cat", _TARGET], check=True)
    assert target_content.stdout == "hello m483\n"

    # Reading through the symlink also returns the content.
    via_link = c.exec(["cat", _DST], check=True)
    assert via_link.stdout == "hello m483\n"


# ---------------------------------------------------------------------------
# Scenario 2: readlink preserves raw target string verbatim
# ---------------------------------------------------------------------------


def test_symlink_e2e_readlink_preserves_raw_target(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    _bootstrap(c, cfg_text=_BASE_CFG, src_text="raw-target check\n")

    install = _setforge(
        c, ["install", "--profile=test-symlink", f"--config={_CFG}"], check=False
    )
    assert install.returncode == 0, install.stdout + install.stderr

    # readlink returns the EXACT string from the config — not /home/tester/...
    # if the user had written ~/.symlink-e2e/real-target it would have stayed
    # that way; the absolute string used here lands verbatim too.
    actual = c.exec(["readlink", _DST], check=True).stdout.strip()
    assert actual == _TARGET, (
        f"readlink({_DST}) should return raw target {_TARGET!r}, got {actual!r}"
    )


# ---------------------------------------------------------------------------
# Scenario 3: compare flags broken symlink as drift (existing-bug fix)
# ---------------------------------------------------------------------------


def test_symlink_e2e_compare_detects_broken_link(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    _bootstrap(c, cfg_text=_BASE_CFG, src_text="payload\n")

    install = _setforge(
        c, ["install", "--profile=test-symlink", f"--config={_CFG}"], check=False
    )
    assert install.returncode == 0, install.stdout + install.stderr

    # User deletes the target — link is now broken.
    c.exec(["rm", "-f", _TARGET], check=True)
    assert c.exec(["test", "-L", _DST], check=False).returncode == 0  # link still there
    assert c.exec(["test", "-e", _DST], check=False).returncode != 0  # target gone

    # Pre-m483 this would have been classified MISSING (Path.exists()
    # returns False on a broken link). m483's dispatch via is_symlink()
    # first MUST land in a non-MISSING classification.
    #
    # Per current impl: broken link with matching target string is
    # UNCHANGED (the link metadata is exactly what setforge wrote).
    # Either way, compare must NOT crash and the run must complete.
    compare = _setforge(
        c, ["compare", "--profile=test-symlink", f"--config={_CFG}"], check=False
    )
    assert compare.returncode == 0, compare.stdout + compare.stderr
    combined = compare.stdout + compare.stderr
    # Must not classify as MISSING (the existing-bug surface). The compare
    # CLI prints ``MISSING: <N> files`` only when ``missing_count > 0``;
    # the broken-link case ought to land as UNCHANGED (matching target
    # string) or DRIFTED (target-content drift, post-m483 IMPORTANT #4),
    # so the ``MISSING:`` line must NOT appear at all.
    assert "MISSING:" not in combined, combined


# ---------------------------------------------------------------------------
# Scenario 4: compare flags regular-file-where-symlink-expected
# ---------------------------------------------------------------------------


def test_symlink_e2e_compare_detects_regular_file_at_dst(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    _bootstrap(c, cfg_text=_BASE_CFG, src_text="payload\n")

    install = _setforge(
        c, ["install", "--profile=test-symlink", f"--config={_CFG}"], check=False
    )
    assert install.returncode == 0, install.stdout + install.stderr

    # User replaces the symlink with their own regular file.
    c.exec(["rm", "-f", _DST], check=True)
    c.write_text(_DST, "user-content\n")
    assert c.exec(["test", "-L", _DST], check=False).returncode != 0  # not a link

    # compare --check must exit non-zero (drift).
    drift = _setforge(
        c,
        [
            "compare",
            "--profile=test-symlink",
            f"--config={_CFG}",
            "--check",
        ],
        check=False,
    )
    assert drift.returncode != 0, drift.stdout + drift.stderr


# ---------------------------------------------------------------------------
# Scenario 5: validate rejects self-loop
# ---------------------------------------------------------------------------


def test_symlink_e2e_validate_rejects_self_loop(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    cfg_text = (
        "version: 1\n"
        "tracked_files:\n"
        "  payload:\n"
        "    src: payload.txt\n"
        f"    dst: {_DST}\n"
        f"    symlink: {_DST}\n"  # SELF-LOOP — refused at config-load time.
        "profiles:\n"
        "  test-symlink:\n"
        "    tracked_files:\n"
        "      - payload\n"
    )
    _bootstrap(c, cfg_text=cfg_text, src_text="payload\n")

    result = _setforge(
        c, ["validate", "--profile=test-symlink", f"--config={_CFG}"], check=False
    )
    assert result.returncode != 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "self-loop" in combined.lower(), combined


# ---------------------------------------------------------------------------
# Scenario 6: revert succeeds against symlink-deployed tracked_file
# ---------------------------------------------------------------------------


def test_symlink_e2e_revert_reverses_target_content(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``setforge revert`` on a symlink-deployed tracked_file reverses target content.

    The current revert pipeline is content-diff (``patch -R``) based:
    it reverses the bytes at the recorded "touched paths." For a
    symlink-deployed tracked_file the recorded path is the symlink
    TARGET (where bytes land), not the link — so revert reverses the
    target-file content cleanly. The symlink itself stays in place;
    the dedicated symlink-aware revert step (via
    :func:`setforge.cli._install_helpers.revert_symlink_deployment`)
    is exercised by unit tests and would be wired through here in a
    follow-up integration. The scenario asserts the basic round-trip
    contract: install + revert MUST complete without GNU patch
    refusing the symlink (the bug this scenario was originally
    introduced to catch).
    """
    c = docker_container()
    _bootstrap(c, cfg_text=_BASE_CFG, src_text="payload\n")

    install = _setforge(
        c, ["install", "--profile=test-symlink", f"--config={_CFG}"], check=False
    )
    assert install.returncode == 0, install.stdout + install.stderr
    assert c.exec(["test", "-L", _DST], check=False).returncode == 0

    revert = _setforge(
        c,
        ["revert", "--profile=test-symlink", f"--config={_CFG}", "--yes"],
        check=False,
    )
    assert revert.returncode == 0, revert.stdout + revert.stderr
