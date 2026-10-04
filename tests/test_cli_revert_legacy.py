"""A transition recorded by an earlier version as a text patch is refused cleanly.

Such a record (the shape setforge 1.3.9 writes for an install that changed a
file's content and another file's mode) holds no pre-image of those files, so
``revert`` refuses it before changing anything, while ``transitions list`` and
``transitions show`` still list it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge import operations, transitions
from setforge.cli import app
from setforge.errors import RevertFailed

_CONFIG = """\
version: 1
tracked_files:
  note:
    src: note.txt
    dst: {dst}
profiles:
  p:
    tracked_files: [note]
"""


def _legacy_record(state: Path, name: str, *, note: Path, keep: Path) -> Path:
    record = state / "transitions" / name
    record.mkdir(parents=True)
    note_rel = str(note).lstrip("/")
    (record / "changes.patch").write_bytes(
        f"--- {note_rel}\n+++ {note_rel}\n@@ -1,2 +1,3 @@\n one\n-two\n"
        "\\ No newline at end of file\n+TWO\n+three\n"
        "\\ No newline at end of file\n".encode()
    )
    (record / "file_modes.json").write_text(json.dumps({str(keep): 0o644}))
    (record / "meta.json").write_text(
        json.dumps(
            {
                "command": "install",
                "profile": "p",
                "timestamp": f"{name[:4]}-{name[4:6]}-{name[6:8]}T00:00:00+00:00",
                "host": "h",
                "version": "1.3.9",
                "paths": [str(note)],
            }
        )
    )
    return record


@pytest.fixture
def legacy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    repo = tmp_path / "repo"
    (repo / "tracked").mkdir(parents=True)
    note = tmp_path / "live" / "note.txt"
    keep = tmp_path / "live" / "keep.txt"
    note.parent.mkdir()
    note.write_bytes(b"one\nTWO\nthree")
    keep.write_bytes(b"keep\n")
    keep.chmod(0o600)
    (repo / "tracked" / "note.txt").write_bytes(b"one\nTWO\nthree")
    config = repo / "setforge.yaml"
    config.write_text(_CONFIG.format(dst=note), encoding="utf-8")
    record = _legacy_record(
        state, "20261001T000000000000Z-install-p", note=note, keep=keep
    )
    return {"config": config, "note": note, "keep": keep, "record": record}


def _live(paths: dict[str, Path]) -> tuple[bytes, bytes, int]:
    return (
        paths["note"].read_bytes(),
        paths["keep"].read_bytes(),
        paths["keep"].stat().st_mode & 0o7777,
    )


@pytest.mark.parametrize("to_before", [False, True])
def test_revert_refuses_a_patch_format_record_before_changing_anything(
    legacy: dict[str, Path], to_before: bool
) -> None:
    before = _live(legacy)
    records = sorted(legacy["record"].parent.iterdir())
    argv = ["revert", "--profile=p", f"--config={legacy['config']}", "--yes"]
    if to_before:
        argv.append(f"--to-before={legacy['record'].name}")

    result = CliRunner().invoke(app, argv)

    assert result.exit_code == 1
    assert isinstance(result.exception, RevertFailed)
    message = str(result.exception)
    assert (
        f"transition {legacy['record'].name} was recorded by an earlier version in "
        "a format this version cannot revert; nothing was changed" in message
    )
    assert "setforge 1.3.9" in message
    assert str(legacy["note"]) in message
    assert str(legacy["keep"]) in message
    assert _live(legacy) == before
    assert sorted(legacy["record"].parent.iterdir()) == records
    assert operations.active("p") is None


def test_a_chain_reaching_a_patch_format_record_is_refused_whole(
    legacy: dict[str, Path],
) -> None:
    newer = legacy["record"].parent / "20261002T000000000000Z-sync-p"
    newer.mkdir()
    transitions.write_meta(
        transitions.TransitionDir(newer),
        transitions.TransitionMeta(
            command=transitions.TransitionCommand.SYNC,
            profile="p",
            timestamp=transitions.now_utc(),
            host="h",
            version="0",
        ),
    )
    before = _live(legacy)

    result = CliRunner().invoke(
        app,
        [
            "revert",
            "--profile=p",
            f"--config={legacy['config']}",
            "--yes",
            f"--to-before={legacy['record'].name}",
        ],
    )

    assert result.exit_code == 1
    assert isinstance(result.exception, RevertFailed)
    assert "cannot revert; nothing was changed" in str(result.exception)
    assert _live(legacy) == before
    assert not any("-revert-" in path.name for path in newer.parent.iterdir())


def test_transitions_list_and_show_still_list_a_patch_format_record(
    legacy: dict[str, Path],
) -> None:
    runner = CliRunner()

    listed = runner.invoke(app, ["transitions", "list"])
    shown = runner.invoke(app, ["transitions", "show", legacy["record"].name])

    assert listed.exit_code == 0, listed.output
    assert legacy["record"].name in listed.output
    assert shown.exit_code == 0, shown.output
    assert "version: 1.3.9" in shown.output
    assert str(legacy["note"]) in shown.output
    assert "--to-before" not in shown.output
    assert "cannot revert" in shown.output
    assert "setforge 1.3.9" in shown.output


def _record(tmp_path: Path, paths: list[str], files: dict[str, str]) -> Path:
    record = tmp_path / "20261001T000000000000Z-install-p"
    record.mkdir()
    (record / "meta.json").write_text(
        json.dumps(
            {
                "command": "install",
                "profile": "p",
                "timestamp": "2026-10-01T00:00:00+00:00",
                "host": "h",
                "version": "1.3.9",
                "paths": paths,
            }
        )
    )
    for name, text in files.items():
        (record / name).write_text(text)
    return record


@pytest.mark.parametrize(
    "files",
    [
        {},
        {"changes.patch": ""},
        {"changes.patch": "--- a\n+++ a\n@@ -1 +1 @@\n-x\n+y\n"},
        {"file_modes.json": "{}"},
    ],
)
def test_a_record_listing_files_it_holds_no_image_of_is_refused(
    tmp_path: Path, files: dict[str, str]
) -> None:
    record = _record(tmp_path, ["/live/a"], files)

    with pytest.raises(RevertFailed, match="restore by hand: /live/a"):
        transitions.refuse_legacy_file_changes(
            transitions.load_record(transitions.TransitionDir(record))
        )


def test_a_record_without_file_changes_is_not_refused(tmp_path: Path) -> None:
    record = _record(tmp_path, [], {"changes.patch": ""})

    transitions.refuse_legacy_file_changes(
        transitions.load_record(transitions.TransitionDir(record))
    )
