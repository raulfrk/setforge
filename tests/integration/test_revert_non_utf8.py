"""Undecodable and line-ending-only tracked files round-trip through revert."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from .conftest import IntegrationEnv

pytestmark = pytest.mark.integration

_REL = ".setforge_it/data/blob.bin"
_PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff\xfe\x00\n\x00end"
_PNG_LIVE = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff\xfe\x01\n\x00live"
_PNG_NEXT = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff\xfe\x02\n\x00next\xc3"
_LATIN1 = "caf\xe9\nna\xefve\n".encode("latin-1")
_LATIN1_LIVE = "caf\xe9\nna\xefve live\n".encode("latin-1")
_LATIN1_NEXT = "caf\xe9 au lait\nna\xefve\n".encode("latin-1")

_CASES = {
    "png": (_PNG, _PNG_LIVE, _PNG_NEXT),
    "latin1": (_LATIN1, _LATIN1_LIVE, _LATIN1_NEXT),
}


def _install(env: IntegrationEnv) -> None:
    result = env.run_verb(["install", "--no-git-check", "--yes"])
    assert result.exit_code == 0, result.output
    assert "not valid UTF-8" not in result.output


@pytest.mark.parametrize("name", sorted(_CASES))
def test_non_utf8_file_through_every_verb(
    name: str,
    integration_env: Callable[..., IntegrationEnv],
    integration_subprocess,
) -> None:
    first, live_edit, nxt = _CASES[name]
    env = integration_env(tracked={"blob": ("data/blob.bin", "seed\n")})
    tracked = env.tracked("data/blob.bin")
    live = env.live(_REL)
    tracked.write_bytes(first)

    _install(env)
    assert live.read_bytes() == first
    assert env.run_verb(["compare", "--check", "--strict"]).exit_code == 0

    live.write_bytes(live_edit)
    result = env.run_verb(["sync", "--auto=use-live", "--yes"])
    assert result.exit_code == 0, result.output
    assert tracked.read_bytes() == live_edit

    result = env.run_verb(["revert", "--yes"])
    assert result.exit_code == 0, result.output
    assert tracked.read_bytes() == first
    result = env.run_verb(["revert", "--yes"])
    assert result.exit_code == 0, result.output
    assert tracked.read_bytes() == live_edit

    tracked.write_bytes(nxt)
    _install(env)
    assert live.read_bytes() == nxt

    result = env.run_verb(["revert", "--yes"])
    assert result.exit_code == 0, result.output
    assert live.read_bytes() == live_edit
    result = env.run_verb(["revert", "--yes"])
    assert result.exit_code == 0, result.output
    assert live.read_bytes() == nxt


def test_line_ending_only_install_is_recorded_and_reverted(
    integration_env: Callable[..., IntegrationEnv],
    integration_subprocess,
) -> None:
    env = integration_env(tracked={"note": ("text/note.txt", "seed\n")})
    tracked = env.tracked("text/note.txt")
    live = env.live(".setforge_it/text/note.txt")
    tracked.write_bytes(b"a\nb\n")
    _install(env)
    tracked.write_bytes(b"a\r\nb\r\n")
    _install(env)
    assert live.read_bytes() == b"a\r\nb\r\n"

    result = env.run_verb(["revert", "--yes"])
    assert result.exit_code == 0, result.output
    assert live.read_bytes() == b"a\nb\n"
    result = env.run_verb(["revert", "--yes"])
    assert result.exit_code == 0, result.output
    assert live.read_bytes() == b"a\r\nb\r\n"


def test_line_ending_only_sync_is_recorded_and_reverted(
    integration_env: Callable[..., IntegrationEnv],
    integration_subprocess,
) -> None:
    env = integration_env(tracked={"note": ("text/note.txt", "seed\n")})
    tracked = env.tracked("text/note.txt")
    live = env.live(".setforge_it/text/note.txt")
    tracked.write_bytes(b"a\nb\n")
    _install(env)
    live.write_bytes(b"a\r\nb\r\n")

    result = env.run_verb(["sync", "--auto=use-live", "--yes"])
    assert result.exit_code == 0, result.output
    assert tracked.read_bytes() == b"a\r\nb\r\n"

    result = env.run_verb(["revert", "--yes"])
    assert result.exit_code == 0, result.output
    assert tracked.read_bytes() == b"a\nb\n"


def test_crlf_yaml_install_then_revert(
    integration_env: Callable[..., IntegrationEnv],
    integration_subprocess,
) -> None:
    env = integration_env(tracked={"conf": ("conf/app.yaml", "a: 1\n")})
    tracked = env.tracked("conf/app.yaml")
    live = env.live(".setforge_it/conf/app.yaml")
    tracked.write_bytes(b"# note\r\na: 1\r\nb: 2\r\n")
    _install(env)
    assert live.read_bytes() == b"# note\r\na: 1\r\nb: 2\r\n"
    tracked.write_bytes(b"# note\r\na: 1\r\nb: 3\r\n")
    _install(env)
    assert live.read_bytes() == b"# note\r\na: 1\r\nb: 3\r\n"

    result = env.run_verb(["revert", "--yes"])
    assert result.exit_code == 0, result.output
    assert live.read_bytes() == b"# note\r\na: 1\r\nb: 2\r\n"
    result = env.run_verb(["revert", "--yes"])
    assert result.exit_code == 0, result.output
    assert live.read_bytes() == b"# note\r\na: 1\r\nb: 3\r\n"
