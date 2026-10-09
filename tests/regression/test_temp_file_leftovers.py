"""A temp file an interrupted SetForge write left behind is never content.

Every temp file carries SetForge's marker in its name, and the walks that
decide what to deploy, capture, compare or offer as an orphan skip that name
and nothing else: a user's own ``foo.tmp`` or ``.foo.abc12345.tmp`` is handled
exactly as any other file."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from setforge import atomicio
from setforge.locking import mutation_locks
from tests.regression.support import REPO_ROOT, Host, tree
from tests.regression.test_ownership import _tree_host

# A user's files whose names merely resemble a temp file.
LOOKALIKES = ("foo.tmp", ".foo.abc12345.tmp", ".foo.setforge-notes.tmp")

# The child dies at the rename of the named destination, temp file written.
KILL_AT_RENAME = """
import os
from setforge.cli import main

real = os.replace

def replace(src, dst, **kwargs):
    if os.path.basename(os.fspath(dst)) == os.environ["SETFORGE_KILL_AT"]:
        os._exit(79)
    return real(src, dst, **kwargs)

os.replace = replace
main()
"""


def leftover_names(name: str) -> tuple[str, str]:
    """The temp names of ``name`` made inside and outside the mutation gate."""
    with mutation_locks():
        gated = atomicio.temp_name(name)
    return gated, atomicio.temp_name(name)


def kill_at_rename(host: Host, leaf: str, *argv: str, config: bool = True) -> None:
    killed = subprocess.run(
        [sys.executable, "-c", KILL_AT_RENAME, *host._argv(argv, config, True)],
        cwd=REPO_ROOT,
        env={**host.proc_env(), "SETFORGE_KILL_AT": leaf},
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert killed.returncode == 79, (killed.stdout, killed.stderr)


def directory_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Host:
    """A host whose one tracked entry is the directory ``d``."""
    host = Host(
        tmp_path,
        monkeypatch,
        tracked={},
        extra_yaml="  d: {src: d, dst: '~/.x/d'}",
        profile_yaml="      - d",
    )
    host.tracked("d").mkdir()
    host.tracked("d/a.txt").write_bytes(b"one\n")
    return host


def test_temp_names_carry_the_marker_and_lookalikes_do_not() -> None:
    gated, unlocked = leftover_names("settings.json")

    assert gated.startswith(".settings.json.setforge-")
    assert unlocked.startswith(".settings.json.setforge-u")
    assert not gated.startswith(".settings.json.setforge-u")
    assert len(gated) == len("settings.json") + 31
    assert len(unlocked) == len("settings.json") + 32
    assert gated.endswith(".tmp")
    assert unlocked.endswith(".tmp")
    assert atomicio.is_temp_name(gated)
    assert atomicio.is_temp_name(unlocked)
    for name in (*LOOKALIKES, ".a.setforge-create", "a.setforge-" + "0" * 16 + ".tmp"):
        assert not atomicio.is_temp_name(name), name


def test_a_killed_write_leaves_only_a_marked_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = Host(tmp_path, monkeypatch)

    kill_at_rename(host, "note.txt", "install", "--yes", "--no-fetch", "--no-git-check")

    assert not host.live("note.txt").exists()
    (leftover,) = host.live_dir.iterdir()
    assert atomicio.is_temp_name(leftover.name)
    assert leftover.name.startswith(".note.txt.setforge-")
    assert leftover.read_bytes() == b"one\n"


def test_install_and_compare_ignore_a_leftover_in_a_tracked_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = directory_host(tmp_path, monkeypatch)
    leftovers = leftover_names("a.txt")
    for name in leftovers:
        host.tracked(f"d/{name}").write_bytes(b"half\n")
    for name in LOOKALIKES:
        host.tracked(f"d/{name}").write_bytes(b"mine\n")

    installed = host.install()

    assert installed.exit_code == 0, installed.output
    assert sorted(path.name for path in host.live("d").iterdir()) == sorted(
        ("a.txt", *LOOKALIKES)
    )
    compared = host.cli("compare", "--check")
    assert compared.exit_code == 0, compared.output
    for name in leftovers:
        assert name not in compared.output.replace("\n", "")
        assert host.tracked(f"d/{name}").read_bytes() == b"half\n"


def test_sync_captures_lookalikes_and_never_a_leftover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = directory_host(tmp_path, monkeypatch)
    for name in LOOKALIKES:
        host.tracked(f"d/{name}").write_bytes(b"mine\n")
    assert host.install().exit_code == 0
    leftovers = leftover_names("a.txt")
    for name in leftovers:
        host.tracked(f"d/{name}").write_bytes(b"half\n")
        host.live(f"d/{name}").write_bytes(b"live half\n")
    for name in LOOKALIKES:
        host.live(f"d/{name}").write_bytes(b"edited\n")

    synced = host.cli("sync", "--auto=use-live", "--yes")

    assert synced.exit_code == 0, synced.output
    for name in LOOKALIKES:
        assert host.tracked(f"d/{name}").read_bytes() == b"edited\n"
    for name in leftovers:
        assert host.tracked(f"d/{name}").read_bytes() == b"half\n"
        assert name not in synced.output.replace("\n", "")


def test_a_managed_tree_deploys_lookalikes_and_never_a_leftover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _tree_host(tmp_path, monkeypatch, {"managed": "~/.managed"})
    source = host.tracked("managed")
    leftovers = leftover_names("kept.txt")
    for name in leftovers:
        (source / name).write_bytes(b"half\n")
    for name in LOOKALIKES:
        (source / name).write_bytes(b"mine\n")

    installed = host.install()

    assert installed.exit_code == 0, installed.output
    live = host.real_home / ".managed"
    assert sorted(path.name for path in live.iterdir()) == sorted(
        ("kept.txt", *LOOKALIKES)
    )
    # A leftover inside the live tree is not drift either.
    (live / leftovers[0]).write_bytes(b"half\n")
    compared = host.cli("compare", "--check")
    assert compared.exit_code == 0, compared.output


def test_a_marked_directory_or_symlink_in_a_managed_tree_is_still_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _tree_host(tmp_path, monkeypatch, {"managed": "~/.managed"})
    host.config.write_text(
        host.config.read_text(encoding="utf-8").replace(
            "tree: {}", "tree: {symlinks: preserve}"
        ),
        encoding="utf-8",
    )
    source = host.tracked("managed")
    directory, _ = leftover_names("dir")
    (source / directory).mkdir()
    (source / directory / "inner.txt").write_bytes(b"mine\n")
    links = leftover_names("kept.txt")
    for link in links:
        (source / link).symlink_to("kept.txt")

    installed = host.install()

    assert installed.exit_code == 0, installed.output
    live = host.real_home / ".managed"
    assert (live / directory / "inner.txt").read_bytes() == b"mine\n"
    for link in links:
        assert (live / link).is_symlink()
        assert str((live / link).readlink()) == "kept.txt"


def test_orphan_scan_offers_a_marked_symlink_and_a_file_in_a_marked_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _tree_host(tmp_path, monkeypatch, {"managed": "~/.managed"})
    assert host.install().exit_code == 0
    live = host.real_home / ".managed"
    directory, _ = leftover_names("dir")
    (live / directory).mkdir()
    (live / directory / "inner.txt").write_bytes(b"mine\n")
    links = leftover_names("kept.txt")
    for link in links:
        (live / link).symlink_to("kept.txt")

    scan = host.cli("cleanup-orphans", "--scan")

    assert scan.exit_code == 0, scan.output
    flat = scan.output.replace("\n", "")
    assert str(live / directory / "inner.txt") in flat
    for link in links:
        assert str(live / link) in flat


def test_a_killed_symlink_deploy_leaves_a_marked_link_that_is_never_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = Host(
        tmp_path,
        monkeypatch,
        tracked={"body.txt": "body\n"},
        dsts={"body.txt": "~/.x/link"},
        extra_yaml="  d: {src: d, dst: '~/.y/d'}",
        profile_yaml="      - d",
    )
    host.config.write_text(
        host.config.read_text(encoding="utf-8").replace(
            "dst: '~/.x/link'}", "dst: '~/.x/link', symlink: real.txt}"
        ),
        encoding="utf-8",
    )
    host.tracked("d").mkdir()
    host.tracked("d/real.txt").write_bytes(b"one\n")
    host.live_dir.mkdir()

    kill_at_rename(host, "link", "install", "--yes", "--no-fetch", "--no-git-check")

    assert host.live("real.txt").read_bytes() == b"body\n"
    (leftover,) = (
        path for path in host.live_dir.iterdir() if atomicio.is_temp_name(path.name)
    )
    assert sorted(path.name for path in host.live_dir.iterdir()) == sorted(
        ("real.txt", leftover.name)
    )
    assert leftover.is_symlink()
    assert leftover.name.startswith(".link.setforge-")
    assert not leftover.name.startswith(".link.setforge-u")
    recovered = host.proc("recover", "--apply", "--yes", config=False)
    assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)
    # The same link inside a tracked directory is neither deployed nor captured.
    (host.tracked("d") / leftover.name).symlink_to("real.txt")

    installed = host.install()

    assert installed.exit_code == 0, installed.output
    assert str(host.live("link").readlink()) == "real.txt"
    live_d = host.home / ".y" / "d"
    assert [path.name for path in live_d.iterdir()] == ["real.txt"]
    (live_d / leftover.name).symlink_to("real.txt")
    compared = host.cli("compare", "--check")
    assert compared.exit_code == 0, compared.output
    assert leftover.name not in compared.output.replace("\n", "")


def test_orphan_scan_offers_lookalikes_and_never_a_leftover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _tree_host(tmp_path, monkeypatch, {"managed": "~/.managed"})
    assert host.install().exit_code == 0
    live = host.real_home / ".managed"
    leftovers = leftover_names("kept.txt")
    for name in (*leftovers, *LOOKALIKES):
        (live / name).write_bytes(b"x\n")

    scan = host.cli("cleanup-orphans", "--scan")

    assert scan.exit_code == 0, scan.output
    flat = scan.output.replace("\n", "")
    for name in LOOKALIKES:
        assert str(live / name) in flat
    for name in leftovers:
        assert name not in flat
        assert (live / name).read_bytes() == b"x\n"


@pytest.mark.parametrize("gate", ["gated", "unlocked"])
def test_a_leftover_keeps_the_managed_directory_it_sits_in_when_that_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate: str
) -> None:
    host = _tree_host(tmp_path, monkeypatch, {"managed": "~/.managed"})
    host.config.write_text(
        host.config.read_text(encoding="utf-8").replace(
            "tree: {}", "tree: {orphans: remove-owned}"
        ),
        encoding="utf-8",
    )
    source = host.tracked("managed")
    (source / "sub" / "deep").mkdir(parents=True)
    (source / "sub" / "a.txt").write_bytes(b"one\n")
    (source / "sub" / "deep" / "b.txt").write_bytes(b"two\n")
    (source / "gone").mkdir()
    (source / "gone" / "c.txt").write_bytes(b"three\n")
    assert host.install().exit_code == 0
    live = host.real_home / ".managed"
    name = leftover_names("b.txt")[gate == "unlocked"]
    leftover = live / "sub" / "deep" / name
    leftover.write_bytes(b"half\n")
    shutil.rmtree(source / "sub")
    shutil.rmtree(source / "gone")

    dry = host.proc_install("--dry-run")

    assert dry.returncode == 0, (dry.stdout, dry.stderr)
    assert (live / "sub" / "a.txt").exists()
    assert (live / "gone" / "c.txt").exists()
    assert "WOULD update" in dry.stdout

    installed = host.proc_install()

    assert installed.returncode == 0, (installed.stdout, installed.stderr)
    # The directories on the way to the leftover stay; everything else goes.
    assert tree(live) == {
        "kept.txt": b"kept\n",
        "sub": "<dir>",
        "sub/deep": "<dir>",
        f"sub/deep/{name}": b"half\n",
    }
    assert "removal" not in installed.stderr
    compared = host.cli("compare", "--check")
    assert compared.exit_code == 0, compared.output
    again = host.proc_install()
    assert again.returncode == 0, (again.stdout, again.stderr)
    assert tree(live)[f"sub/deep/{name}"] == b"half\n"
    assert (live / "sub" / "deep").is_dir()
    settled = host.proc_install("--dry-run")
    assert settled.returncode == 0, (settled.stdout, settled.stderr)
    assert "WOULD update" not in settled.stdout
