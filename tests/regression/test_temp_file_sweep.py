"""The command that next writes a file removes what a killed write left beside it."""

from __future__ import annotations

from pathlib import Path

import pytest

from setforge import atomicio
from tests.regression.support import Host
from tests.regression.test_temp_file_leftovers import (
    LOOKALIKES,
    directory_host,
    kill_at_rename,
    leftover_names,
)

INSTALL = ("install", "--yes", "--no-fetch", "--no-git-check", "--no-secrets-scan")
REMOVED = "warning: removed a temporary file left by an interrupted setforge run: "


def recover(host: Host) -> str:
    recovered = host.proc("recover", "--apply", "--yes", config=False)
    assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)
    return recovered.stderr.replace("\n", "")


def test_install_removes_and_reports_what_a_killed_install_left_beside_a_live_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = Host(tmp_path, monkeypatch)
    for name in LOOKALIKES:
        host.live(name).parent.mkdir(exist_ok=True)
        host.live(name).write_bytes(b"mine\n")
    kill_at_rename(host, "note.txt", *INSTALL)
    (leftover,) = (
        path for path in host.live_dir.iterdir() if atomicio.is_temp_name(path.name)
    )
    recover(host)
    assert leftover.exists()

    dry = host.proc_install("--dry-run")

    assert dry.returncode == 0, (dry.stdout, dry.stderr)
    assert leftover.exists()
    assert REMOVED not in dry.stderr

    installed = host.proc_install()

    assert installed.returncode == 0, (installed.stdout, installed.stderr)
    assert f"{REMOVED}{leftover}" in installed.stderr.replace("\n", "")
    assert sorted(path.name for path in host.live_dir.iterdir()) == sorted(
        ("note.txt", *LOOKALIKES)
    )
    for name in LOOKALIKES:
        assert host.live(name).read_bytes() == b"mine\n"


def test_recovering_a_killed_sync_removes_what_it_left_in_the_tracked_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = directory_host(tmp_path, monkeypatch)
    assert host.install().exit_code == 0
    host.live("d/a.txt").write_bytes(b"edited\n")
    sync = ("sync", "--auto=use-live", "--yes")
    kill_at_rename(host, "a.txt", *sync)
    (leftover,) = (
        path for path in host.tracked("d").iterdir() if atomicio.is_temp_name(path.name)
    )
    assert leftover.read_bytes() == b"edited\n"

    reported = recover(host)

    assert f"{REMOVED}{leftover}" in reported
    assert [path.name for path in host.tracked("d").iterdir()] == ["a.txt"]
    assert host.tracked("d/a.txt").read_bytes() == b"one\n"
    installed = host.proc_install("--auto=keep-live")
    assert installed.returncode == 0, (installed.stdout, installed.stderr)
    assert [path.name for path in host.live("d").iterdir()] == ["a.txt"]


def test_locked_commands_keep_a_marked_symlink_directory_and_ungated_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = Host(tmp_path, monkeypatch)
    host.live_dir.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"mine\n")
    gated, unlocked = leftover_names("note.txt")
    gated_backup, _ = leftover_names("note.txt.bak")
    host.live(gated).symlink_to(outside)
    host.live(gated_backup).mkdir()
    host.live(unlocked).write_bytes(b"live writer\n")

    installed = host.proc_install()

    assert installed.returncode == 0, (installed.stdout, installed.stderr)
    assert REMOVED not in installed.stderr
    assert host.live("note.txt").read_bytes() == b"one\n"
    assert host.live(gated).is_symlink()
    assert outside.read_bytes() == b"mine\n"
    assert host.live(gated_backup).is_dir()
    assert host.live(unlocked).read_bytes() == b"live writer\n"


def symlink_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Host:
    """A host whose one tracked file is deployed as ``link`` -> ``real.txt``."""
    host = Host(
        tmp_path,
        monkeypatch,
        tracked={"body.txt": "body\n"},
        dsts={"body.txt": "~/.x/link"},
    )
    host.config.write_text(
        host.config.read_text(encoding="utf-8").replace(
            "dst: '~/.x/link'}", "dst: '~/.x/link', symlink: real.txt}"
        ),
        encoding="utf-8",
    )
    host.live_dir.mkdir()
    return host


def test_install_removes_the_staging_link_a_killed_symlink_deploy_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = symlink_host(tmp_path, monkeypatch)
    kill_at_rename(host, "link", *INSTALL)
    (leftover,) = (
        path for path in host.live_dir.iterdir() if atomicio.is_temp_name(path.name)
    )
    assert leftover.is_symlink()
    recover(host)
    assert leftover.is_symlink()
    # Whatever is not a gated staging link of this very destination stays.
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"mine\n")
    gated, unlocked = leftover_names("link")
    other, _ = leftover_names("other")
    backup, _ = leftover_names("link.bak")
    regular, directory = gated[:-5] + "0.tmp", gated[:-5] + "1.tmp"
    kept_links = (unlocked, other, backup, ".link.abc12345.tmp")
    for name in kept_links:
        host.live(name).symlink_to(outside)
    host.live(regular).write_bytes(b"mine\n")
    host.live(directory).mkdir()
    # A link with the full gated name is removed; what it points at is not.
    host.live(gated).symlink_to(outside)

    dry = host.proc_install("--dry-run")

    assert dry.returncode == 0, (dry.stdout, dry.stderr)
    assert REMOVED not in dry.stderr
    assert leftover.is_symlink()
    assert host.live(gated).is_symlink()

    installed = host.proc_install()

    assert installed.returncode == 0, (installed.stdout, installed.stderr)
    reported = installed.stderr.replace("\n", "")
    assert f"{REMOVED}{leftover}" in reported
    assert f"{REMOVED}{host.live(gated)}" in reported
    assert reported.count(REMOVED) == 2
    assert str(host.live("link").readlink()) == "real.txt"
    assert host.live("real.txt").read_bytes() == b"body\n"
    assert outside.read_bytes() == b"mine\n"
    assert sorted(path.name for path in host.live_dir.iterdir()) == sorted(
        ("link", "real.txt", regular, directory, *kept_links)
    )
    for name in kept_links:
        assert host.live(name).is_symlink()
    assert host.live(regular).read_bytes() == b"mine\n"
    assert host.live(directory).is_dir()
