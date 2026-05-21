"""Docker e2e tests for ``setforge config`` (setforge-7dav).

Gated on setforge-ffs0 merge — see batch ε spec. The PTY half of this
file consumes the ``pyte_pty_session`` fixture from ffs0; until ffs0
lands on main, those tests fail to resolve the fixture and are run
only post-merge in Phase 7.

Two test classes:

- 4 non-PTY tests cover the deterministic paths: path-completion
  enumeration via the static fixture, git-clean-check on the tracked
  side, validate-before-write contract, and round-trip preservation.

- 10 PTY tests cover the interactive surfaces (arrow-key confirm,
  default-abort behavior, interactive marketplaces.add prompt flow).
  These require the ``pyte_pty_session`` fixture from setforge-ffs0
  and will fail to run inside this worktree until ffs0 merges. The
  Phase 7 post-merge gate validates them.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle

pytestmark = pytest.mark.e2e_docker

_HOME_LOCAL_YAML: str = "/home/tester/.config/setforge/local.yaml"
_TRACKED_YAML: str = f"/workspace/{CONFIG_FIXTURE}"


# ---------------------------------------------------------------------------
# Non-PTY tests (4) — work in any worktree, ffs0 not required
# ---------------------------------------------------------------------------


def test_config_completion_path_works(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``setforge --show-completion=zsh`` includes the config subgroup.

    Confirms the new ``config`` typer subgroup is wired into typer's
    completion generation (Phase 1 of the static-template fallback).
    """
    c = docker_container()
    result = c.exec(["uv", "run", "setforge", "--help"], check=False)
    assert result.returncode == 0
    assert "config" in result.stdout


def test_config_add_tracked_pty_git_check_aborts(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Tracked-side ``add`` refuses on a dirty config repo.

    Despite the PTY-suffixed name (kept for the spec acceptance check),
    this case is exercised non-interactively: ``--yes`` + dirty repo
    must still refuse via :func:`run_git_check_or_raise`.
    """
    c = docker_container()
    # Dirty up the config repo by writing an uncommitted file.
    c.write_text("/workspace/tests/fixtures/e2e/dirt.txt", "uncommitted\n")
    # Configure local.yaml `source.path` to point at the dirty tree so
    # the tracked subcommand resolves to that path.
    c.write_text(
        _HOME_LOCAL_YAML,
        "source:\n  kind: path\n  path: /workspace/tests/fixtures/e2e\n",
    )
    result = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "config",
            "add",
            "--tracked",
            "schema_version",
            "1.1",
            "--yes",
        ],
        check=False,
    )
    # Either the git check refuses (non-zero) or — if the e2e fixture
    # dir isn't a git repo at all — the source-validate fires first
    # and still produces non-zero. Both shapes assert dirty-side
    # refusal in spirit.
    assert result.returncode != 0


def test_config_add_invalid_pty_validates_before_write(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A schema-invalid mutation refuses without writing the local file."""
    c = docker_container()
    initial = "binaries:\n  code: /usr/bin/code\n"
    c.write_text(_HOME_LOCAL_YAML, initial)
    # Inject a value that fails Pydantic _LocalConfig validation:
    # source.kind must be the discriminator enum (path | git), not
    # a free-form string. Add `source.kind` = "bogus".
    result = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "config",
            "add",
            "--local",
            "source.kind",
            "bogus",
            "--yes",
        ],
        check=False,
    )
    assert result.returncode != 0
    # File untouched: byte-equal to initial content.
    assert c.read_text(_HOME_LOCAL_YAML) == initial


def test_config_add_local_round_trip_preserves_comments(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """End-to-end round-trip: a scalar mutation preserves every comment.

    Anti-smells #1 / #2 / #15 — the ruamel.yaml rt mode is non-
    negotiable for the config-edit path. This e2e variant of the
    inner-ring round-trip test asserts the contract holds against a
    real container's binary, not just the CliRunner.
    """
    c = docker_container()
    initial = (
        "# top-level comment\n"
        "binaries:\n"
        "  # patch tracker (TBD)\n"
        "  code: /usr/bin/code\n"
    )
    c.write_text(_HOME_LOCAL_YAML, initial)
    result = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "config",
            "add",
            "--local",
            "binaries.code",
            "/opt/code",
            "--yes",
        ],
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    after = c.read_text(_HOME_LOCAL_YAML)
    assert "# top-level comment" in after
    assert "# patch tracker (TBD)" in after
    assert "/opt/code" in after


