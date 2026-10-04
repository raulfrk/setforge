"""Every file a transition producer changes is recorded as a pre/post image."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from setforge import operations, transitions
from setforge.transitions import FilesystemKind
from tests.shared_helpers import assert_patch_matches_images

from .conftest import IntegrationEnv

pytestmark = pytest.mark.integration

_NOTE = ".setforge_it/text/note.txt"
_SETTINGS = ".setforge_it/json/settings.json"


def _records(env: IntegrationEnv) -> list[transitions.TransitionDir]:
    root = env.state_dir / "transitions"
    return [
        transitions.TransitionDir(path)
        for path in sorted(transitions.committed_transition_dirs(root))
    ]


def _payload(image: transitions.FilesystemImage) -> bytes | None:
    return image.payload if image.kind is FilesystemKind.FILE else None


def _file_changes(
    transition: transitions.TransitionDir, under: Path
) -> dict[Path, tuple[bytes | None, bytes | None]]:
    return {
        item.path: (_payload(item.pre), _payload(item.post))
        for item in transitions.load_filesystem_deltas(transition)
        if item.path.is_relative_to(under)
    }


def test_install_records_every_changed_file(
    integration_env: Callable[..., IntegrationEnv],
    integration_subprocess,
) -> None:
    env = integration_env()
    note = env.live(_NOTE)
    settings = env.live(_SETTINGS)
    assert env.run_verb(["install", "--no-git-check"]).exit_code == 0
    created = _records(env)[-1]
    assert _file_changes(created, env.home) == {
        note: (None, note.read_bytes()),
        settings: (None, settings.read_bytes()),
    }
    assert_patch_matches_images(created)

    before = note.read_bytes()
    env.tracked("text/note.txt").write_bytes(b"changed\r\nbody")
    env.tracked("json/settings.json").chmod(0o600)
    mode_before = settings.stat().st_mode & 0o7777
    assert env.run_verb(["install", "--no-git-check"]).exit_code == 0
    updated = _records(env)[-1]

    assert _file_changes(updated, env.home) == {
        note: (before, b"changed\r\nbody"),
        settings: (settings.read_bytes(), settings.read_bytes()),
    }
    by_path = {item.path: item for item in transitions.load_filesystem_deltas(updated)}
    assert by_path[settings].pre.mode == mode_before
    assert by_path[settings].post.mode == 0o600
    assert transitions.load_file_modes(updated) == {settings: mode_before}
    assert_patch_matches_images(updated)


def test_sync_and_its_revert_record_every_changed_file(
    integration_env: Callable[..., IntegrationEnv],
    integration_subprocess,
) -> None:
    env = integration_env()
    assert env.run_verb(["install", "--no-git-check"]).exit_code == 0
    tracked = env.tracked("text/note.txt")
    before = tracked.read_bytes()
    env.live(_NOTE).write_bytes(b"live edit\n")

    result = env.run_verb(["sync", "--auto=use-live", "--yes"])
    assert result.exit_code == 0, result.output
    synced = _records(env)[-1]
    assert _file_changes(synced, env.repo) == {tracked: (before, b"live edit\n")}
    assert_patch_matches_images(synced)

    result = env.run_verb(["revert", "--yes"])
    assert result.exit_code == 0, result.output
    redo = _records(env)[-1]
    assert _file_changes(redo, env.repo) == {tracked: (b"live edit\n", before)}
    assert transitions.load_meta_payload(redo)["paths"] == [str(tracked)]


def test_revert_ignores_a_changed_modification_time(
    integration_env: Callable[..., IntegrationEnv],
    integration_subprocess,
) -> None:
    env = integration_env()
    note = env.live(_NOTE)
    assert env.run_verb(["install", "--no-git-check"]).exit_code == 0
    before = note.read_bytes()
    env.tracked("text/note.txt").write_bytes(b"changed\n")
    assert env.run_verb(["install", "--no-git-check"]).exit_code == 0
    os.utime(note, ns=(1_000_000_000, 1_000_000_000))

    result = env.run_verb(["revert", "--yes"])

    assert result.exit_code == 0, result.output
    assert note.read_bytes() == before


def test_revert_refuses_a_file_edited_since_before_writing_any_file(
    integration_env: Callable[..., IntegrationEnv],
    integration_subprocess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = integration_env()
    note = env.live(_NOTE)
    settings = env.live(_SETTINGS)
    assert env.run_verb(["install", "--no-git-check"]).exit_code == 0
    env.tracked("text/note.txt").write_bytes(b"line 1\nline 2\nline 3\n")
    env.tracked("json/settings.json").write_bytes(b'{"a": 2}\n')
    assert env.run_verb(["install", "--no-git-check"]).exit_code == 0
    # An edit far from the line the install changed is still a change; the
    # edited file is the one the reversal would write last.
    note.write_bytes(b"line 1\nline 2\nline 3\nmine\n")
    records = _records(env)
    writes: list[object] = []
    monkeypatch.setattr(
        operations,
        "apply_filesystem_deltas_reverse_anchored",
        lambda *args, **kwargs: writes.append(args),
    )

    result = env.run_verb(["revert", "--yes"])

    assert writes == []
    assert result.exit_code == 1
    assert f"filesystem path changed since transition: {note}" in str(result.exception)
    assert settings.read_bytes() == b'{"a": 2}\n'
    assert note.read_bytes() == b"line 1\nline 2\nline 3\nmine\n"
    assert _records(env) == records
