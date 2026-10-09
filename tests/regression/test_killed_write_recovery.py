"""An install killed inside a directory it created can still be recovered."""

from __future__ import annotations

from pathlib import Path

import pytest

from setforge import atomicio
from tests.regression.support import Host
from tests.regression.test_temp_file_leftovers import directory_host, kill_at_rename

INSTALL = ("install", "--yes", "--no-fetch", "--no-git-check", "--no-secrets-scan")
REMOVED = "warning: removed a temporary file left by an interrupted setforge run: "
REFUSED = "refusing to remove non-empty recovery directory "


def killed_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Host, Path]:
    """A host whose install died writing into the directory it had just made."""
    host = directory_host(tmp_path, monkeypatch)
    kill_at_rename(host, "a.txt", *INSTALL)
    (leftover,) = host.live("d").iterdir()
    assert atomicio.is_gated_temp_name(leftover.name)
    return host, leftover


def test_recover_removes_the_created_directory_with_what_the_killed_write_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host, leftover = killed_install(tmp_path, monkeypatch)

    shown = host.proc("recover", config=False)

    assert shown.returncode == 0, (shown.stdout, shown.stderr)
    assert REMOVED not in shown.stderr
    assert leftover.read_bytes() == b"one\n"

    recovered = host.proc("recover", "--apply", "--yes", config=False)

    assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)
    assert f"{REMOVED}{leftover}" in recovered.stderr.replace("\n", "")
    assert not host.live_dir.exists()
    installed = host.proc_install()
    assert installed.returncode == 0, (installed.stdout, installed.stderr)
    assert [path.name for path in host.live("d").iterdir()] == ["a.txt"]


@pytest.mark.parametrize(
    "blocker",
    ["unlocked-name", "user-file", "marked-directory", "marked-symlink", "symlink"],
)
def test_recover_refuses_and_names_anything_else_in_the_created_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocker: str
) -> None:
    host, leftover = killed_install(tmp_path, monkeypatch)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"mine\n")
    gated = leftover.name.replace(".a.txt.", ".b.txt.")
    unlocked = gated.replace(".setforge-", ".setforge-u")
    assert atomicio.is_gated_temp_name(gated)
    assert atomicio.is_temp_name(unlocked)
    assert not atomicio.is_gated_temp_name(unlocked)
    name = {
        "unlocked-name": unlocked,
        "user-file": "notes.txt",
        "marked-directory": gated,
        "marked-symlink": gated,
        "symlink": "link",
    }[blocker]
    entry = host.live(f"d/{name}")
    if blocker == "marked-directory":
        entry.mkdir()
    elif blocker.endswith("symlink"):
        entry.symlink_to(outside)
    else:
        entry.write_bytes(b"mine\n")

    recovered = host.proc("recover", "--apply", "--yes", config=False)

    assert recovered.returncode != 0, (recovered.stdout, recovered.stderr)
    error = recovered.stderr.replace("\n", "")
    assert f"{REFUSED}{host.live('d')}: it still holds {name}" in error
    assert leftover.name not in error
    assert REMOVED not in error
    assert leftover.read_bytes() == b"one\n"
    assert outside.read_bytes() == b"mine\n"
    if blocker == "marked-directory":
        assert entry.is_dir()
    elif blocker.endswith("symlink"):
        assert entry.is_symlink()
    else:
        assert entry.read_bytes() == b"mine\n"
    blocked = host.proc_install()
    assert blocked.returncode != 0, (blocked.stdout, blocked.stderr)

    # Once the user moves the named entry away, recovery finishes.
    entry.rename(tmp_path / "moved-aside")
    retried = host.proc("recover", "--apply", "--yes", config=False)

    assert retried.returncode == 0, (retried.stdout, retried.stderr)
    assert not host.live_dir.exists()
    assert outside.read_bytes() == b"mine\n"
