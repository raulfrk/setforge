"""A recorded file change reverses and redoes byte-exactly through its images.

Each case records a change the way every producer does (``capture_files``
before and after, then ``write_transition``), reverses it the way ``revert``
does (check every entry, then restore through guarded directory descriptors)
and redoes it from the reversal's own record.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from setforge import operations, orphan_scan, transitions
from setforge.errors import RevertFailed

_CASES: dict[str, tuple[bytes | None, bytes | None]] = {
    "crlf": (b"a\r\nb\r\n", b"a\r\nb\r\nc\r\n"),
    "crlf-changed-line": (b"a\r\nb\r\n", b"a\r\nB\r\n"),
    "lone-cr": (b"a\rb\r", b"a\rB\r"),
    "mixed": (b"a\r\nb\nc\r", b"a\r\nb\nc\r\nd"),
    "form-feed": (b"one\nsec\x0cond\nthree\n", b"one\nsec\x0cond\nthree\nfour\n"),
    "separators": (
        "x\u2028y\x85z\x1cw\nq\n".encode(),
        "x\u2028y\x85z\x1cw\nq\nr\n".encode(),
    ),
    "nul": (b"a\x00b\nc\n", b"a\x00b\nc\nd\n"),
    "bom": (b"\xef\xbb\xbfkey: 1\n", b"\xef\xbb\xbfkey: 2\n"),
    "non-utf8": (b"caf\xe9\n\xff\xfe\n", b"caf\xe9\n\xff\xfe\nmore\n"),
    "no-final-newline": (b"a\nb", b"a\nb\nc"),
    "newline-only-after": (b"a\nb\n", b"a\nb"),
    "header-like-lines": (b"-- a\n++ b\nx\n", b"-- a\n++ b\nx\ny\n"),
    "empty-to-content": (b"", b"content\n"),
    "content-to-empty": (b"content\n", b""),
    "created": (None, b"fresh\n"),
    "created-empty": (None, b""),
    "deleted": (b"old\n", None),
}


def _write(path: Path, data: bytes | None, mode: int = 0o644) -> None:
    if data is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)


def _record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    paths: list[Path],
    change: dict[Path, tuple[bytes | None, int]],
) -> transitions.TransitionRecord:
    """Record the change of ``paths`` to ``change`` like a producer does."""
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    pre = transitions.capture_files(paths)
    for path, (data, mode) in change.items():
        _write(path, data, mode)
    post = transitions.capture_files(paths)
    target = transitions.write_transition(
        transitions.make_meta(transitions.TransitionCommand.INSTALL, "p"),
        pre,
        post,
        None,
    )
    return transitions.load_record(target)


def _revert(record: transitions.TransitionRecord, *roots: Path) -> None:
    deltas = record.filesystem_deltas
    transitions.validate_filesystem_deltas_reverse(deltas)
    operations.apply_filesystem_deltas_reverse_anchored(
        deltas,
        orphan_scan.capture_parent_path_guards(
            (*(item.path for item in deltas), *roots)
        ),
    )


def _state(path: Path) -> tuple[bytes | None, int | None]:
    if not path.exists():
        return None, None
    return path.read_bytes(), path.stat().st_mode & 0o7777


@pytest.mark.parametrize("name", sorted(_CASES))
def test_file_change_reverts_and_redoes_byte_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    before, after = _CASES[name]
    live = tmp_path / "live" / "file.txt"
    _write(live, before, 0o600)
    expected_before = _state(live)

    record = _record(monkeypatch, tmp_path, [live], {live: (after, 0o640)})
    expected_after = _state(live)
    assert record.files_complete
    assert [item.path for item in record.filesystem_deltas] == [live]

    _revert(record)
    assert _state(live) == expected_before
    assert sorted(path.name for path in live.parent.iterdir()) == (
        [] if before is None else ["file.txt"]
    )

    redo = transitions.reverse_filesystem_deltas(record.filesystem_deltas)
    operations.apply_filesystem_deltas_reverse_anchored(
        redo, orphan_scan.capture_parent_path_guards((live,))
    )
    assert _state(live) == expected_after


def test_mode_only_change_reverts_the_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "secret.txt"
    _write(live, b"same\n", 0o600)

    record = _record(monkeypatch, tmp_path, [live], {live: (b"same\n", 0o644)})
    (delta,) = record.filesystem_deltas
    assert (delta.pre.mode, delta.post.mode) == (0o600, 0o644)
    assert record.paths == ()

    _revert(record)
    assert _state(live) == (b"same\n", 0o600)


def test_change_through_a_symlinked_destination_restores_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "elsewhere" / "target.txt"
    _write(target, b"one\n")
    live = tmp_path / "live.txt"
    live.symlink_to(target)

    record = _record(monkeypatch, tmp_path, [live], {target: (b"one\ntwo\n", 0o644)})
    assert [item.path for item in record.filesystem_deltas] == [target]
    assert record.paths == (live,)

    _revert(record)
    assert live.is_symlink()
    assert target.read_bytes() == b"one\n"


@pytest.mark.parametrize("link_name", ["link", "with space"])
@pytest.mark.parametrize("before", [b"one\n", None])
def test_change_through_a_symlinked_directory_is_reverted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    link_name: str,
    before: bytes | None,
) -> None:
    real = tmp_path / "real dir"
    real.mkdir()
    (tmp_path / link_name).symlink_to(real)
    live = tmp_path / link_name / "sub" / "f.txt"
    _write(live, before)

    record = _record(monkeypatch, tmp_path, [live], {live: (b"one\ntwo\n", 0o644)})
    assert [item.path for item in record.filesystem_deltas] == [real / "sub" / "f.txt"]

    _revert(record)
    assert _state(real / "sub" / "f.txt")[0] == before


def test_a_path_with_spaces_and_quotes_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / 'dir with "quotes"' / "my file\twith tab.txt"
    _write(live, b"before\n")

    record = _record(monkeypatch, tmp_path, [live], {live: (b"after\n", 0o644)})
    _revert(record)
    assert live.read_bytes() == b"before\n"


def test_a_changed_file_refuses_and_nothing_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clean = tmp_path / "a.txt"
    edited = tmp_path / "b.txt"
    _write(clean, b"before-a\n")
    _write(edited, b"before-b\n")
    record = _record(
        monkeypatch,
        tmp_path,
        [clean, edited],
        {clean: (b"after-a\n", 0o644), edited: (b"after-b\n", 0o644)},
    )
    edited.write_bytes(b"after-b\nmine\n")

    with pytest.raises(RevertFailed, match=f"changed since transition: {edited}"):
        transitions.validate_filesystem_deltas_reverse(record.filesystem_deltas)

    assert clean.read_bytes() == b"after-a\n"
    assert edited.read_bytes() == b"after-b\nmine\n"


@pytest.mark.parametrize("drift", ["mode", "kind", "absent"])
def test_mode_kind_and_presence_drift_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    live = tmp_path / "f.txt"
    _write(live, b"before\n")
    record = _record(monkeypatch, tmp_path, [live], {live: (b"after\n", 0o644)})
    if drift == "mode":
        live.chmod(0o600)
    elif drift == "kind":
        live.unlink()
        live.symlink_to("elsewhere")
    else:
        live.unlink()

    with pytest.raises(RevertFailed, match="changed since transition"):
        transitions.validate_filesystem_deltas_reverse(record.filesystem_deltas)


def test_a_touched_file_still_reverts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "f.txt"
    _write(live, b"before\n")
    record = _record(monkeypatch, tmp_path, [live], {live: (b"after\n", 0o644)})
    os.utime(live, ns=(1, 1))

    _revert(record)
    assert live.read_bytes() == b"before\n"


def test_no_change_records_no_file_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "f.txt"
    _write(live, b"same\n")
    pre = transitions.capture_files([live])
    os.utime(live, ns=(1, 1))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))

    target = transitions.write_transition(
        transitions.make_meta(transitions.TransitionCommand.INSTALL, "p"),
        pre,
        transitions.capture_files([live]),
        None,
    )

    assert not (target / "filesystem_deltas.json").exists()
    assert transitions.load_record(target).paths == ()