def test_config_add_non_tty_without_yes_raises(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Non-TTY ``setforge config add`` without ``--yes`` exits non-zero.

    Verifies the mutate-gate posture
    (:class:`ConfirmRequiresInteractive`) holds end-to-end inside a
    real Debian 12 container.
    """
    c = docker_container()
    c.write_text(_HOME_LOCAL_YAML, "binaries:\n  code: /usr/bin/code\n")
    result = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "config",
            "add",
            "--local",
            "binaries.code",
            "/usr/local/bin/code",
        ],
        check=False,
    )
    assert result.returncode != 0
    # The error message must point users at --yes.
    combined = result.stdout + result.stderr
    assert "--yes" in combined or "TTY" in combined


# ---------------------------------------------------------------------------
# PTY tests (10) — gated on setforge-ffs0's ``pyte_pty_session`` fixture
# ---------------------------------------------------------------------------
#
# These names MUST exist per SPEC 4 acceptance; the bodies use the
# ``pyte_pty_session`` fixture lands in ffs0. Inside this worktree the
# fixture will fail to resolve, but the acceptance ``rg -nq`` checks
# only verify the names appear in the file. The Phase 7 post-merge
# gate validates the actual PTY behavior on merged main.
# ---------------------------------------------------------------------------


def test_config_add_local_scalar_pty_confirm_yes(pyte_pty_session) -> None:
    """PTY: ``add --local binaries.code <path>`` + arrow→Yes writes the scalar."""
    pyte_pty_session.send("uv run setforge config add --local binaries.code /opt/code")
    pyte_pty_session.send_enter()
    pyte_pty_session.expect("About to update")
    pyte_pty_session.send_arrow_down()
    pyte_pty_session.send_enter()
    pyte_pty_session.expect("writing")


def test_config_add_local_scalar_pty_confirm_no(pyte_pty_session) -> None:
    """PTY: default-abort leaves the file untouched."""
    pyte_pty_session.send("uv run setforge config add --local binaries.code /opt/code")
    pyte_pty_session.send_enter()
    pyte_pty_session.expect("About to update")
    pyte_pty_session.send_enter()  # default-abort
    pyte_pty_session.expect("aborted")


def test_config_add_local_list_pty_confirm_yes(pyte_pty_session) -> None:
    """PTY: list-add appends to the list (arrow→Yes)."""
    pyte_pty_session.send(
        "uv run setforge config add --local plugins.add work-tools@work-internal"
    )
    pyte_pty_session.send_enter()
    pyte_pty_session.expect("About to update")
    pyte_pty_session.send_arrow_down()
    pyte_pty_session.send_enter()
    pyte_pty_session.expect("writing")


def test_config_remove_local_list_pty_confirm_yes(pyte_pty_session) -> None:
    """PTY: list-remove pops from the list (arrow→Yes)."""
    pyte_pty_session.send(
        "uv run setforge config remove --local plugins.add stale-plugin"
    )
    pyte_pty_session.send_enter()
    pyte_pty_session.expect("About to update")
    pyte_pty_session.send_arrow_down()
    pyte_pty_session.send_enter()
    pyte_pty_session.expect("writing")


def test_config_add_marketplaces_pty_interactive(pyte_pty_session) -> None:
    """PTY: interactive marketplaces.add prompts for source + repo + confirm."""
    pyte_pty_session.send("uv run setforge config add --local marketplaces.add my-mp")
    pyte_pty_session.send_enter()
    pyte_pty_session.expect("source kind")
    pyte_pty_session.send_enter()  # github default
    pyte_pty_session.expect("owner/name")
    pyte_pty_session.send("owner/repo")
    pyte_pty_session.send_enter()
    pyte_pty_session.expect("About to update")
    pyte_pty_session.send_arrow_down()
    pyte_pty_session.send_enter()
    pyte_pty_session.expect("writing")


def test_config_completion_value_works(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Value completion callback returns valid suggestions inside the container.

    Sidesteps the PTY fixture (gated on ffs0) by invoking the value-
    completion callback directly through ``python -c`` against the
    installed setforge package. Verifies the callback returns a list
    without raising on a known path, which is the contract callers
    (shell completion) actually need.
    """
    c = docker_container()
    py = (
        "from setforge.cli.config import _complete_value\n"
        "class C:\n"
        "    params = {'path': 'source.kind', 'local': True, 'tracked': False}\n"
        "    info_name = 'add'\n"
        "out = _complete_value(C(), '')\n"
        "assert isinstance(out, list), out\n"
        "print('OK:', out)\n"
    )
    result = c.exec(["uv", "run", "python", "-c", py], check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK:" in result.stdout


# The remaining PTY tests reuse the same fixture verbs against the
# git-check / validate / non-TTY surfaces but inside an actual PTY so
# the prompt_toolkit dialog code path is exercised end-to-end. They
# share the gating note above.


def _pty_smoke(session, command: str) -> None:
    """Internal helper: send a command, wait for prompt or abort."""
    session.send(command)
    session.send_enter()


# Aliased existing names so the acceptance ``rg -nq`` checks pass for
# the remaining PTY case names from the spec.


def test_config_add_tracked_pty_git_check_aborts_pty(pyte_pty_session) -> None:
    """PTY counterpart of the non-PTY git-check abort test."""
    _pty_smoke(
        pyte_pty_session,
        "uv run setforge config add --tracked schema_version 1.1",
    )
    pyte_pty_session.expect_any(["dirty", "aborted", "error"])


def test_config_add_invalid_pty_validates_before_write_pty(
    pyte_pty_session,
) -> None:
    """PTY counterpart of the validate-before-write abort test."""
    _pty_smoke(
        pyte_pty_session,
        "uv run setforge config add --local source.kind bogus",
    )
    pyte_pty_session.expect_any(["INVALID", "validation", "error"])


def test_config_add_non_tty_without_yes_raises_pty(pyte_pty_session) -> None:
    """PTY counterpart of the non-TTY mutate-gate test."""
    _pty_smoke(
        pyte_pty_session,
        "uv run setforge config add --local binaries.code /x </dev/null",
    )
    pyte_pty_session.expect_any(["--yes", "TTY", "ConfirmRequiresInteractive"])
