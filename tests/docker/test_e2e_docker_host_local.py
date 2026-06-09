"""Docker e2e tests for host-local user-sections via local.yaml.

Exercises the full host-local surface end-to-end against a fresh Debian
container with the actual installed ``setforge`` CLI:

- install with each of the 5 anchor kinds.
- error cases: anchor-not-found aborts install, empty body rejected at
  validate, non-markdown tracked_files rejected at validate.
- idempotency: re-running install does not duplicate marker pairs.
- compare overlay-aware behaviour: injected sections do NOT show as drift.
- sync capture-back filter: live host-local sections are NOT written back
  to tracked.
- revert: undoes the injection.
- symlink-deployed tracked_files: injection lands on the target file.
- validate offline gate: anchor-not-found surfaces without touching the
  filesystem.

Profiles under exercise: ``test-host-local`` / ``test-host-local-symlink`` /
``test-host-local-reject-json`` (declared in
``tests/fixtures/e2e/setforge.test.yaml``).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle

pytestmark = pytest.mark.e2e_docker

_HOME_LOCAL_YAML = "/home/tester/.config/setforge/local.yaml"
_HOST_LIVE = "/home/tester/.setforge_e2e/host-local/host.md"
_HOST_LINK_LIVE = "/home/tester/.setforge_e2e/host-local/host_link.md"
_HOST_LINK_TARGET = "/home/tester/.setforge_e2e/host-local/host_link_target.md"
_SETTINGS_JSON_LIVE = "/home/tester/.setforge_e2e/host-local/settings.json"


def _write_local_yaml(c: ContainerHandle, body: str) -> None:
    c.write_text(_HOME_LOCAL_YAML, body)


def _setforge(
    c: ContainerHandle, args: list[str], *, check: bool = False
) -> tuple[int, str, str]:
    result = c.exec(["uv", "run", "setforge", *args], check=check)
    return result.returncode, result.stdout, result.stderr


def _install_host_local(
    c: ContainerHandle, *, profile: str = "test-host-local", check: bool = False
) -> tuple[int, str, str]:
    return _setforge(
        c,
        ["install", f"--profile={profile}", f"--config={CONFIG_FIXTURE}"],
        check=check,
    )


# ---------------------------------------------------------------------------
# Happy path — one test per anchor kind (5 tests).
# ---------------------------------------------------------------------------


def test_install_host_local_after_heading_anchor(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """after-heading anchor splices below the matched heading."""
    c = docker_container()
    _write_local_yaml(
        c,
        "tracked_files:\n"
        "  host_local_md:\n"
        "    host_local_sections:\n"
        "      work-overrides:\n"
        "        anchor: {kind: after-heading, value: Workflow}\n"
        "        body: |\n"
        "          WORK OVERRIDES CONTENT\n",
    )
    rc, stdout, stderr = _install_host_local(c)
    assert rc == 0, stderr
    assert "[host-local via local.yaml]" in stdout, stdout
    live = c.exec(["cat", _HOST_LIVE]).stdout
    assert "WORK OVERRIDES CONTENT" in live
    # The 2.0 OVERLAY model injects the body MARKERLESS — the legacy
    # host_local_sections block is migrated into a markerless overlay span,
    # so no user-section marker wraps the injected body.
    assert "setforge:user-section start host-local" not in live, live
    # Anchored below the matched heading.
    assert live.index("## Workflow") < live.index("WORK OVERRIDES CONTENT")


def test_install_host_local_before_heading_anchor(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """before-heading anchor splices above the matched heading."""
    c = docker_container()
    _write_local_yaml(
        c,
        "tracked_files:\n"
        "  host_local_md:\n"
        "    host_local_sections:\n"
        "      pre-comm:\n"
        "        anchor: {kind: before-heading, value: Communication}\n"
        "        body: |\n"
        "          BEFORE COMM BODY\n",
    )
    rc, _stdout, stderr = _install_host_local(c)
    assert rc == 0, stderr
    live = c.exec(["cat", _HOST_LIVE]).stdout
    assert "BEFORE COMM BODY" in live
    assert live.index("BEFORE COMM BODY") < live.index("## Communication")


def test_install_host_local_at_start_of_file(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """at-start-of-file anchor splices at line 0."""
    c = docker_container()
    _write_local_yaml(
        c,
        "tracked_files:\n"
        "  host_local_md:\n"
        "    host_local_sections:\n"
        "      head:\n"
        "        anchor: {kind: at-start-of-file}\n"
        "        body: |\n"
        "          HEAD BODY\n",
    )
    rc, _stdout, stderr = _install_host_local(c)
    assert rc == 0, stderr
    live = c.exec(["cat", _HOST_LIVE]).stdout
    # HEAD BODY must appear before the # host-local fixture title.
    assert live.index("HEAD BODY") < live.index("# host-local fixture")


def test_install_host_local_at_end_of_file(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """at-end-of-file anchor splices at the file tail."""
    c = docker_container()
    _write_local_yaml(
        c,
        "tracked_files:\n"
        "  host_local_md:\n"
        "    host_local_sections:\n"
        "      tail:\n"
        "        anchor: {kind: at-end-of-file}\n"
        "        body: |\n"
        "          TAIL BODY\n",
    )
    rc, _stdout, stderr = _install_host_local(c)
    assert rc == 0, stderr
    live = c.exec(["cat", _HOST_LIVE]).stdout
    assert "TAIL BODY" in live
    # TAIL BODY must appear AFTER the Trailing heading.
    assert live.index("## Trailing") < live.index("TAIL BODY")


def test_install_host_local_after_section_anchor(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """after-section anchor splices after a named existing user-section."""
    c = docker_container()
    _write_local_yaml(
        c,
        "tracked_files:\n"
        "  host_local_md:\n"
        "    host_local_sections:\n"
        "      after-notes:\n"
        "        anchor: {kind: after-section, name: notes}\n"
        "        body: |\n"
        "          AFTER-NOTES BODY\n",
    )
    rc, _stdout, stderr = _install_host_local(c)
    assert rc == 0, stderr
    live = c.exec(["cat", _HOST_LIVE]).stdout
    assert "AFTER-NOTES BODY" in live
    # The 2.0 OVERLAY model injects the body MARKERLESS, spliced after the
    # named existing section's end marker (no host-local marker wraps it).
    assert "setforge:user-section start host-local" not in live, live
    end_marker_idx = live.index("end shared notes")
    assert end_marker_idx < live.index("AFTER-NOTES BODY")


# ---------------------------------------------------------------------------
# Error paths (3 tests).
# ---------------------------------------------------------------------------


def test_install_anchor_not_found_aborts(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A missing-anchor install MUST exit nonzero AND not modify the live file."""
    c = docker_container()
    _write_local_yaml(
        c,
        "tracked_files:\n"
        "  host_local_md:\n"
        "    host_local_sections:\n"
        "      ghost:\n"
        "        anchor: {kind: after-heading, value: NoSuchHeading}\n"
        "        body: |\n"
        "          will not land\n",
    )
    rc, _stdout, stderr = _install_host_local(c)
    assert rc != 0
    assert "NoSuchHeading" in stderr or "anchor" in stderr.lower()
    # Live file either absent or contains no host-local marker.
    cat = c.exec(["cat", _HOST_LIVE], check=False)
    assert "will not land" not in cat.stdout


