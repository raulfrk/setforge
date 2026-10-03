"""Revert and redo restore exact bytes and work through symlinked destinations."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from .conftest import IntegrationEnv

pytestmark = pytest.mark.integration

_REL = ".setforge_it/text/note.txt"


def _tracked_only() -> dict[str, tuple[str, str]]:
    return {"note": ("text/note.txt", "seed\n")}


def _set_tracked(env: IntegrationEnv, data: bytes) -> None:
    env.tracked("text/note.txt").write_bytes(data)


_BYTE_CASES = {
    "crlf": (b"a\r\nb\r\n", b"a\r\nb\r\nc\r\n"),
    "crlf-changed-line": (b"a\r\nb\r\n", b"a\r\nB\r\n"),
    "lone-cr": (b"a\rb\r", b"a\rB\r"),
    "mixed-endings": (b"a\r\nb\nc\r", b"a\r\nb\nc\r\nd"),
    "form-feed": (b"one\nsec\x0cond\nthree\n", b"one\nsec\x0cond\nthree\nfour\n"),
    "line-separator": (
        "one\nsec\u2028ond\nthree\n".encode(),
        "one\nsec\u2028ond\nthree\nfour\n".encode(),
    ),
    "next-line": (
        "one\nsec\x85ond\n".encode(),
        "one\nsec\x85ond\nthree\n".encode(),
    ),
    "no-final-newline": (b"a\nb", b"a\nb\nc"),
}


@pytest.mark.parametrize("name", sorted(_BYTE_CASES))
def test_revert_and_redo_restore_exact_bytes(
    name: str,
    integration_env: Callable[..., IntegrationEnv],
    integration_subprocess,
) -> None:
    before, after = _BYTE_CASES[name]
    env = integration_env(tracked=_tracked_only())
    live = env.live(_REL)
    _set_tracked(env, before)
    assert env.run_verb(["install", "--no-git-check"]).exit_code == 0
    assert live.read_bytes() == before
    _set_tracked(env, after)
    assert env.run_verb(["install", "--no-git-check"]).exit_code == 0
    assert live.read_bytes() == after

    result = env.run_verb(["revert", "--yes"])
    assert result.exit_code == 0, result.output
    assert live.read_bytes() == before

    result = env.run_verb(["revert", "--yes"])
    assert result.exit_code == 0, result.output
    assert live.read_bytes() == after


def test_older_transition_with_crlf_can_be_reverted(
    integration_env: Callable[..., IntegrationEnv],
    integration_subprocess,
) -> None:
    env = integration_env(tracked=_tracked_only())
    live = env.live(_REL)
    _set_tracked(env, b"a\r\nb\r\n")
    assert env.run_verb(["install", "--no-git-check"]).exit_code == 0
    _set_tracked(env, b"a\r\nb\r\nc\r\n")
    assert env.run_verb(["install", "--no-git-check"]).exit_code == 0
    first = sorted((env.state_dir / "transitions").iterdir())[0].name

    result = env.run_verb(["revert", "--yes", f"--to-before={first}"])

    assert result.exit_code == 0, result.output
    assert not live.exists()


def test_revert_through_symlinked_destination_directory(
    integration_env: Callable[..., IntegrationEnv],
    integration_subprocess,
    tmp_path: Path,
) -> None:
    env = integration_env(tracked=_tracked_only())
    real = tmp_path / "elsewhere"
    real.mkdir()
    (env.home / ".setforge_it").symlink_to(real)
    assert env.run_verb(["install", "--no-git-check"]).exit_code == 0
    _set_tracked(env, b"seed\nmore\n")
    assert env.run_verb(["install", "--no-git-check"]).exit_code == 0

    result = env.run_verb(["revert", "--yes"])

    assert result.exit_code == 0, result.output
    assert (real / "text/note.txt").read_text() == "seed\n"
    result = env.run_verb(["revert", "--yes"])
    assert result.exit_code == 0, result.output
    assert (real / "text/note.txt").read_text() == "seed\nmore\n"


def test_revert_when_destination_is_a_symlink(
    integration_env: Callable[..., IntegrationEnv],
    integration_subprocess,
    tmp_path: Path,
) -> None:
    env = integration_env(tracked=_tracked_only())
    target = tmp_path / "managed-elsewhere.txt"
    target.write_text("seed\n")
    live = env.live(_REL)
    live.parent.mkdir(parents=True)
    live.symlink_to(target)
    assert env.run_verb(["install", "--no-git-check", "--yes"]).exit_code == 0
    _set_tracked(env, b"seed\nmore\n")
    assert env.run_verb(["install", "--no-git-check", "--yes"]).exit_code == 0
    assert live.is_symlink()

    result = env.run_verb(["revert", "--yes"])

    assert result.exit_code == 0, result.output
    assert live.is_symlink()
    assert target.read_text() == "seed\n"
