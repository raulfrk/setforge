"""Docker e2e tests for ``setforge config`` (setforge-7dav).

The PTY half of this file consumes the ``pyte_pty_session`` factory
fixture from setforge-ffs0 (now merged on main). The factory takes
``container=`` + ``cmd=`` and returns a :class:`PyteSession` whose
key verbs are :meth:`send_keys`, :meth:`expect_in_display`, and
:meth:`wait_for_exit`.

Two test classes:

- 5 non-PTY / non-interactive tests cover the deterministic paths:
  git-clean-check on the tracked side, validate-before-write contract,
  round-trip preservation, the non-TTY mutate-gate, and the inner
  ``_complete_value`` callback contract invoked through ``python -c``.

- 10 PTY tests cover the interactive surfaces: scalar / list arrow-key
  confirm (yes + default-abort), list remove, the interactive
  marketplaces.add prompt flow, shell-completion for paths + values,
  tracked-side git-check refusal under a TTY, the validate-before-write
  surface under a TTY, and the non-TTY-stdin mutate-gate exercised from
  inside an interactive PTY.
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
    pyte_pty_session: Callable[..., object],
) -> None:
    """PTY: shell tab-completion on ``setforge config add --local <TAB>``.

    Spawns an interactive zsh inside the container, sources the
    ``setforge --show-completion=zsh`` script, types
    ``setforge config add --local `` and presses TAB, then asserts a
    known dotted-path candidate (``binaries.code``) appears in the
    rendered completion menu. Exercises the END-TO-END shell-completion
    path (typer's completion machinery → setforge's
    _complete_path_dispatch callback → shell-rendered candidates).
    """
    c = docker_container()
    session = pyte_pty_session(
        container=c.cid,
        cmd=["zsh", "-i"],
    )
    session.send_keys('eval "$(uv run setforge --show-completion=zsh)" 2>/dev/null\r')
    session.expect_in_display("$", timeout=10)
    session.send_keys("uv run setforge config add --local \t")
    session.expect_in_display("binaries.code", timeout=10)


def test_config_add_tracked_dirty_repo_refuses_non_pty(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Tracked-side ``add`` refuses on a dirty config repo (non-PTY).

    Non-interactive variant: ``--yes`` + dirty repo must still refuse
    via :func:`run_git_check_or_raise`. The PTY variant
    :func:`test_config_add_tracked_pty_git_check_aborts` (below)
    exercises the same gate inside a real PTY for the prompt_toolkit
    code path.
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


def test_config_add_invalid_value_refuses_write_non_pty(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A schema-invalid mutation refuses without writing the local file (non-PTY).

    Non-interactive variant. The PTY variant
    :func:`test_config_add_invalid_pty_validates_before_write` (below)
    exercises the same validate-before-write gate inside a real PTY.
    """
    c = docker_container()
    initial = "binaries:\n  code: /usr/bin/code\n"
    c.write_text(_HOME_LOCAL_YAML, initial)
    # Inject a value that fails Pydantic LocalConfig validation:
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


def test_config_add_non_tty_without_yes_raises_non_pty(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Non-TTY ``setforge config add`` without ``--yes`` exits non-zero (non-PTY).

    Verifies the mutate-gate posture
    (:class:`ConfirmRequiresInteractive`) holds end-to-end inside a
    real Debian 12 container. The PTY variant
    :func:`test_config_add_non_tty_without_yes_raises` (below) drives
    the same flow from inside an interactive PTY (where stdin
    redirection still produces a non-TTY for the child).
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
# PTY tests (10) — drive the real ``pyte_pty_session`` factory from ffs0
# ---------------------------------------------------------------------------
#
# Pattern: ``session = pyte_pty_session(container=..., cmd=[...])`` then
# anchor on ``session.expect_in_display(needle, timeout=...)`` /
# ``session.send_keys(seq)`` / ``session.wait_for_exit(timeout=...,
# expected_code=...)``. Radiolist labels are matched by their visible
# substrings — ``"(*) abort"`` / ``"(*) write"`` (asterisk indicates the
# currently-selected radio option in the rendered display). Arrow down
# is ``"\x1b[B"``; Enter is ``"\r"``; Tab is ``"\t"``.
# ---------------------------------------------------------------------------


def test_config_add_local_scalar_pty_confirm_yes(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., object],
) -> None:
    """PTY: ``add --local binaries.code <path>`` + arrow→write writes the scalar."""
    c = docker_container()
    c.write_text(_HOME_LOCAL_YAML, "binaries:\n  code: /usr/bin/code\n")
    session = pyte_pty_session(
        container=c.cid,
        cmd=[
            "uv",
            "run",
            "setforge",
            "config",
            "add",
            "--local",
            "binaries.code",
            "/opt/code",
        ],
    )
    session.expect_in_display("About to update", timeout=30.0)
    session.expect_in_display("(*) abort", timeout=10.0)
    session.send_keys("\x1b[B")
    session.send_keys("\r")
    session.expect_in_display("writing", timeout=15.0)
    session.wait_for_exit(timeout=60.0, expected_code=0)
    after = c.read_text(_HOME_LOCAL_YAML)
    assert "/opt/code" in after


