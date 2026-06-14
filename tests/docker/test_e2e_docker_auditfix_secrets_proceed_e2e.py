"""Docker e2e: the secrets-scan PROCEED paths (ALLOWLIST / SILENCE_ONE_SHOT).

The interactive secrets-confirm wizard
(:func:`setforge.cli._secrets_confirm.prompt_secret_action`) offers three
actions when gitleaks flags a tracked file during ``install``:

* ``ABORT``            (row 0, default) — refuse to deploy.
* ``ALLOWLIST``        (row 1) — persist ``sha256(snippet)`` to the host-local
  allowlist at ``~/.config/setforge/secrets-allowlist`` AND continue deploying;
  the hash silences the SAME finding on every future install.
* ``SILENCE_ONE_SHOT`` (row 2) — continue THIS run only, WITHOUT writing the
  allowlist; a later install re-scans and aborts again.

The wizard renders through prompt_toolkit's full-screen ``radiolist_dialog``,
which only paints under a TTY (non-TTY stdin short-circuits to ABORT). The
existing suite (``test_e2e_docker.py``) covers only the non-TTY ABORT path and
the missing-gitleaks warn path; neither PROCEED branch had end-to-end coverage.
Per the project's e2e conventions for full-screen prompt panels these drive the
wizard through :func:`pyte_pty_session`, anchoring on the EMULATED screen
(``.display``) rather than the raw pexpect byte stream.

Secret recipe (mirrors ``test_e2e_docker_install_secrets_scan_finds_and_aborts``)
---------------------------------------------------------------------------------
Plant a fake GitHub Personal Access Token (one of gitleaks' built-in rules) into
the ``test-minimal`` tracked source inside the container workspace. The token is
assembled by string concat so pre-commit's own gitleaks hook does NOT trip on
this test file at commit time — only the runtime gitleaks invocation inside the
container sees the fully-formed pattern.

radiolist row order
-------------------
``values=[(ABORT, ...), (ALLOWLIST, ...), (SILENCE_ONE_SHOT, ...)]`` with
``default=ABORT`` selected on entry, so one ``\\x1b[B`` (arrow down) moves the
selection to ALLOWLIST and two move it to SILENCE_ONE_SHOT. ``\\r`` confirms.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle
from tests.docker.pyte_session import PyteSession

pytestmark = pytest.mark.e2e_docker

# Fake GitHub PAT assembled at runtime so the pre-commit gitleaks hook does NOT
# trip on this source file; only the container's gitleaks sees the joined value.
_FAKE_TOKEN = "ghp_" + "x6Hv9Kp2zQwL8mN3rF7tY1bC4dE5gJ0sA9iU"
_PLANTED = f"hello from test-minimal\nfake gh pat for secrets-scan e2e: {_FAKE_TOKEN}\n"

# Tracked source inside the container workspace + its live destination.
_TRACKED_TXT = "/workspace/tests/fixtures/e2e/tracked/minimal/text.txt"
_LIVE_TXT = "/home/tester/.setforge_e2e/minimal/text.txt"
_ALLOWLIST = "/home/tester/.config/setforge/secrets-allowlist"

_INSTALL_CMD = [
    "uv",
    "run",
    "setforge",
    "install",
    "--profile=test-minimal",
    f"--config={CONFIG_FIXTURE}",
]

# Anchor the wizard render on the dialog's stable prompt text (the Rich panel
# title may land split across cursor-positioned bytes, but the radiolist text
# is painted as a contiguous cell run).
_DIALOG_ANCHOR = "How would you like to proceed?"


def _plant_secret(c: ContainerHandle) -> None:
    """Plant the fake PAT in tracked + clear any leftover live dst + allowlist."""
    c.write_text(_TRACKED_TXT, _PLANTED)
    # The live dst must not pre-exist so a post-deploy assertion cannot be
    # satisfied by a previous-install leftover.
    c.exec(["rm", "-f", _LIVE_TXT], check=False)
    # Ensure no allowlist carries over from image baseline.
    c.exec(["rm", "-f", _ALLOWLIST], check=False)


def _drive_wizard(
    pyte_pty_session: Callable[..., PyteSession],
    c: ContainerHandle,
    downs: int,
) -> PyteSession:
    """Spawn the interactive install under pyte, select a radiolist row, confirm.

    ``downs`` arrow-down presses move the selection off the default ABORT
    row (0): ``downs=1`` selects ALLOWLIST, ``downs=2`` selects
    SILENCE_ONE_SHOT. Submitting a ``radiolist_dialog`` requires the full
    sequence — arrow to highlight, Enter to commit the radio, Tab to focus
    the OK button, Enter to submit — after which the install proceeds to
    deploy, exiting 0. (A bare trailing Enter only toggles the radio and
    leaves the dialog open, hanging the process.)
    """
    session = pyte_pty_session(
        container=c.cid,
        cmd=_INSTALL_CMD,
        timeout=120.0,
    )
    session.expect_in_display(_DIALOG_ANCHOR, timeout=60.0)
    session.send_keys("\x1b[B" * downs)  # move selection off ABORT
    session.send_keys("\r")  # commit the radio
    session.send_keys("\t")  # focus the OK button
    session.send_keys("\r")  # submit the dialog
    session.wait_for_exit(timeout=60, expected_code=0)
    return session


# ---------------------------------------------------------------------------
# 1: ALLOWLIST — proceeds, deploys, persists the hash, silences the re-scan.
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_e2e_docker_install_secrets_scan_allowlist_proceeds(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., PyteSession],
) -> None:
    """ALLOWLIST: deploy proceeds, snippet hash persists, re-scan stays silent.

    Drive the radiolist down one row (ABORT → ALLOWLIST) + Enter. Then:

    1. The live dst IS written (the flagged file deployed).
    2. The host-local allowlist file exists and is non-empty (a sha256 hash
       line was persisted).
    3. A SECOND, non-interactive install no longer aborts — the persisted
       hash filters the finding out of the re-scan, so the soft ABORT line
       does not reappear and the install exits 0.
    """
    c = docker_container()
    _plant_secret(c)

    _drive_wizard(pyte_pty_session, c, downs=1)

    # 1: flagged file deployed.
    assert c.read_text(_LIVE_TXT) == _PLANTED, (
        "ALLOWLIST must deploy the flagged tracked file to the live dst"
    )
    # 2: allowlist persisted with at least one 64-hex sha256 line.
    exists = c.exec(["test", "-f", _ALLOWLIST], check=False)
    assert exists.returncode == 0, "ALLOWLIST must create the host-local allowlist"
    allow_body = c.read_text(_ALLOWLIST)
    hash_lines = [
        ln.strip()
        for ln in allow_body.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    assert any(
        len(ln) == 64 and all(ch in "0123456789abcdef" for ch in ln)
        for ln in hash_lines
    ), f"allowlist must contain a sha256 snippet hash; body={allow_body!r}"

    # 3: re-scan is silenced by the persisted hash — non-TTY install no longer
    # aborts (the finding is filtered before the wizard would even fire).
    rerun = c.exec(_INSTALL_CMD, check=False)
    assert rerun.returncode == 0, rerun.stderr or rerun.stdout
    assert "install aborted by secrets scan" not in rerun.stderr, (
        f"persisted allowlist hash must silence the re-scan; stderr={rerun.stderr!r}"
    )


# ---------------------------------------------------------------------------
# 2: SILENCE_ONE_SHOT — proceeds THIS run only, no allowlist, re-scan aborts.
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_e2e_docker_install_secrets_scan_silence_one_shot_proceeds(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., PyteSession],
) -> None:
    """SILENCE_ONE_SHOT: deploy proceeds once, NO allowlist, next scan aborts.

    Drive the radiolist down two rows (ABORT → ALLOWLIST → SILENCE_ONE_SHOT)
    + Enter. Then:

    1. The live dst IS written (the flagged file deployed this run).
    2. The allowlist file is NOT created (silence was one-shot, not persisted).
    3. A SECOND, non-interactive install aborts AGAIN — proving the silence
       did not persist: the re-scan still finds the unallowlisted secret and
       the non-TTY wizard short-circuits to ABORT.
    """
    c = docker_container()
    _plant_secret(c)

    _drive_wizard(pyte_pty_session, c, downs=2)

    # 1: flagged file deployed this run.
    assert c.read_text(_LIVE_TXT) == _PLANTED, (
        "SILENCE_ONE_SHOT must deploy the flagged tracked file this run"
    )
    # 2: allowlist NOT created — silence was not persisted.
    exists = c.exec(["test", "-f", _ALLOWLIST], check=False)
    assert exists.returncode != 0, (
        "SILENCE_ONE_SHOT must NOT write the host-local allowlist"
    )

    # 3: a second non-TTY install re-scans and aborts again (one-shot proven).
    # Remove the just-deployed dst so the abort assertion below cannot be
    # confused with a no-op; the abort line on stderr is the real signal.
    c.exec(["rm", "-f", _LIVE_TXT], check=False)
    rerun = c.exec(_INSTALL_CMD, check=False)
    assert "install aborted by secrets scan" in rerun.stderr, (
        f"one-shot silence must NOT persist; second scan must abort; "
        f"stderr={rerun.stderr!r}"
    )
    second_exists = c.exec(["test", "-f", _LIVE_TXT], check=False)
    assert second_exists.returncode != 0, (
        "second install must abort before deploying (one-shot silence expired)"
    )
