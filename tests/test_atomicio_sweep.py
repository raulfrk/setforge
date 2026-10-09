"""A gated atomic write removes the temp files a killed gate holder left.

Only the gated names of the destination being written, only regular files,
only in the destination's own directory, and only while the mutation gate is
held."""

import errno
import os
import threading
from pathlib import Path

import pytest

from setforge import atomicio
from setforge.errors import SetforgeError
from setforge.locking import mutation_locks

HEX = "a" * 16


def gated(name: str) -> str:
    return f".{name}.setforge-{HEX}.tmp"


def test_gated_write_removes_and_reports_the_destinations_leftovers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "settings.json"
    target.write_bytes(b"old\n")
    stale = tmp_path / gated("settings.json")
    stale_backup = tmp_path / gated("settings.json.bak")
    stale.write_bytes(b"half\n")
    stale_backup.write_bytes(b"half\n")

    with mutation_locks():
        atomicio.atomic_write_bytes(target, b"new\n", backup=True)

    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "settings.json",
        "settings.json.bak",
    ]
    assert target.read_bytes() == b"new\n"
    captured = capsys.readouterr()
    assert captured.out == ""
    lines = captured.err.splitlines()
    assert len(lines) == 2
    for line, path in zip(lines, (stale_backup, stale), strict=True):
        assert line == (
            "warning: removed a temporary file left by an interrupted "
            f"setforge run: {path}"
        )


@pytest.mark.parametrize(
    "name",
    [
        ".settings.json.setforge-u" + HEX + ".tmp",  # may be a live writer
        gated("other.json"),  # another destination's
        ".settings.json.abc12345.tmp",  # an earlier release's, or the user's
        "settings.json.tmp",
        ".settings.json.setforge-" + HEX,
        "x" + gated("settings.json"),
    ],
)
def test_gated_write_keeps_every_other_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    target = tmp_path / "settings.json"
    (tmp_path / name).write_bytes(b"kept\n")

    with mutation_locks():
        atomicio.atomic_write_bytes(target, b"new\n")

    assert (tmp_path / name).read_bytes() == b"kept\n"
    assert capsys.readouterr().err == ""


def test_gated_write_never_removes_a_symlink_or_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "settings.json"
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"mine\n")
    link = tmp_path / gated("settings.json")
    link.symlink_to(outside)
    directory = tmp_path / gated("settings.json.bak")
    directory.mkdir()
    (directory / "inner.txt").write_bytes(b"mine\n")

    with mutation_locks():
        atomicio.atomic_write_bytes(target, b"new\n")

    assert link.is_symlink()
    assert outside.read_bytes() == b"mine\n"
    assert (directory / "inner.txt").read_bytes() == b"mine\n"
    assert capsys.readouterr().err == ""


def test_a_leftover_that_cannot_be_removed_is_skipped_and_the_write_completes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "settings.json"
    # Sorted first, so the sweep meets the one it cannot remove first.
    stale = tmp_path / gated("settings.json.bak")
    stale.write_bytes(b"half\n")
    removable = tmp_path / gated("settings.json")
    removable.write_bytes(b"half\n")
    real_unlink = os.unlink

    def unlink(name: str, *, dir_fd: int | None = None) -> None:
        if name == stale.name:
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), name)
        real_unlink(name, dir_fd=dir_fd)

    monkeypatch.setattr(atomicio.os, "unlink", unlink)

    with mutation_locks():
        atomicio.atomic_write_bytes(target, b"new\n")

    assert target.read_bytes() == b"new\n"
    assert stale.read_bytes() == b"half\n"
    # The failure is not reported as a removal; the next leftover still goes.
    assert capsys.readouterr().err == (
        "warning: removed a temporary file left by an interrupted "
        f"setforge run: {removable}\n"
    )
    assert not removable.exists()


