"""Revert removes the directories a command created, and only those."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge import operations, transitions
from setforge.cli import app
from tests.shared_helpers import write_setforge_yaml
from tests.test_cli_revert import _no_code, _state_root

_ONE_FILE = """\
version: 1
tracked_files:
  note:
    src: note.txt
    dst: {dst}
profiles:
  p:
    tracked_files: [note]
"""

_TWO_FILES = """\
version: 1
tracked_files:
  note:
    src: note.txt
    dst: {dst}
  other:
    src: other.txt
    dst: {other}
profiles:
  p:
    tracked_files: [note, other]
"""

_SYMLINK = """\
version: 1
tracked_files:
  hook:
    src: hook.sh
    dst: {link}
    symlink: real/hook.sh
profiles:
  p:
    tracked_files: [hook]
"""


_FILE_AND_TREE = """\
schema_version: '6.2'
minimum_version: '6.2'
version: 1
tracked_files:
  note:
    src: note.txt
    dst: {dst}
  bundle:
    src: bundle
    dst: {tree}
    tree: {{}}
profiles:
  p:
    tracked_files: [note, bundle]
"""


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _state_root(tmp_path, monkeypatch)
    _no_code(monkeypatch)
    repo = tmp_path / "repo"
    (repo / "tracked").mkdir(parents=True)
    (repo / "tracked" / "note.txt").write_text("note\n", encoding="utf-8")
    (repo / "tracked" / "other.txt").write_text("other\n", encoding="utf-8")
    (repo / "tracked" / "hook.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    return repo


def _run(*argv: str) -> Result:
    return CliRunner().invoke(app, list(argv))


def _install(config: Path) -> None:
    result = CliRunner().invoke(
        app, ["install", "--profile=p", f"--config={config}", "--yes"]
    )
    assert result.exit_code == 0, result.output


def _revert(config: Path, *extra: str) -> Result:
    return CliRunner().invoke(
        app, ["revert", "--profile=p", f"--config={config}", "--yes", *extra]
    )


def test_revert_removes_directories_the_install_created_and_redo_recreates_them(
    repo: Path, tmp_path: Path
) -> None:
    live = tmp_path / "live"
    note = live / "newdir" / "sub" / "note.txt"
    config = write_setforge_yaml(repo, _ONE_FILE.format(dst=note))
    _install(config)
    mode = (live / "newdir" / "sub").stat().st_mode & 0o7777
    assert note.read_text() == "note\n"

    reverted = _revert(config)
    assert reverted.exit_code == 0, reverted.output
    assert not live.exists()

    redone = _revert(config)
    assert redone.exit_code == 0, redone.output
    assert note.read_text() == "note\n"
    assert (live / "newdir" / "sub").stat().st_mode & 0o7777 == mode


def test_revert_removes_the_directories_of_a_symlink_target(
    repo: Path, tmp_path: Path
) -> None:
    link = tmp_path / "sl" / "hook.sh"
    config = write_setforge_yaml(repo, _SYMLINK.format(link=link))
    _install(config)
    assert link.is_symlink()
    assert (tmp_path / "sl" / "real").is_dir()

    reverted = _revert(config)

    assert reverted.exit_code == 0, reverted.output
    assert not (tmp_path / "sl").exists()


def test_revert_keeps_a_directory_that_existed_before_even_when_empty(
    repo: Path, tmp_path: Path
) -> None:
    kept = tmp_path / "live" / "kept"
    kept.mkdir(parents=True)
    note = kept / "new" / "note.txt"
    config = write_setforge_yaml(repo, _ONE_FILE.format(dst=note))
    _install(config)

    reverted = _revert(config)

    assert reverted.exit_code == 0, reverted.output
    assert kept.is_dir()
    assert list(kept.iterdir()) == []


def test_revert_keeps_a_created_directory_that_gained_other_entries(
    repo: Path, tmp_path: Path
) -> None:
    created = tmp_path / "live" / "newdir"
    note = created / "note.txt"
    config = write_setforge_yaml(repo, _ONE_FILE.format(dst=note))
    _install(config)
    mine = created / "mine" / "file.txt"
    mine.parent.mkdir()
    mine.write_text("mine\n", encoding="utf-8")

    reverted = _revert(config)

    assert reverted.exit_code == 0, reverted.output
    assert not note.exists()
    assert mine.read_text() == "mine\n"
    assert sorted(path.name for path in created.iterdir()) == ["mine"]
    assert operations.active("p") is None

    redone = _revert(config)
    assert redone.exit_code == 0, redone.output
    assert note.read_text() == "note\n"
    assert mine.read_text() == "mine\n"


def test_revert_to_before_removes_directories_created_across_the_chain(
    repo: Path, tmp_path: Path
) -> None:
    live = tmp_path / "live"
    note = live / "newdir" / "note.txt"
    other = live / "newdir" / "sub" / "other.txt"
    _install(write_setforge_yaml(repo, _ONE_FILE.format(dst=note)))
    first = sorted(transitions.transitions_root().iterdir())[-1].name
    config = write_setforge_yaml(repo, _TWO_FILES.format(dst=note, other=other))
    _install(config)
    assert other.read_text() == "other\n"

    reverted = _revert(config, f"--to-before={first}")

    assert reverted.exit_code == 0, reverted.output
    assert not live.exists()


def test_revert_of_a_sync_removes_the_directories_it_created(
    repo: Path, tmp_path: Path
) -> None:
    note = tmp_path / "live" / "note.txt"
    config = write_setforge_yaml(repo, _ONE_FILE.format(dst=note))
    _install(config)
    config.write_text(
        _ONE_FILE.replace("src: note.txt", "src: deep/new/note.txt").format(dst=note),
        encoding="utf-8",
    )
    note.write_text("edited live\n", encoding="utf-8")

    synced = _run(
        "sync", "--profile=p", f"--config={config}", "--auto=use-live", "--yes"
    )
    assert synced.exit_code == 0, synced.output
    assert (repo / "tracked" / "deep" / "new" / "note.txt").exists()

    reverted = _revert(config)

    assert reverted.exit_code == 0, reverted.output
    assert not (repo / "tracked" / "deep").exists()


@pytest.mark.parametrize("via_symlink", [False, True])
def test_revert_removes_a_directory_shared_by_a_file_and_a_tree(
    repo: Path, tmp_path: Path, via_symlink: bool
) -> None:
    (repo / "tracked" / "bundle").mkdir()
    (repo / "tracked" / "bundle" / "item.txt").write_text("item\n", encoding="utf-8")
    real = tmp_path / "real"
    real.mkdir()
    base = real
    if via_symlink:
        base = tmp_path / "link"
        base.symlink_to(real)
    shared = base / "newtool"
    config = write_setforge_yaml(
        repo,
        _FILE_AND_TREE.format(dst=shared / "note.txt", tree=shared / "bundle"),
    )
    _install(config)

    reverted = _revert(config)

    assert reverted.exit_code == 0, reverted.output
    assert not (real / "newtool").exists()
