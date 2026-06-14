"""Docker E2E: ``setforge status`` against a real installed profile.

Pre-audit, ``status`` had ZERO e2e coverage — ``rg '"status"'
tests/docker/*.py`` matched only git-porcelain calls and JSON
``entry["status"]`` field reads from OTHER commands, never ``setforge
status`` itself. Yet ``status`` is a substantial read-only command whose
behavior is overwhelmingly integration-emergent:

- it resolves the config repo through the source layer
  (``~/.config/setforge/local.yaml`` path source),
- shells out to the real ``git`` binary on that repo to parse HEAD short
  sha, commits-since-last-install (``rev-list --count <sha>..HEAD``), and
  commits-vs-``origin/main``,
- reads the most recent INSTALL transition meta (and the ``source_sha``
  it carries) off disk,
- computes drift counts against the deployed live files via a real
  ``compare`` pass, and
- renders BOTH a human screen and a versioned ``-o json`` envelope.

None of that runs under a unit test with mocks — a regression in git
output parsing, source-dir resolution, last-install meta read, or drift
counting on ``status`` would ship undetected. These tests drive the
command end-to-end against a fresh container with a real ``git`` repo as
the source, asserting both the human render and the stable JSON envelope.

Self-contained: each test git-inits its own config repo under
``/tmp/cfg`` and points a path ``source:`` at it via the home
``local.yaml`` so the shared baked-in fixture tree is never touched.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from tests.docker.conftest import ContainerHandle

pytestmark = pytest.mark.e2e_docker

_SRC_REPO = "/tmp/cfg"
_SRC_YAML = f"{_SRC_REPO}/setforge.yaml"
_HOME_LOCAL_YAML = "/home/tester/.config/setforge/local.yaml"
_LIVE_DST = "/home/tester/.setforge_e2e/status/text.txt"
_TRACKED_BODY = "hello from status e2e\n"


def _bootstrap_git_source(c: ContainerHandle) -> None:
    """Create a git-initialised config repo at ``_SRC_REPO`` + path source.

    A single-file ``base`` profile (plain byte-copy tracked file) is the
    floor case — it isolates ``status``'s git / meta / drift logic from
    any disposition or overlay machinery. The repo is a real git working
    tree with one commit so ``status``'s ``git`` subprocess parsing runs
    against true output (HEAD short sha, ``rev-list --count`` since the
    install sha, ``rev-parse --verify origin/main``).
    """
    c.write_text(
        _SRC_YAML,
        "version: 1\n"
        "schema_version: '1.0'\n"
        "tracked_files:\n"
        "  status_text:\n"
        "    src: status/text.txt\n"
        f"    dst: {_LIVE_DST}\n"
        "profiles:\n"
        "  base:\n"
        "    tracked_files:\n"
        "      - status_text\n",
    )
    c.write_text(f"{_SRC_REPO}/tracked/status/text.txt", _TRACKED_BODY)
    c.write_text(
        _HOME_LOCAL_YAML,
        f"source:\n  kind: path\n  path: {_SRC_REPO}\n",
    )
    # Real git working tree with one commit so status's git parsing has
    # genuine output to consume (otherwise it short-circuits to the
    # "config dir not a git repo" placeholder and the git block is inert).
    c.exec(["git", "init", "-q", _SRC_REPO], check=True)
    c.exec(["git", "-C", _SRC_REPO, "config", "user.email", "e2e@example.com"])
    c.exec(["git", "-C", _SRC_REPO, "config", "user.name", "e2e"])
    c.exec(["git", "-C", _SRC_REPO, "add", "-A"], check=True)
    c.exec(["git", "-C", _SRC_REPO, "commit", "-qm", "init"], check=True)


def _install(c: ContainerHandle) -> None:
    """Install the ``base`` profile from the path source; assert exit 0."""
    res = c.exec(
        ["uv", "run", "setforge", "install", "--profile=base", f"--config={_SRC_YAML}"],
        check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert c.read_text(_LIVE_DST) == _TRACKED_BODY


def _status(c: ContainerHandle, *, json_mode: bool = False) -> tuple[int, str, str]:
    """Run ``setforge status --profile=base``; (rc, stdout, stderr).

    With ``json_mode`` the global ``-o json`` option precedes the
    subcommand so the versioned envelope lands on stdout.
    """
    prefix = ["uv", "run", "setforge"]
    if json_mode:
        prefix += ["-o", "json"]
    res = c.exec([*prefix, "status", "--profile=base"], check=False)
    return res.returncode, res.stdout, res.stderr


def test_status_after_install_reports_clean_human_and_json(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``status`` after a clean install: human screen + JSON envelope agree.

    Drives the full read-only pipeline against a real installed profile
    and a real git source repo. The human render must surface the
    config-repo line (with the source dir and a resolved short sha), a
    recorded last-install row, and a zero-drift line. The ``-o json``
    envelope must carry the versioned shape with every status section
    populated: a resolved ``head_short`` and ``commits_since_install: 0``
    (HEAD == install sha), a non-null ``last_install`` block, and
    all-zero drift.
    """
    c = docker_container()
    _bootstrap_git_source(c)
    _install(c)

    rc, stdout, stderr = _status(c)
    combined = stdout + stderr
    assert rc == 0, combined
    # config-repo section: the resolved source dir + a real 7-char sha.
    assert f"config-repo:    {_SRC_REPO}" in stdout, combined
    assert "(no HEAD)" not in stdout, combined
    # last-install section: a transition was recorded (not the empty form).
    assert "last install:" in stdout, combined
    assert "no transitions recorded" not in stdout, combined
    # drift section: nothing diverged → all zeros.
    assert "drift:          0 unexpected, 0 user-section drift" in stdout, combined

    rc, stdout, stderr = _status(c, json_mode=True)
    combined = stdout + stderr
    assert rc == 0, combined
    envelope = json.loads(stdout)
    assert envelope["command"] == "status", envelope
    assert "schema_version" in envelope, envelope
    data = envelope["data"]
    assert data["profile"] == "base", data
    # config_repo block: real git parse produced a sha + a clean since-count.
    repo = data["config_repo"]
    assert repo["source_dir"] == _SRC_REPO, repo
    assert repo["head_short"], repo
    assert repo["commits_since_install"] == 0, repo
    assert repo["commits_since_install_reason"] is None, repo
    # last_install block populated from the recorded INSTALL transition.
    assert data["last_install"] is not None, data
    assert data["last_install"]["command"] == "install", data
    # drift all-zero and the capability rows are present.
    assert data["drift"] == {
        "unexpected": 0,
        "user_section": 0,
        "expected": 0,
    }, data
    assert isinstance(data["capabilities"], list), data
    assert data["capabilities"], data