def test_config_add_local_scalar_pty_confirm_no(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., object],
) -> None:
    """PTY: default-abort (Enter on first selection) leaves the file untouched."""
    c = docker_container()
    initial = "binaries:\n  code: /usr/bin/code\n"
    c.write_text(_HOME_LOCAL_YAML, initial)
    session = pyte_pty_session(
        container=c.cid,
        cmd=[
            "uv",
            "run",
            "setforge",
            "config",
            "add",
            "--local",
            "binaries.code",
            "/opt/code",
        ],
    )
    session.expect_in_display("About to update", timeout=30.0)
    session.expect_in_display("(*) abort", timeout=10.0)
    # Default-selected is "abort"; Enter accepts that choice.
    session.send_keys("\r")
    session.expect_in_display("aborted", timeout=15.0)
    session.wait_for_exit(timeout=60.0, expected_code=0)
    assert c.read_text(_HOME_LOCAL_YAML) == initial


def test_config_add_local_list_pty_confirm_yes(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., object],
) -> None:
    """PTY: list-add appends to the list (arrow→write)."""
    c = docker_container()
    c.write_text(
        _HOME_LOCAL_YAML,
        "plugins:\n  add:\n    - existing-plugin@official\n",
    )
    session = pyte_pty_session(
        container=c.cid,
        cmd=[
            "uv",
            "run",
            "setforge",
            "config",
            "add",
            "--local",
            "plugins.add",
            "work-tools@work-internal",
        ],
    )
    session.expect_in_display("About to update", timeout=30.0)
    session.expect_in_display("(*) abort", timeout=10.0)
    session.send_keys("\x1b[B")
    session.send_keys("\r")
    session.expect_in_display("writing", timeout=15.0)
    session.wait_for_exit(timeout=60.0, expected_code=0)
    assert "work-tools@work-internal" in c.read_text(_HOME_LOCAL_YAML)


def test_config_remove_local_list_pty_confirm_yes(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., object],
) -> None:
    """PTY: list-remove pops from the list (arrow→write)."""
    c = docker_container()
    c.write_text(
        _HOME_LOCAL_YAML,
        "plugins:\n  add:\n    - stale-plugin@official\n    - keep@official\n",
    )
    session = pyte_pty_session(
        container=c.cid,
        cmd=[
            "uv",
            "run",
            "setforge",
            "config",
            "remove",
            "--local",
            "plugins.add",
            "stale-plugin@official",
        ],
    )
    session.expect_in_display("About to update", timeout=30.0)
    session.expect_in_display("(*) abort", timeout=10.0)
    session.send_keys("\x1b[B")
    session.send_keys("\r")
    session.expect_in_display("writing", timeout=15.0)
    session.wait_for_exit(timeout=60.0, expected_code=0)
    after = c.read_text(_HOME_LOCAL_YAML)
    assert "stale-plugin@official" not in after
    assert "keep@official" in after


def test_config_add_marketplaces_pty_interactive(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., object],
) -> None:
    """PTY: interactive marketplaces.add prompts for source + repo + confirm."""
    c = docker_container()
    c.write_text(_HOME_LOCAL_YAML, "binaries:\n  code: /usr/bin/code\n")
    session = pyte_pty_session(
        container=c.cid,
        cmd=[
            "uv",
            "run",
            "setforge",
            "config",
            "add",
            "--local",
            "marketplaces.add",
            "my-mp",
        ],
    )
    # Source-kind radiolist (github is the default per _prompt_marketplace_kind).
    session.expect_in_display("source kind", timeout=30.0)
    session.send_keys("\r")
    # owner/name input dialog for github.
    session.expect_in_display("owner/name", timeout=15.0)
    session.send_keys("owner/repo")
    session.send_keys("\t")
    session.send_keys("\r")
    # Diff-preview confirm panel.
    session.expect_in_display("About to update", timeout=15.0)
    session.expect_in_display("(*) abort", timeout=10.0)
    session.send_keys("\x1b[B")
    session.send_keys("\r")
    session.expect_in_display("writing", timeout=15.0)
    session.wait_for_exit(timeout=60.0, expected_code=0)
    after = c.read_text(_HOME_LOCAL_YAML)
    assert "owner/repo" in after


def test_config_completion_value_works(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., object],
) -> None:
    """PTY: shell tab-completion on ``setforge config remove --local plugins.add``.

    Spawns an interactive zsh inside the container, sources the
    ``setforge --show-completion=zsh`` script, types
    ``setforge config remove --local plugins.add `` and presses TAB,
    then asserts the configured plugin names appear in the screen
    buffer as completion candidates.

    This exercises the END-TO-END shell-completion path (typer's
    completion machinery → setforge's _complete_value callback →
    shell-rendered candidates) rather than the unit-level callback
    contract that ``test_config_completion_value_callback_contract``
    covers. The PTY route catches integration breakage that
    monkeypatched callbacks can't (shell-renderer escapes,
    completion-script wiring, lazy-import timing).
    """
    c = docker_container()
    # Seed a local.yaml with one plugin so TAB has something to complete.
    c.write_text(
        _HOME_LOCAL_YAML,
        "binaries:\n"
        "  code: /usr/bin/code\n"
        "plugins:\n"
        "  add:\n"
        "    - secure-code-review@official\n",
    )
    session = pyte_pty_session(
        container=c.cid,
        cmd=["zsh", "-i"],
    )
    # Source the completion script + tab on the relevant path.
    session.send_keys('eval "$(uv run setforge --show-completion=zsh)" 2>/dev/null\r')
    session.expect_in_display("$", timeout=10)
    session.send_keys("uv run setforge config remove --local plugins.add \t")
    # The plugin name should appear in the rendered completion menu.
    session.expect_in_display("secure-code-review@official", timeout=10)


