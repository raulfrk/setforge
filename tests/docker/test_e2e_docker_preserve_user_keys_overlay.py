"""Docker E2E tests for the local.yaml preserve_user_keys overlay (setforge-lgvp).

Exercises mockup-B output (SPEC 8) end-to-end against a real Debian
container with the actual installed ``setforge`` CLI:

- ``compare`` emits the ``=== applying host overlay ===`` block with
  per-key provenance lines tagged ``[from local.yaml]`` / ``[from
  profile X]`` / ``[removed via local.yaml]``.
- ``install`` emits the ``preserved keys (N effective)`` block per
  tracked_file with the auditable ``✗`` row for keys removed via the
  overlay.
- Validation failures (add∩remove, remove-of-unknown-key) surface as
  setforge exit-1 with the canonical error phrase.

Profile under exercise: ``test-jsonc-shallow`` (declares
``preserve_user_keys: [userKeyA, userKeyB]`` per the e2e fixture in
:data:`tests.docker.conftest.CONFIG_FIXTURE`).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle

pytestmark = pytest.mark.e2e_docker

_HOME_LOCAL_YAML = "/home/tester/.config/setforge/local.yaml"


def _write_local_yaml_overlay(
    c: ContainerHandle, *, add: list[str], remove: list[str]
) -> None:
    """Write a ``preserve_user_keys`` overlay for ``jsonc_shallow`` to local.yaml.

    The fixture's ``jsonc_shallow`` tracked_file already carries
    ``preserve_user_keys: [userKeyA, userKeyB]``; the overlay adds /
    removes keys against that base set.
    """
    add_lines = "\n".join(f"        - {item!r}" for item in add)
    remove_lines = "\n".join(f"        - {item!r}" for item in remove)
    body = "tracked_files:\n  jsonc_shallow:\n    preserve_user_keys:\n"
    if add:
        body += "      add:\n" + add_lines + "\n"
    if remove:
        body += "      remove:\n" + remove_lines + "\n"
    c.write_text(_HOME_LOCAL_YAML, body)


def _setforge(
    c: ContainerHandle, args: list[str], *, check: bool = False
) -> tuple[int, str, str]:
    """Run ``uv run setforge <args>`` and return (returncode, stdout, stderr)."""
    result = c.exec(["uv", "run", "setforge", *args], check=check)
    return result.returncode, result.stdout, result.stderr


def test_compare_emits_host_overlay_block_with_add_and_remove(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """End-to-end: compare prints the mockup-B host-overlay block.

    With ``add: [userKeyC]`` + ``remove: [userKeyA]``, the compare
    output must include the header, the affected-file count, the
    per-tracked_file effective set, and three per-key provenance
    lines (one each for FROM_LOCAL_YAML / FROM_PROFILE / REMOVED).
    """
    c = docker_container()
    # First install to materialize the live file (compare without a live
    # file produces MISSING entries, which are not the surface under test).
    _setforge(
        c,
        [
            "install",
            "--profile=test-jsonc-shallow",
            f"--config={CONFIG_FIXTURE}",
        ],
        check=True,
    )
    # Now seed the overlay and run compare.
    _write_local_yaml_overlay(c, add=["userKeyC"], remove=["userKeyA"])
    rc, stdout, stderr = _setforge(
        c,
        [
            "compare",
            "--profile=test-jsonc-shallow",
            f"--config={CONFIG_FIXTURE}",
        ],
    )
    assert rc == 0, stderr
    assert "=== applying host overlay (~/.config/setforge/local.yaml) ===" in stdout, (
        stdout
    )
    assert "tracked_files overlays: 1 file affected" in stdout, stdout
    assert "jsonc_shallow:" in stdout, stdout
    assert "preserve_user_keys effective set:" in stdout, stdout
    # FROM_LOCAL_YAML — added key.
    assert "userKeyC" in stdout, stdout
    assert "[from local.yaml]" in stdout, stdout
    # FROM_PROFILE — kept profile key (userKeyB was not removed).
    assert "userKeyB" in stdout, stdout
    assert "[from profile test-jsonc-shallow]" in stdout, stdout
    # REMOVED_VIA_LOCAL — removed profile key.
    assert "userKeyA" in stdout, stdout
    assert "[removed via local.yaml]" in stdout, stdout


def test_install_emits_preserved_keys_block_per_tracked_file(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """End-to-end: install prints the mockup-B preserved-keys block.

    With ``add: [userKeyExtra]`` against the test-jsonc-shallow
    profile's ``[userKeyA, userKeyB]`` base, install must echo:

    - The deploy action line for the tracked_file (existing behavior).
    - A ``preserved keys (3 effective):`` sub-block listing the three
      effective keys with their provenance tags.
    """
    c = docker_container()
    _write_local_yaml_overlay(c, add=["userKeyExtra"], remove=[])
    rc, stdout, stderr = _setforge(
        c,
        [
            "install",
            "--profile=test-jsonc-shallow",
            f"--config={CONFIG_FIXTURE}",
        ],
    )
    assert rc == 0, stderr
    assert "preserved keys (3 effective):" in stdout, stdout
    assert "userKeyA" in stdout, stdout
    assert "userKeyB" in stdout, stdout
    assert "userKeyExtra" in stdout, stdout
    assert "[from local.yaml]" in stdout, stdout
    assert "[from profile test-jsonc-shallow]" in stdout, stdout


def test_install_shows_removed_via_local_audit_row(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """End-to-end: removed-via-local rows render the auditable ✗ row.

    Mockup B specifies the ``✗ key  [removed via local.yaml —
    overwritten with tracked value]`` line to surface what WOULD have
    been preserved had the overlay not removed it. This is the
    user-visible artifact of the auditability acceptance criterion.
    """
    c = docker_container()
    _write_local_yaml_overlay(c, add=[], remove=["userKeyA"])
    rc, stdout, stderr = _setforge(
        c,
        [
            "install",
            "--profile=test-jsonc-shallow",
            f"--config={CONFIG_FIXTURE}",
        ],
    )
    assert rc == 0, stderr
    # ✗ marker + key + removal tag with overwrite explanation per mockup B.
    assert "✗ userKeyA" in stdout, stdout
    assert "[removed via local.yaml — overwritten with tracked value]" in stdout, stdout
    # Effective count reflects the removal (userKeyB only).
    assert "preserved keys (1 effective):" in stdout, stdout


def test_no_overlay_emits_no_host_overlay_block(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Anti-smell guard: absent local.yaml overlay → no mockup-B block.

    Confirms the overlay = identity path (today's behavior preserved
    when ``local.yaml`` has no ``tracked_files:`` entry). The host
    overlay header and the per-file provenance block both stay out
    of the compare output.
    """
    c = docker_container()
    _setforge(
        c,
        [
            "install",
            "--profile=test-jsonc-shallow",
            f"--config={CONFIG_FIXTURE}",
        ],
        check=True,
    )
    # No local.yaml overlay seeded. Compare should not emit the block.
    rc, stdout, stderr = _setforge(
        c,
        [
            "compare",
            "--profile=test-jsonc-shallow",
            f"--config={CONFIG_FIXTURE}",
        ],
    )
    assert rc == 0, stderr
    assert "=== applying host overlay" not in stdout, stdout
    assert "preserve_user_keys effective set" not in stdout, stdout


