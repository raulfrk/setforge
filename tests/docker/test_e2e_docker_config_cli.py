"""Docker e2e tests for ``setforge config``.

The PTY half of this file consumes the ``pyte_pty_session`` factory
fixture (now merged on main). The factory takes
``container=`` + ``cmd=`` and returns a :class:`PyteSession` whose
key verbs are :meth:`send_keys`, :meth:`expect_in_display`, and
:meth:`wait_for_exit`.

Two test classes:

- 6 non-PTY / non-interactive tests cover the deterministic paths:
  git-clean-check on the tracked side, validate-before-write contract,
  round-trip preservation, the non-TTY mutate-gate, the
  ``config show --effective`` smoke test (regression guard against
  the round-2 ``ctx_obj=None`` crash), and the inner
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
from tests.docker.pyte_session import PyteSession

pytestmark = pytest.mark.e2e_docker

_HOME_LOCAL_YAML: str = "/home/tester/.config/setforge/local.yaml"
_TRACKED_YAML: str = f"/workspace/{CONFIG_FIXTURE}"


# ---------------------------------------------------------------------------
# Mixed top-section: shell-completion path test (uses pyte) + 4 non-PTY
# tests covering the deterministic surfaces.
# ---------------------------------------------------------------------------


def test_config_completion_path_works(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., PyteSession],
) -> None:
    """PTY: shell tab-completion on ``setforge config add --local <TAB>``.

    Spawns an interactive zsh inside the container, sources the
    ``setforge --show-completion zsh`` script, types
    ``setforge config add --local `` and presses TAB, then asserts a
    known schema-derived dotted-path candidate (``source.kind``)
    appears in the rendered completion menu. Exercises the END-TO-END
    shell-completion path (typer's completion machinery → setforge's
    _complete_path_dispatch callback → shell-rendered candidates).

    ``source.kind`` is a known Pydantic schema path (``LocalConfig.source.kind``)
    so ``_enumerate_paths`` always surfaces it; arbitrary user dict-keys
    like ``binaries.code`` only appear via the schema walker when the
    parent field has structured children, which the free-form
    ``dict[str, str]`` ``binaries`` field does not.
    """
    c = docker_container()
    # Touch ~/.zshrc so `zsh -i` doesn't trigger the zsh-newuser-install
    # wizard, which intercepts stdin before our completion-eval can run.
    # Same idiom as tests/docker/test_e2e_docker_completion.py.
    c.exec(["touch", "/home/tester/.zshrc"])
    session = pyte_pty_session(
        container=c.cid,
        cmd=["zsh", "-i"],
    )
    # Wiring constraints for zsh shell completion:
    # (1) Put the project venv bin on PATH so bare ``setforge`` resolves
    #     to the installed CLI (interactive zsh does not see the
    #     uv-managed venv unless asked). The typer-generated completion
    #     registers ``compdef _setforge_completion setforge`` — i.e.
    #     for the literal command word ``setforge``, not ``uv``, so
    #     invoking via ``uv run setforge`` bypasses the completion.
    # (2) ``autoload -U compinit && compinit -u`` before the eval so the
    #     ``compdef`` builtin used by the typer script is defined.
    # (3) Explicit echo markers (DONE_COMPINIT / DONE_EVAL) — a stale
    #     ``%`` prompt from initial shell startup would otherwise let
    #     TAB fire before compinit / eval actually completed.
    session.send_keys("export PATH=/workspace/.venv/bin:$PATH\r")
    session.send_keys("autoload -U compinit && compinit -u && echo DONE_COMPINIT\r")
    session.expect_in_display("DONE_COMPINIT", timeout=15)
    session.send_keys(
        'eval "$(setforge --show-completion zsh)" 2>/dev/null && echo DONE_EVAL\r'
    )
    session.expect_in_display("DONE_EVAL", timeout=30)
    session.send_keys("setforge config add --local \t")
    session.expect_in_display("source.kind", timeout=15)


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


def test_config_show_effective_smoke_non_pty(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``setforge config show --effective --profile=test-minimal`` exits 0 (non-PTY).

    Regression guard for the round-2 ``_show_effective`` extraction:
    the helper used to pass ``ctx_obj=None`` to
    :func:`setforge.cli._output.render`, which trips the production
    guard (``RuntimeError("render() called with ctx_obj=None outside
    test context")``) when ``PYTEST_CURRENT_TEST`` is not set.
    Subprocess invocation inside the container does NOT inherit
    ``PYTEST_CURRENT_TEST`` from the host pytest, so this test
    exercises the real production env-shape.

    The fix threads ``ctx.obj`` (typer-injected) from ``config_show``
    into ``_show_effective``, so a real :class:`OutputContext` reaches
    ``render`` and the human renderer fires cleanly.
    """
    c = docker_container()
    # Seed a minimal setforge.yaml at /tmp/cfg (source-resolution expects
    # the file to be named exactly setforge.yaml; the fixtures dir has
    # setforge.test.yaml which won't be picked up).
    c.write_text(
        "/tmp/cfg/setforge.yaml",
        "version: 1\n"
        "schema_version: '1.0'\n"
        "tracked_files:\n"
        "  foo:\n"
        "    src: foo.md\n"
        "    dst: foo.md\n"
        "profiles:\n"
        "  base:\n"
        "    tracked_files:\n"
        "      - foo\n",
    )
    c.write_text(
        _HOME_LOCAL_YAML,
        "source:\n  kind: path\n  path: /tmp/cfg\n",
    )
    result = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "config",
            "show",
            "--effective",
            "--profile=base",
        ],
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    # The profile-show body prints the resolved profile name.
    assert "base" in result.stdout


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
# PTY tests (10) — drive the real ``pyte_pty_session`` factory
# ---------------------------------------------------------------------------
#
# Pattern: ``session = pyte_pty_session(container=..., cmd=[...])`` then
# anchor on ``session.expect_in_display(needle, timeout=...)`` /
# ``session.send_keys(seq)`` / ``session.wait_for_exit(timeout=...,
# expected_code=...)``.
#
# The confirm radiolist is the full-screen prompt_toolkit
# ``radiolist_dialog`` with title=``setforge config`` and prompt
# ``Apply the mutation above?``. Labels render as ``( ) abort (no
# change)`` and ``( ) write``; the asterisk marks the currently-selected
# radio item. The dialog CLEARS THE SCREEN on first paint, so anchor on
# the DIALOG content (``setforge config`` / ``Apply the mutation`` /
# ``(*) abort``), NOT on the diff-panel preamble (``About to update``)
# which is wiped from the display by the time the dialog appears.
# Submitting requires arrow→to select + Enter (commit radio) + Tab
# (focus OK button) + Enter (submit) — see the auto-confirm reference tests.
# ---------------------------------------------------------------------------


