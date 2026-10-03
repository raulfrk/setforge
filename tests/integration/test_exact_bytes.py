"""Install / compare / sync keep file bytes exact (line endings, non-UTF-8)."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable

import pytest

from .conftest import IntegrationEnv

pytestmark = pytest.mark.integration

_NOTE = ".setforge_it/text/note.txt"


def _commit(env: IntegrationEnv) -> None:
    git_env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t.invalid",
    }
    for args in (["add", "-A"], ["commit", "-q", "-m", "edit"]):
        subprocess.run(
            ["git", *args], cwd=env.repo, env=git_env, check=True, capture_output=True
        )


def _env(
    integration_env: Callable[..., IntegrationEnv], tracked: bytes
) -> IntegrationEnv:
    env = integration_env(tracked={"note": ("text/note.txt", "seed\n")})
    env.tracked("text/note.txt").write_bytes(tracked)
    _commit(env)
    return env


def _no_traceback(result: object) -> None:
    exception = getattr(result, "exception", None)
    assert exception is None or isinstance(exception, SystemExit), repr(exception)


def test_sync_keeps_crlf_bytes_of_live_edit(
    integration_env: Callable[..., IntegrationEnv], integration_subprocess
) -> None:
    env = _env(integration_env, b"a\r\nb\r\nc\r\n")
    assert env.run_verb(["install", "--yes"]).exit_code == 0
    assert env.live(_NOTE).read_bytes() == b"a\r\nb\r\nc\r\n"
    env.live(_NOTE).write_bytes(b"a\r\nB\r\nc\r\n")
    result = env.run_verb(["sync", "--auto=use-live", "--yes"])
    assert result.exit_code == 0, result.output
    assert env.tracked("text/note.txt").read_bytes() == b"a\r\nB\r\nc\r\n"


@pytest.mark.parametrize(
    ("tracked", "live"),
    [(b"a\nb\n", b"a\r\nb\r\n"), (b"a\r\nb\r\n", b"a\nb\n")],
)
def test_line_ending_only_difference_is_drift(
    integration_env: Callable[..., IntegrationEnv],
    integration_subprocess,
    tracked: bytes,
    live: bytes,
) -> None:
    env = _env(integration_env, tracked)
    assert env.run_verb(["install", "--yes"]).exit_code == 0
    env.live(_NOTE).write_bytes(live)
    result = env.run_verb(["compare", "--check"])
    assert result.exit_code != 0, result.output


_BINARY = b"\x89PNG\r\n\x1a\n\x00caf\xe9\xff\n"


def test_undecodable_tracked_file_compare_has_no_traceback(
    integration_env: Callable[..., IntegrationEnv], integration_subprocess
) -> None:
    env = _env(integration_env, _BINARY)
    env.live(_NOTE).parent.mkdir(parents=True)
    env.live(_NOTE).write_bytes(_BINARY)
    clean = env.run_verb(["compare", "--check"])
    _no_traceback(clean)
    assert clean.exit_code == 0, clean.output
    env.live(_NOTE).write_bytes(_BINARY + b"\x00")
    drifted = env.run_verb(["compare", "--check"])
    _no_traceback(drifted)
    assert drifted.exit_code == 1, drifted.output


def test_compare_reports_tracked_source_missing_from_repo(
    integration_env: Callable[..., IntegrationEnv], integration_subprocess
) -> None:
    env = _env(integration_env, b"a\n")
    assert env.run_verb(["install", "--yes"]).exit_code == 0
    env.tracked("text/note.txt").unlink()
    _commit(env)
    result = env.run_verb(["compare", "--check"])
    _no_traceback(result)
    assert result.exit_code == 1
    assert "tracked source missing" in result.output


@pytest.mark.parametrize("bad", [b'{\n  "a": 1,\n  "L', b""])
def test_sync_refuses_live_json_that_does_not_parse(
    integration_env: Callable[..., IntegrationEnv],
    integration_subprocess,
    bad: bytes,
) -> None:
    env = integration_env()
    assert env.run_verb(["install", "--yes"]).exit_code == 0
    live = env.live(".setforge_it/json/settings.json")
    tracked = env.tracked("json/settings.json")
    before = tracked.read_bytes()
    live.write_bytes(bad)
    result = env.run_verb(["sync", "--auto=use-live", "--yes"])
    assert result.exit_code != 0
    assert "settings.json" in f"{result.output}{result.exception}"
    assert tracked.read_bytes() == before