def test_install_with_collision_overlay_fails_cleanly(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """End-to-end: add∩remove collision exits non-zero with the canonical phrase.

    Lgvp's resolver raises :class:`PreserveUserKeysOverlayError`
    (a :class:`ConfigError` subclass) which surfaces through setforge's
    global error handler as ``error: ...`` + exit 1. tmln will later
    upgrade this to the mockup-D format; today's surface is the bare
    error phrase.
    """
    c = docker_container()
    _write_local_yaml_overlay(c, add=["userKeyX"], remove=["userKeyX"])
    rc, _stdout, stderr = _setforge(
        c,
        [
            "install",
            "--profile=test-jsonc-shallow",
            f"--config={CONFIG_FIXTURE}",
        ],
    )
    assert rc != 0
    assert "in both add and remove" in stderr, stderr
    assert "'userKeyX'" in stderr, stderr


def test_install_with_unknown_remove_fails_cleanly(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """End-to-end: removing a key absent from the profile chain exits non-zero.

    Anti-smell guard: must NOT silently overlay an unrecognized
    ``remove`` entry — surface the typo immediately with the canonical
    "not in profile chain" phrase so tmln's setforge-validate can
    upgrade the formatting later.
    """
    c = docker_container()
    _write_local_yaml_overlay(c, add=[], remove=["nonexistentKey"])
    rc, _stdout, stderr = _setforge(
        c,
        [
            "install",
            "--profile=test-jsonc-shallow",
            f"--config={CONFIG_FIXTURE}",
        ],
    )
    assert rc != 0
    assert "not in profile chain" in stderr, stderr
    assert "'nonexistentKey'" in stderr, stderr