def test_status_reports_drift_after_live_edit(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Dirtying one deployed live file makes ``status`` report drift.

    After a clean install, overwrite the deployed live file so it diverges
    from tracked. ``status`` (still exit 0 — it is informational) must now
    count the divergence: the human ``drift:`` line shows a non-zero
    unexpected count and the JSON ``drift.unexpected`` field increments
    from 0. This guards the real ``compare``-driven drift computation that
    a mocked unit test cannot exercise.
    """
    c = docker_container()
    _bootstrap_git_source(c)
    _install(c)

    # Clean baseline: zero unexpected drift in JSON.
    rc, stdout, _ = _status(c, json_mode=True)
    assert rc == 0, stdout
    assert json.loads(stdout)["data"]["drift"]["unexpected"] == 0, stdout

    # Diverge the deployed live file from tracked.
    c.write_text(_LIVE_DST, "tampered live content\n")

    rc, stdout, stderr = _status(c)
    combined = stdout + stderr
    assert rc == 0, combined  # status stays informational on drift.
    assert "drift:          0 unexpected" not in stdout, combined

    rc, stdout, stderr = _status(c, json_mode=True)
    combined = stdout + stderr
    assert rc == 0, combined
    drift = json.loads(stdout)["data"]["drift"]
    assert drift["unexpected"] >= 1, drift