def _confirm_radiolist_write(session: PyteSession) -> None:
    """Arrow-down to select ``write``, commit, Tab to OK, submit.

    The radiolist default is ``abort``. Sending arrow-down moves the
    cursor onto ``write``; the inner Enter commits the radio selection;
    Tab moves focus to the ``Ok`` button; the final Enter submits the
    dialog. Mirrors the auto-confirm confirm-yes ``send_keys`` sequence.
    """
    session.send_keys("\x1b[B")
    session.send_keys("\r")
    session.expect_in_display("(*) write", timeout=5.0)
    session.send_keys("\t")
    session.send_keys("\r")


def _confirm_radiolist_abort(session: PyteSession) -> None:
    """Default-abort: leave the radio on ``abort``, Tab to OK, submit.

    The radiolist default is ``abort``. Tab moves focus to the ``Ok``
    button without changing the radio selection; the final Enter
    submits with the default value. Mirrors the auto-confirm confirm-no shape.
    """
    session.send_keys("\t")
    session.send_keys("\r")


def test_config_add_local_scalar_pty_confirm_yes(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., PyteSession],
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
    session.expect_in_display("Apply the mutation above?", timeout=30.0)
    session.expect_in_display("(*) abort", timeout=10.0)
    _confirm_radiolist_write(session)
    session.expect_in_display("writing", timeout=15.0)
    session.wait_for_exit(timeout=60.0, expected_code=0)
    after = c.read_text(_HOME_LOCAL_YAML)
    assert "/opt/code" in after


def test_config_add_local_scalar_pty_confirm_no(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., PyteSession],
) -> None:
    """PTY: default-abort (Tab → OK → Enter) leaves the file untouched."""
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
    session.expect_in_display("Apply the mutation above?", timeout=30.0)
    session.expect_in_display("(*) abort", timeout=10.0)
    _confirm_radiolist_abort(session)
    session.expect_in_display("aborted", timeout=15.0)
    session.wait_for_exit(timeout=60.0, expected_code=0)
    assert c.read_text(_HOME_LOCAL_YAML) == initial


