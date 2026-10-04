"""Project injection: inject, sync and remove, hidden and tracked overlays.

Removal puts back exactly what preceded the injection, and sync merges YAML
and JSON members to the bytes install would give."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from click.testing import Result

from .support import (
    PROJECT_YAML,
    Host,
    candidate_setforge_on_path,
    git,
    git_repo,
)

pytestmark = pytest.mark.integration

_TEAM = b"team instructions\n"
_PROFILE = b"team instructions\nmanaged instructions\n"


def _host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: bytes = b"managed\n",
    member: str = "AGENTS.md",
    file_id: str = "agents",
) -> Host:
    candidate_setforge_on_path(tmp_path, monkeypatch)
    yaml = PROJECT_YAML.replace("AGENTS.md", member).replace("agents:", f"{file_id}:")
    host = Host(tmp_path, monkeypatch, extra_yaml=yaml)
    source = host.repo / "project" / "demo"
    source.mkdir(parents=True)
    (source / member).write_bytes(body)
    return host


def _profile_file(host: Host, member: str = "AGENTS.md") -> Path:
    return host.repo / "project" / "demo" / member


def _args(host: Host, target: Path) -> list[str]:
    return ["demo", str(target), f"--config={host.config}"]


def _run(host: Host, *argv: str) -> Result:
    return host.cli("project", *argv, config=False, profile=False)


def _commit(target: Path, name: str, body: bytes) -> None:
    (target / name).write_bytes(body)
    git(target, "add", name)
    git(target, "commit", "-q", "-m", f"add {name}")


def _tracked_overlay(host: Host, target: Path, draft: bytes = b"") -> None:
    _commit(target, "AGENTS.md", _TEAM)
    _profile_file(host).write_bytes(_PROFILE)
    (target / "AGENTS.md").write_bytes(_TEAM + draft)
    injected = _run(host, "inject", *_args(host, target), "--auto=use-profile", "--yes")
    assert injected.exit_code == 0, injected.output
    assert (target / "AGENTS.md").read_bytes() == _PROFILE


def test_hidden_injection_round_trip_leaves_git_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _host(tmp_path, monkeypatch)
    target = git_repo(tmp_path / "target")

    injected = _run(host, "inject", *_args(host, target), "--yes")

    assert injected.exit_code == 0, injected.output
    assert (target / "AGENTS.md").read_bytes() == b"managed\n"
    assert git(target, "status", "--short") == ""
    listed = _run(host, "list")
    assert str(target) in listed.output

    _profile_file(host).write_bytes(b"managed v2\n")
    synced = _run(host, "sync", str(target), "--yes")
    assert synced.exit_code == 0, synced.output
    assert (target / "AGENTS.md").read_bytes() == b"managed v2\n"

    removed = _run(host, "remove", *_args(host, target), "--yes")
    assert removed.exit_code == 0, removed.output
    assert not (target / "AGENTS.md").exists()
    assert git(target, "status", "--short") == ""
    assert _run(host, "list").output == "no project injections recorded\n"


@pytest.mark.parametrize("draft", [b"", b"local draft\n"], ids=["clean", "draft"])
def test_remove_restores_a_tracked_overlay_file_deleted_with_plain_rm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, draft: bytes
) -> None:
    host = _host(tmp_path, monkeypatch)
    target = git_repo(tmp_path / "target")
    _tracked_overlay(host, target, draft)
    (target / "AGENTS.md").unlink()

    removed = _run(host, "remove", *_args(host, target), "--yes")

    assert removed.exit_code == 0, removed.output
    assert (target / "AGENTS.md").read_bytes() == _TEAM + draft
    assert _run(host, "list").output == "no project injections recorded\n"
    assert git(target, "status", "--short") == (" M AGENTS.md\n" if draft else "")


def test_remove_leaves_a_file_absent_when_git_removed_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _host(tmp_path, monkeypatch)
    target = git_repo(tmp_path / "target")
    _tracked_overlay(host, target)
    git(target, "rm", "-q", "-f", "AGENTS.md")

    preview = _run(host, "remove", *_args(host, target), "--dry-run")
    assert preview.exit_code == 0, preview.output
    assert "leave absent: AGENTS.md" in preview.output
    assert "restore" not in preview.output

    removed = _run(host, "remove", *_args(host, target), "--yes")

    assert removed.exit_code == 0, removed.output
    assert not (target / "AGENTS.md").exists()
    assert git(target, "status", "--short") == "D  AGENTS.md\n"
    assert _run(host, "list").output == "no project injections recorded\n"


@pytest.mark.parametrize("tracked", [False, True], ids=["hidden", "tracked-overlay"])
def test_injection_works_in_a_repository_without_a_git_info_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tracked: bool
) -> None:
    host = _host(tmp_path, monkeypatch)
    target = git_repo(tmp_path / "target")
    if tracked:
        _commit(target, "AGENTS.md", _TEAM)
        _profile_file(host).write_bytes(_PROFILE)
    shutil.rmtree(target / ".git" / "info")
    extra = ["--auto=use-profile"] if tracked else []

    preview = _run(host, "inject", *_args(host, target), *extra, "--dry-run")
    assert preview.exit_code == 0, preview.output
    injected = _run(host, "inject", *_args(host, target), *extra, "--yes")
    assert injected.exit_code == 0, injected.output
    expected = _PROFILE if tracked else b"managed\n"
    assert (target / "AGENTS.md").read_bytes() == expected
    assert git(target, "diff", "--", "AGENTS.md") == ""

    removed = _run(host, "remove", *_args(host, target), "--yes")
    assert removed.exit_code == 0, removed.output
    assert (target / "AGENTS.md").exists() is tracked
    if tracked:
        assert (target / "AGENTS.md").read_bytes() == _TEAM
    assert git(target, "status", "--short") == ""


def test_a_file_declared_under_an_empty_id_is_still_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _host(tmp_path, monkeypatch, file_id='""')
    target = git_repo(tmp_path / "target")
    (target / "AGENTS.md").write_bytes(b"my own notes\n")

    injected = _run(host, "inject", *_args(host, target), "--yes")
    assert injected.exit_code == 0, injected.output
    assert (target / "AGENTS.md").read_bytes() == b"managed\n"

    for mode in ("--dry-run", "--yes"):
        removed = _run(host, "remove", *_args(host, target), mode)
        assert removed.exit_code == 0, removed.output

    assert (target / "AGENTS.md").read_bytes() == b"my own notes\n"
    assert _run(host, "list").output == "no project injections recorded\n"


_MEMBERS = {
    "yaml": (
        "settings.yaml",
        b"a:   1\nb:  [1,2]   # keep\nc: 'x'\n",
        b"a:   1\nb:  [1,2]   # keep\nc: 'x'\nhost: true\n",
        b"a:   2\nb:  [1,2]   # keep\nc: 'x'\n",
        b"a:   2\nb:  [1,2]   # keep\nc: 'x'\nhost: true\n",
    ),
    "json-with-comment": (
        "settings.json",
        b'{\n    "a": 1,\n    "b": [1,2],   // keep\n    "c": "x"\n}\n',
        b'{\n    "a": 1,\n    "b": [1,2],   // keep\n    "c": "x",\n'
        b'    "host": true\n}\n',
        b'{\n    "a": 1,\n    "b": [1,2],   // keep\n    "c": "y"\n}\n',
        b'{\n    "a": 1,\n    "b": [1,2],   // keep\n    "c": "y",\n'
        b'    "host": true\n}\n',
    ),
    "json-array-root": (
        "settings.json",
        b'[\n  {"n": 1},\n  {"n": 2}\n]\n',
        b'[\n  {"n": 1},\n  {"n": 2},\n  {"n": 9}\n]\n',
        b'[\n  {"n": 7},\n  {"n": 2}\n]\n',
        b'[\n  {"n": 7},\n  {"n": 2},\n  {"n": 9}\n]\n',
    ),
    "yml-crlf": (
        "settings.yml",
        b"a: 1\r\nb: 2\r\n# note\r\nc: 3\r\n",
        b"a: 1\r\nb: 2\r\n# note\r\nc: 3\r\nhost: 1\r\n",
        b"a: 5\r\nb: 2\r\n# note\r\nc: 3\r\n",
        b"a: 5\r\nb: 2\r\n# note\r\nc: 3\r\nhost: 1\r\n",
    ),
}


@pytest.mark.parametrize("name", sorted(_MEMBERS))
def test_sync_of_a_structured_member_keeps_untouched_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    member, base, local, profile, expected = _MEMBERS[name]
    host = _host(tmp_path, monkeypatch, base, member)
    target = git_repo(tmp_path / "target")
    assert _run(host, "inject", *_args(host, target), "--yes").exit_code == 0
    (target / member).write_bytes(local)
    _profile_file(host, member).write_bytes(profile)

    synced = _run(host, "sync", str(target), "--yes")

    assert synced.exit_code == 0, synced.output
    assert (target / member).read_bytes() == expected
    again = _run(host, "sync", str(target), "--yes")
    assert again.exit_code == 0, again.output
    assert (target / member).read_bytes() == expected


def test_sync_of_a_json_member_with_a_duplicate_key_is_a_conflict_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = b'{\n  "a": 1,\n  "b": 2\n}\n'
    local = b'{\n  "a": 1,\n  "a": 5,\n  "b": 2\n}\n'
    profile = b'{\n  "a": 1,\n  "b": 3\n}\n'
    host = _host(tmp_path, monkeypatch, base, "s.json")
    target = git_repo(tmp_path / "target")
    assert _run(host, "inject", *_args(host, target), "--yes").exit_code == 0
    (target / "s.json").write_bytes(local)
    _profile_file(host, "s.json").write_bytes(profile)

    refused = host.proc(
        "project", "sync", str(target), "--yes", config=False, profile=False
    )

    assert refused.returncode == 1
    assert "Traceback" not in refused.stderr
    assert "unresolved conflicts in s.json" in refused.stderr
    assert (target / "s.json").read_bytes() == local

    resolved = _run(host, "sync", str(target), "--auto=use-profile", "--yes")
    assert resolved.exit_code == 0, resolved.output
    assert (target / "s.json").read_bytes() == profile


def test_a_stale_record_whose_old_path_became_a_symlink_can_be_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _host(tmp_path, monkeypatch)
    target = git_repo(tmp_path / "target")
    moved = tmp_path / "moved"
    assert _run(host, "inject", *_args(host, target), "--yes").exit_code == 0
    target.rename(moved)
    target.symlink_to(moved, target_is_directory=True)

    listed = _run(host, "list")
    assert listed.exit_code == 1
    assert "project directory no longer exists" in listed.output
    assert f"setforge project remove demo {target}" in listed.output

    dropped = _run(host, "remove", *_args(host, target), "--yes")

    assert dropped.exit_code == 0, dropped.output
    assert dropped.output.endswith("stale injection dropped\n")
    assert (moved / "AGENTS.md").read_bytes() == b"managed\n"
    assert target.is_symlink()
    assert _run(host, "list").output == "no project injections recorded\n"


def test_remove_works_after_the_git_directory_was_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _host(tmp_path, monkeypatch)
    target = git_repo(tmp_path / "target")
    assert _run(host, "inject", *_args(host, target), "--yes").exit_code == 0
    shutil.rmtree(target / ".git")
    git(target, "init", "-q", "-b", "main")

    removed = _run(host, "remove", *_args(host, target), "--yes")

    assert removed.exit_code == 0, removed.output
    assert not (target / "AGENTS.md").exists()
    assert _run(host, "list").output == "no project injections recorded\n"