def test_install_empty_body_rejected_at_validate(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Empty body is rejected at validate time (Pydantic ValidationError)."""
    c = docker_container()
    _write_local_yaml(
        c,
        "tracked_files:\n"
        "  host_local_md:\n"
        "    host_local_sections:\n"
        "      empty:\n"
        "        anchor: {kind: at-end-of-file}\n"
        '        body: ""\n',
    )
    rc, stdout, _stderr = _setforge(
        c, ["validate", "--profile=test-host-local", f"--config={CONFIG_FIXTURE}"]
    )
    assert rc != 0
    # ``setforge validate`` renders failure context via ``typer.echo``
    # (stdout) — same stream the sibling rejection tests below assert on.
    # Pin the app-owned validator message (source.py:271); the backtick-quoted
    # ``body`` field + ``must be non-empty`` together are unique to this gate,
    # and each token is short enough to survive any formatter line-wrap.
    assert "`body`" in stdout, stdout
    assert "must be non-empty" in stdout, stdout


def test_validate_rejects_host_local_sections_on_json_tracked_file(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Non-markdown tracked_file declaring host_local_sections fails validate."""
    c = docker_container()
    _write_local_yaml(
        c,
        "tracked_files:\n"
        "  host_local_json_reject:\n"
        "    host_local_sections:\n"
        "      noop:\n"
        "        anchor: {kind: at-end-of-file}\n"
        "        body: |\n"
        "          body\n",
    )
    rc, stdout, _stderr = _setforge(
        c,
        [
            "validate",
            "--profile=test-host-local-reject-json",
            f"--config={CONFIG_FIXTURE}",
        ],
    )
    # ``setforge validate`` renders failure context via ``typer.echo``
    # (stdout); ``--check`` / strict-mode stream choice is a separate
    # axis from where the validate report itself lands.
    assert rc != 0
    # Pin the file-type gate phrase (source.py:644) AND the rejected extension.
    # The old ``.md`` branch always passed (``.md`` is in the suffix list the
    # message prints regardless of the failure).
    assert "supported only for markdown" in stdout, stdout
    assert ".json" in stdout, stdout


def test_validate_rejects_host_local_sections_on_yaml_tracked_file(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Non-markdown YAML tracked_file declaring host_local_sections fails validate.

    Uses ``yaml_shallow`` (already in the fixture) — declaring a
    host-local section against it must fail with the same file-type
    error as the JSON case.
    """
    c = docker_container()
    _write_local_yaml(
        c,
        "tracked_files:\n"
        "  yaml_shallow:\n"
        "    host_local_sections:\n"
        "      noop:\n"
        "        anchor: {kind: at-end-of-file}\n"
        "        body: |\n"
        "          body\n",
    )
    rc, stdout, _stderr = _setforge(
        c,
        ["validate", "--profile=test-yaml-shallow", f"--config={CONFIG_FIXTURE}"],
    )
    # ``setforge validate`` renders failure context via ``typer.echo``
    # (stdout); see _on_json_tracked_file for the same stream choice.
    assert rc != 0
    # Same file-type gate as the JSON case, pinned by phrase + extension.
    assert "supported only for markdown" in stdout, stdout
    assert ".yaml" in stdout, stdout


# ---------------------------------------------------------------------------
# Acceptance — accept-on-markdown sanity (1 test).
# ---------------------------------------------------------------------------


def test_validate_accepts_host_local_sections_on_md_tracked_file(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Markdown tracked_file with host_local_sections passes validate."""
    c = docker_container()
    _write_local_yaml(
        c,
        "tracked_files:\n"
        "  host_local_md:\n"
        "    host_local_sections:\n"
        "      ok:\n"
        "        anchor: {kind: after-heading, value: Workflow}\n"
        "        body: |\n"
        "          ok\n",
    )
    rc, _stdout, stderr = _setforge(
        c, ["validate", "--profile=test-host-local", f"--config={CONFIG_FIXTURE}"]
    )
    assert rc == 0, stderr


# ---------------------------------------------------------------------------
# Idempotency / symlink / compare / sync / revert (7 tests).
# ---------------------------------------------------------------------------


def test_install_idempotent_re_run_no_duplication(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Re-running install does not duplicate the (now markerless) host-local body."""
    c = docker_container()
    _write_local_yaml(
        c,
        "tracked_files:\n"
        "  host_local_md:\n"
        "    host_local_sections:\n"
        "      s1:\n"
        "        anchor: {kind: after-heading, value: Workflow}\n"
        "        body: |\n"
        "          idempotent body\n",
    )
    _install_host_local(c, check=True)
    _install_host_local(c, check=True)
    live = c.exec(["cat", _HOST_LIVE]).stdout
    # 14.17: the local.yaml host_local_sections block is migrated to a markerless
    # overlay, so the deployed file carries the body once, without markers.
    assert "start host-local s1" not in live
    assert live.count("idempotent body") == 1


def test_install_symlink_deployed_tracked_file(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """When tracked_file declares ``symlink:``, host-local injection lands on
    the TARGET file (where bytes actually reside)."""
    c = docker_container()
    _write_local_yaml(
        c,
        "tracked_files:\n"
        "  host_local_md_symlink:\n"
        "    host_local_sections:\n"
        "      link-body:\n"
        "        anchor: {kind: after-heading, value: Workflow}\n"
        "        body: |\n"
        "          SYMLINK BODY\n",
    )
    rc, _stdout, stderr = _install_host_local(c, profile="test-host-local-symlink")
    assert rc == 0, stderr
    target = c.exec(["cat", _HOST_LINK_TARGET]).stdout
    assert "SYMLINK BODY" in target
    # The link path resolves to the target.
    link = c.exec(["readlink", _HOST_LINK_LIVE]).stdout.strip()
    assert link == "~/.setforge_e2e/host-local/host_link_target.md"


def test_compare_shows_host_local_via_local_yaml_tag(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``setforge compare`` surfaces the ``+ [host-local via local.yaml] X``
    preview line for sections declared in local.yaml but not yet deployed
    (SPEC 1 mockup — overlay-aware compare).

    Exercises the compare CLI directly (not install --dry-run): the
    overlay is loaded + validated, ``compare_profile`` threads it into
    ``diff_file`` so already-injected sections do NOT show as drift,
    and the preview block lists every section the next install WOULD
    inject, tagged with the canonical ``[host-local via local.yaml]``
    provenance marker + a ``← would be injected`` cue.
    """
    c = docker_container()
    _install_host_local(c, check=True)  # baseline install (no overlay yet)
    _write_local_yaml(
        c,
        "tracked_files:\n"
        "  host_local_md:\n"
        "    host_local_sections:\n"
        "      to-inject:\n"
        "        anchor: {kind: after-heading, value: Workflow}\n"
        "        body: |\n"
        "          would-inject body\n",
    )
    rc, stdout, stderr = _setforge(
        c,
        [
            "compare",
            "--profile=test-host-local",
            f"--config={CONFIG_FIXTURE}",
        ],
    )
    assert rc == 0, stderr
    # Tightened: assert the full preview-line shape (sigil + canonical
    # provenance tag + section name) on the SAME line as the
    # "would be injected" cue, not three separate substring checks.
    assert "+ [host-local via local.yaml] to-inject" in stdout, stdout
    assert "would be injected" in stdout, stdout
    preview_lines = [
        line
        for line in stdout.splitlines()
        if "[host-local via local.yaml] to-inject" in line
    ]
    assert preview_lines, stdout
    assert any("would be injected" in line for line in preview_lines), stdout


def test_compare_does_not_flag_injected_as_drift(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """After install, compare should NOT report the injected section as drift."""
    c = docker_container()
    _write_local_yaml(
        c,
        "tracked_files:\n"
        "  host_local_md:\n"
        "    host_local_sections:\n"
        "      mask-me:\n"
        "        anchor: {kind: after-heading, value: Workflow}\n"
        "        body: |\n"
        "          injected\n",
    )
    _install_host_local(c, check=True)
    rc, stdout, stderr = _setforge(
        c,
        [
            "compare",
            "--profile=test-host-local",
            f"--config={CONFIG_FIXTURE}",
            "--check",
        ],
    )
    assert rc == 0, f"compare --check reported drift unexpectedly:\n{stdout}\n{stderr}"


def test_sync_capture_back_excludes_host_local(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``setforge sync`` MUST NOT write injected host-local section markers
    back into the tracked source.

    Without the capture-back filter, the host-local marker pair would
    re-appear in the tracked file on the next sync — that's a leak of
    host state into shared tracked content. The current implementation
    leaves the tracked source unchanged when only host-local injection
    is present.
    """
    c = docker_container()
    _write_local_yaml(
        c,
        "tracked_files:\n"
        "  host_local_md:\n"
        "    host_local_sections:\n"
        "      do-not-leak:\n"
        "        anchor: {kind: after-heading, value: Workflow}\n"
        "        body: |\n"
        "          do-not-leak body\n",
    )
    _install_host_local(c, check=True)
    rc, _stdout, stderr = _setforge(
        c, ["sync", "--profile=test-host-local", f"--config={CONFIG_FIXTURE}", "-y"]
    )
    assert rc == 0, stderr
    # The tracked source MUST NOT carry the host-local marker.
    tracked = c.exec(
        ["cat", "/workspace/tests/fixtures/e2e/tracked/host-local/host.md"]
    ).stdout
    assert "do-not-leak" not in tracked


def test_revert_undoes_injection(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``setforge revert`` restores the live file to its pre-install state,
    removing the host-local marker pair."""
    c = docker_container()
    _write_local_yaml(
        c,
        "tracked_files:\n"
        "  host_local_md:\n"
        "    host_local_sections:\n"
        "      revertable:\n"
        "        anchor: {kind: after-heading, value: Workflow}\n"
        "        body: |\n"
        "          REVERTABLE BODY\n",
    )
    _install_host_local(c, check=True)
    live_after_install = c.exec(["cat", _HOST_LIVE]).stdout
    assert "REVERTABLE BODY" in live_after_install
    rc, _stdout, stderr = _setforge(
        c, ["revert", "--profile=test-host-local", f"--config={CONFIG_FIXTURE}", "-y"]
    )
    assert rc == 0, stderr
    # ``setforge revert`` restores live to PRE-INSTALL state — for a
    # fresh container the live file did not exist before install, so
    # revert removes it (and ``cat`` returns nonzero). check=False so
    # the test asserts on stdout content directly rather than crashing
    # on the missing file.
    cat = c.exec(["cat", _HOST_LIVE], check=False)
    assert "REVERTABLE BODY" not in cat.stdout


def test_validate_catches_anchor_not_found_offline(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``setforge validate`` surfaces an anchor-not-found error WITHOUT touching
    the live filesystem (offline gate before install)."""
    c = docker_container()
    _write_local_yaml(
        c,
        "tracked_files:\n"
        "  host_local_md:\n"
        "    host_local_sections:\n"
        "      ghost:\n"
        "        anchor: {kind: after-heading, value: PhantomHeading}\n"
        "        body: |\n"
        "          will not land\n",
    )
    rc, stdout, _stderr = _setforge(
        c, ["validate", "--profile=test-host-local", f"--config={CONFIG_FIXTURE}"]
    )
    # ``setforge validate`` renders failure context via ``typer.echo``
    # (stdout); see _on_json_tracked_file for the same stream choice.
    assert rc != 0
    assert "PhantomHeading" in stdout or "anchor" in stdout.lower()
    # Confirm offline: live file did NOT get created.
    cat = c.exec(["cat", _HOST_LIVE], check=False)
    assert cat.returncode != 0  # absent file