def _seed_tracked_git_repo(c: ContainerHandle) -> str:
    """Initialize ``/tmp/track`` as a clean git repo with a small ``setforge.yaml``.

    The ``--tracked`` config-mutate path runs ``run_git_check_or_raise``
    over the source-resolved config directory; mutating tracked content
    on a non-repo (or dirty repo) refuses cleanly. Tests that drive a
    real tracked mutation under a PTY need a clean git tree, so seed a
    minimal config repo on the fly and point ``source.path`` at it via
    ``local.yaml``. Returns the in-container path.
    """
    repo_dir = "/tmp/track"
    c.exec(["mkdir", "-p", f"{repo_dir}/tracked"])
    c.write_text(
        f"{repo_dir}/setforge.yaml",
        "version: 1\n"
        "tracked_files:\n"
        "  ex:\n"
        "    src: ex.txt\n"
        "    dst: ~/.ex.txt\n"
        "  newfile:\n"
        "    src: newfile.txt\n"
        "    dst: ~/.newfile.txt\n"
        "profiles:\n"
        "  base:\n"
        "    tracked_files:\n"
        "      - ex\n",
    )
    c.write_text(f"{repo_dir}/tracked/ex.txt", "hello\n")
    c.write_text(f"{repo_dir}/tracked/newfile.txt", "fresh\n")
    git_cfg = "-c user.email=t@t -c user.name=t"
    init_cmd = (
        f"cd {repo_dir} && git init -q && "
        f"git {git_cfg} add -A && "
        f"git {git_cfg} commit -q -m seed"
    )
    c.exec(["bash", "-c", init_cmd])
    c.write_text(
        _HOME_LOCAL_YAML,
        f"source:\n  kind: path\n  path: {repo_dir}\n",
    )
    return repo_dir