def test_a_directory_that_cannot_be_listed_is_not_swept_and_the_write_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "settings.json"
    stale = tmp_path / gated("settings.json")
    stale.write_bytes(b"half\n")

    def listdir(path: object) -> list[str]:
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))

    monkeypatch.setattr(atomicio.os, "listdir", listdir)

    with mutation_locks():
        atomicio.atomic_write_bytes(target, b"new\n")

    assert target.read_bytes() == b"new\n"
    assert stale.read_bytes() == b"half\n"


def test_sweep_stays_in_the_destinations_own_directory(tmp_path: Path) -> None:
    target = tmp_path / "dir" / "settings.json"
    above = tmp_path / gated("settings.json")
    below = tmp_path / "dir" / "sub" / gated("settings.json")
    below.parent.mkdir(parents=True)
    above.write_bytes(b"kept\n")
    below.write_bytes(b"kept\n")

    with mutation_locks():
        atomicio.atomic_write_bytes(target, b"new\n")

    assert above.read_bytes() == b"kept\n"
    assert below.read_bytes() == b"kept\n"


def test_write_outside_the_gate_removes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "settings.json"
    stale = tmp_path / gated("settings.json")
    stale.write_bytes(b"half\n")

    atomicio.atomic_write_bytes(target, b"new\n")

    assert stale.read_bytes() == b"half\n"
    assert capsys.readouterr().err == ""


def test_gated_sweep_spares_a_writer_running_outside_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gate holder writes the same file while an ungated write is mid-flight."""
    target = tmp_path / "rc"
    real_replace = os.replace
    seen: list[bool] = []

    def replace(src: Path, dst: Path) -> None:
        if not seen:
            seen.append(True)
            with mutation_locks():
                atomicio.atomic_write_bytes(target, b"gated\n")
            seen.append(Path(src).exists())
        real_replace(src, dst)

    monkeypatch.setattr(atomicio.os, "replace", replace)

    atomicio.atomic_write_bytes(target, b"ungated\n")

    assert seen == [True, True]
    assert target.read_bytes() == b"ungated\n"
    assert [path.name for path in tmp_path.iterdir()] == ["rc"]


def test_no_second_gate_holder_can_sweep_while_a_gated_write_is_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The name a sweep removes is only made under the gate, which is exclusive."""
    target = tmp_path / "settings.json"
    real_replace = os.replace
    outcome: list[object] = []

    def other_command() -> None:
        try:
            with mutation_locks(timeout=0.2):
                atomicio.atomic_write_bytes(target, b"other\n")
        except SetforgeError as exc:
            outcome.append(str(exc))

    def replace(src: Path, dst: Path) -> None:
        assert atomicio.is_temp_name(Path(src).name)
        assert Path(src).name.startswith(".settings.json.setforge-")
        assert not Path(src).name.startswith(".settings.json.setforge-u")
        thread = threading.Thread(target=other_command)
        thread.start()
        thread.join()
        outcome.append(Path(src).exists())
        real_replace(src, dst)

    monkeypatch.setattr(atomicio.os, "replace", replace)

    with mutation_locks():
        atomicio.atomic_write_bytes(target, b"ours\n")

    assert outcome == [
        "another setforge command holds the global mutation gate; retry shortly",
        True,
    ]
    assert target.read_bytes() == b"ours\n"


def test_stale_staging_links_are_removed_only_under_the_mutation_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "target.txt"
    target.write_bytes(b"mine\n")
    stale = tmp_path / gated("link")
    stale.symlink_to(target)

    atomicio.sweep_stale_temp_links(tmp_path / "link")

    assert stale.is_symlink()
    assert capsys.readouterr().err == ""

    with mutation_locks():
        atomicio.sweep_stale_temp_links(tmp_path / "link")

    assert [path.name for path in tmp_path.iterdir()] == ["target.txt"]
    assert target.read_bytes() == b"mine\n"
    assert capsys.readouterr().err.replace("\n", "") == (
        "warning: removed a temporary file left by an interrupted "
        f"setforge run: {stale}"
    )