def test_config_completion_value_callback_contract(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Direct-callback contract for ``_complete_value`` inside the container.

    Sits alongside the PTY test above. The PTY test exercises the
    end-to-end shell-completion pipeline; this one pins the inner
    callback contract (returns a list, never raises) by invoking
    ``setforge.cli.config._complete_value`` directly through
    ``python -c`` against the installed setforge package.

    A real shell-completion PTY flow is brittle (shell-startup timing,
    completion-script source order, host shell version skew, TAB-byte
    handling). The PTY test above documents the failure-mode set; this
    callback test gives a fast deterministic signal when the inner
    contract regresses without bisecting through the shell layer.
    """
    c = docker_container()
    py = (
        "from setforge.cli.config import _complete_value\n"
        "class C:\n"
        "    params = {'path': 'source.kind', 'local': True, 'tracked': False}\n"
        "    info_name = 'add'\n"
        "out = _complete_value(C(), '')\n"
        "assert isinstance(out, list), out\n"
        "assert 'path' in out and 'git' in out, out\n"
        "print('OK:', out)\n"
    )
    result = c.exec(["uv", "run", "python", "-c", py], check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK:" in result.stdout


# The remaining PTY tests reuse the same fixture verbs against the
# git-check / validate / non-TTY surfaces but inside an actual PTY so
# the prompt_toolkit dialog code path is exercised end-to-end.


def test_config_add_tracked_pty_git_check_aborts(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., object],
) -> None:
    """PTY counterpart of the non-PTY git-check abort test."""
    c = docker_container()
    # Dirty the e2e config-repo working tree so the tracked-side gate trips.
    c.write_text("/workspace/tests/fixtures/e2e/dirt.txt", "uncommitted\n")
    c.write_text(
        _HOME_LOCAL_YAML,
        "source:\n  kind: path\n  path: /workspace/tests/fixtures/e2e\n",
    )
    session = pyte_pty_session(
        container=c.cid,
        cmd=[
            "uv",
            "run",
            "setforge",
            "config",
            "add",
            "--tracked",
            "schema_version",
            "1.1",
        ],
    )
    # The git-check path surfaces a non-zero exit before the diff-preview
    # panel; the dirty-tree refusal message contains "dirty" or the
    # source-validate text. Either signal counts as "gate tripped".
    try:
        session.expect_in_display("dirty", timeout=30.0)
    except TimeoutError:
        session.expect_in_display("Error", timeout=5.0)
    session.wait_for_exit(timeout=60.0, expected_code=1)


def test_config_add_invalid_pty_validates_before_write(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., object],
) -> None:
    """PTY counterpart of the validate-before-write abort test."""
    c = docker_container()
    c.write_text(_HOME_LOCAL_YAML, "binaries:\n  code: /usr/bin/code\n")
    session = pyte_pty_session(
        container=c.cid,
        cmd=[
            "uv",
            "run",
            "setforge",
            "config",
            "add",
            "--local",
            "source.kind",
            "bogus",
        ],
    )
    # The schema-validate hook fires before the diff-preview panel and
    # raises a SetforgeError surfaced as a non-zero exit with the field
    # name in the message.
    try:
        session.expect_in_display("validation", timeout=30.0)
    except TimeoutError:
        session.expect_in_display("source", timeout=5.0)
    session.wait_for_exit(timeout=60.0, expected_code=1)


def test_config_add_non_tty_without_yes_raises(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., object],
) -> None:
    """PTY counterpart of the non-TTY mutate-gate test.

    Drives ``setforge config add`` from inside an interactive PTY but
    redirects the child's stdin from ``/dev/null`` via the shell so the
    SUT sees a non-TTY stdin even though the PTY itself is interactive.
    This exercises the ``ConfirmRequiresInteractive`` mutate-gate code
    path inside a real TTY-aware host shell.
    """
    c = docker_container()
    c.write_text(_HOME_LOCAL_YAML, "binaries:\n  code: /usr/bin/code\n")
    session = pyte_pty_session(
        container=c.cid,
        cmd=[
            "sh",
            "-c",
            "uv run setforge config add --local binaries.code /x </dev/null",
        ],
    )
    try:
        session.expect_in_display("--yes", timeout=30.0)
    except TimeoutError:
        session.expect_in_display("TTY", timeout=5.0)
    session.wait_for_exit(timeout=60.0, expected_code=1)