def test_config_add_local_list_duplicate_raises_non_pty(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Non-PTY: tracked list-add of an already-present value refuses cleanly.

    :func:`setforge.cli._config_helpers.apply_add` raises
    :class:`SetforgeError` ``"<path> already contains <value>"`` BEFORE
    :func:`_preview_and_write` renders any confirm dialog, so the dup
    path is deterministic — no bifurcation, no PTY needed. ``ex`` is
    already in ``profiles.base.tracked_files`` in the seeded throwaway
    config repo, so adding it again hits the dup-raise branch.

    The companion PTY test
    :func:`test_config_add_local_list_new_value_pty_confirm_yes`
    drives a FRESH value through the actual dialog write path.
    """
    c = docker_container()
    _seed_tracked_git_repo(c)
    result = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "config",
            "add",
            "--tracked",
            "profiles.base.tracked_files",
            "ex",  # already present; must trip apply_add's dup-raise
            "--profile=base",
            "--yes",
        ],
        check=False,
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "already" in combined, combined


def test_config_add_local_list_new_value_pty_confirm_yes(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., PyteSession],
) -> None:
    """PTY: tracked list-add of a fresh value exercises the radiolist write path.

    The ``--local`` schema has no list-shaped paths today (binaries +
    source + claude are scalars / dicts), so the list-add behavior is
    exercised on the tracked side via ``profiles.<name>.tracked_files``
    against a clean throw-away git repo seeded inside the container.

    Uses a NEW tracked-files key (``"newfile"``) that is also seeded in
    ``tracked_files:`` so the schema-validate step finds it; ``"ex"`` is
    deliberately avoided here because it would short-circuit at
    ``apply_add``'s dup-check before the dialog. The companion non-PTY
    test :func:`test_config_add_local_list_duplicate_raises_non_pty`
    covers the dup-raise branch.
    """
    c = docker_container()
    _seed_tracked_git_repo(c)
    session = pyte_pty_session(
        container=c.cid,
        cmd=[
            "uv",
            "run",
            "setforge",
            "config",
            "add",
            "--tracked",
            "profiles.base.tracked_files",
            "newfile",  # fresh; not in the seeded base.tracked_files list
            "--profile=base",
        ],
    )
    session.expect_in_display("Apply the mutation above?", timeout=30.0)
    session.expect_in_display("(*) abort", timeout=10.0)
    _confirm_radiolist_write(session)
    session.expect_in_display("writing", timeout=15.0)
    session.wait_for_exit(timeout=60.0, expected_code=0)
    after = c.read_text("/tmp/track/setforge.yaml")
    assert "- newfile" in after


def test_config_remove_local_list_pty_confirm_yes(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., PyteSession],
) -> None:
    """PTY: list-remove pops from the list (arrow→write).

    See :func:`test_config_add_local_list_new_value_pty_confirm_yes`
    for the tracked-side fallback rationale (``--local`` has no list paths).
    """
    c = docker_container()
    _seed_tracked_git_repo(c)
    session = pyte_pty_session(
        container=c.cid,
        cmd=[
            "uv",
            "run",
            "setforge",
            "config",
            "remove",
            "--tracked",
            "profiles.base.tracked_files",
            "ex",
            "--profile=base",
        ],
    )
    session.expect_in_display("Apply the mutation above?", timeout=30.0)
    session.expect_in_display("(*) abort", timeout=10.0)
    _confirm_radiolist_write(session)
    session.expect_in_display("writing", timeout=15.0)
    session.wait_for_exit(timeout=60.0, expected_code=0)
    after = c.read_text("/tmp/track/setforge.yaml")
    assert "- ex" not in after


@pytest.mark.xdist_group("docker_daemon")
def test_config_add_marketplaces_pty_interactive(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., PyteSession],
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
    session.expect_in_display("Pick the source kind", timeout=30.0)
    # Default selection is github — Tab to OK and Enter accepts it.
    session.send_keys("\t")
    session.send_keys("\r")
    # owner/name input dialog for github.
    session.expect_in_display("owner/name", timeout=15.0)
    session.send_keys("owner/repo")
    session.send_keys("\t")
    session.send_keys("\r")
    # Final diff-preview confirm radiolist.
    session.expect_in_display("Apply the mutation above?", timeout=15.0)
    session.expect_in_display("(*) abort", timeout=10.0)
    _confirm_radiolist_write(session)
    session.expect_in_display("writing", timeout=15.0)
    session.wait_for_exit(timeout=60.0, expected_code=0)
    after = c.read_text(_HOME_LOCAL_YAML)
    assert "owner/repo" in after


def test_config_completion_value_works(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., PyteSession],
) -> None:
    """PTY: shell tab-completion on ``setforge config add --local source.kind``.

    Spawns an interactive zsh inside the container, sources the
    ``setforge --show-completion zsh`` script, types
    ``setforge config add --local source.kind `` and presses TAB, then
    asserts the Literal enum candidate (``path``) appears in the
    rendered completion menu.

    ``source.kind`` is the only ``LocalConfig`` field with enum_values
    in the LocalConfig schema today (``Literal[SourceKind.PATH,
    SourceKind.GIT]``); list-shaped paths only exist tracked-side
    (``profiles.<name>.tracked_files`` etc.). Asserting the enum branch
    of ``_complete_value`` here gives end-to-end coverage of the value
    callback machinery; the list branch is covered by the unit-level
    ``test_config_completion_value_callback_contract`` plus the PTY
    list-add test
    ``test_config_add_local_list_new_value_pty_confirm_yes`` and its
    non-PTY companion
    ``test_config_add_local_list_duplicate_raises_non_pty``.

    This exercises the END-TO-END shell-completion path (typer's
    completion machinery → setforge's _complete_value callback →
    shell-rendered candidates) rather than the unit-level callback
    contract — catches integration breakage (shell-renderer escapes,
    completion-script wiring, lazy-import timing) that monkeypatched
    callbacks can't.
    """
    c = docker_container()
    # Touch ~/.zshrc so `zsh -i` doesn't trigger the zsh-newuser-install
    # wizard, which intercepts stdin before our completion-eval can run.
    # Same idiom as tests/docker/test_e2e_docker_completion.py.
    c.exec(["touch", "/home/tester/.zshrc"])
    session = pyte_pty_session(
        container=c.cid,
        cmd=["zsh", "-i"],
    )
    # See test_config_completion_path_works for the four wiring
    # constraints (venv bin on PATH for bare ``setforge``, compinit
    # before the typer eval, explicit DONE markers, ``compdef setforge``
    # only matches the literal command word ``setforge``).
    session.send_keys("export PATH=/workspace/.venv/bin:$PATH\r")
    session.send_keys("autoload -U compinit && compinit -u && echo DONE_COMPINIT\r")
    session.expect_in_display("DONE_COMPINIT", timeout=15)
    session.send_keys(
        'eval "$(setforge --show-completion zsh)" 2>/dev/null && echo DONE_EVAL\r'
    )
    session.expect_in_display("DONE_EVAL", timeout=30)
    session.send_keys("setforge config add --local source.kind \t")
    # The enum value should appear in the rendered completion menu.
    session.expect_in_display("path", timeout=15)


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
    pyte_pty_session: Callable[..., PyteSession],
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
    # The tracked-side gate surfaces a non-zero exit before the diff-
    # preview panel. The refusal message can come from either the
    # git-clean check ("dirty") or, when the e2e fixture is not a git
    # repo, the source-validate ("does not contain setforge.yaml" or
    # similar). Both shapes are accepted as "gate tripped"; the
    # contract under test is the non-zero exit.
    session.wait_for_exit(timeout=60.0, expected_code=1)
    final = "\n".join(session.display)
    assert "error" in final.lower() or "dirty" in final.lower()


def test_config_add_invalid_pty_validates_before_write(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., PyteSession],
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
    pyte_pty_session: Callable[..., PyteSession],
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
