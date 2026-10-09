"""A live entry the managed-tree scan passes over still occupies its directory.

With ``orphans: remove-owned``, a directory dropped from the tracked source is
removed only when nothing of the user's is left in it: a file or directory the
tree's ``exclude`` patterns cover, or SetForge's own state, keeps the directory
and is never deleted. An entry SetForge cannot manage at all is refused by
name, in a dry run as in a real one."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from tests.regression.support import Host, tree
from tests.regression.test_ownership import _tree_host

_EXCLUDES = "exclude: ['*.local', 'cache/']"


def _host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: str = "remove-owned"
) -> tuple[Host, Path, Path]:
    """An installed tree whose source holds ``sub/deep`` and ``gone``."""
    host = _tree_host(tmp_path, monkeypatch, {"managed": "~/.managed"})
    host.config.write_text(
        host.config.read_text(encoding="utf-8").replace(
            "tree: {}", f"tree: {{orphans: {policy}, {_EXCLUDES}}}"
        ),
        encoding="utf-8",
    )
    source = host.tracked("managed")
    (source / "sub" / "deep").mkdir(parents=True)
    (source / "sub" / "a.txt").write_bytes(b"one\n")
    (source / "sub" / "deep" / "b.txt").write_bytes(b"two\n")
    (source / "gone").mkdir()
    (source / "gone" / "c.txt").write_bytes(b"three\n")
    return host, source, host.real_home / ".managed"


def _drop_from_source(source: Path) -> None:
    shutil.rmtree(source / "sub")
    shutil.rmtree(source / "gone")


@pytest.mark.parametrize(
    "skipped", ["excluded-file", "excluded-directory", "state-root", "user-symlink"]
)
def test_a_skipped_live_entry_keeps_the_removed_directory_it_sits_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skipped: str
) -> None:
    host, source, live = _host(tmp_path, monkeypatch)
    if skipped == "state-root":
        state = live / "sub" / "deep" / ".sfstate"
        monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
        host.state = state
    assert host.install().exit_code == 0
    mine: dict[str, bytes | str]
    if skipped == "excluded-file":
        mine = {"sub/deep/mine.local": b"mine\n"}
    elif skipped == "excluded-directory":
        mine = {"sub/deep/cache": "<dir>", "sub/deep/cache/data.bin": b"mine\n"}
        (live / "sub" / "deep" / "cache").mkdir()
    elif skipped == "user-symlink":
        # Not skipped but unowned: the same rule already kept its directory.
        mine = {"sub/deep/link": "-> elsewhere"}
        (live / "sub" / "deep" / "link").symlink_to("elsewhere")
    else:
        mine = {}
    for rel, body in mine.items():
        if isinstance(body, bytes):
            (live / rel).write_bytes(body)
    _drop_from_source(source)

    before = tree(live)

    dry = host.proc_install("--dry-run")

    assert dry.returncode == 0, (dry.stdout, dry.stderr)
    # The dry run itemises nothing below the tree and writes nothing in it.
    assert tree(live) == before
    assert "WOULD update" in dry.stdout

    installed = host.proc_install()

    assert installed.returncode == 0, (installed.stdout, installed.stderr)
    assert "removal" not in installed.stderr
    # The directories on the way to the skipped entry stay; everything else goes.
    kept = {"kept.txt": b"kept\n", "sub": "<dir>", "sub/deep": "<dir>"}
    if skipped == "state-root":
        assert (live / "sub" / "deep" / ".sfstate").is_dir()
        assert not (live / "sub" / "a.txt").exists()
        assert not (live / "sub" / "deep" / "b.txt").exists()
        assert not (live / "gone").exists()
    else:
        assert tree(live) == kept | mine
    compared = host.proc("compare", "--check")
    assert compared.returncode == 0, (compared.stdout, compared.stderr)
    again = host.proc_install()
    assert again.returncode == 0, (again.stdout, again.stderr)
    settled = host.proc_install("--dry-run")
    assert settled.returncode == 0, (settled.stdout, settled.stderr)
    assert "WOULD update" not in settled.stdout
    assert (live / "sub" / "deep").is_dir()
    if skipped != "state-root":
        assert tree(live) == kept | mine


def test_an_excluded_entry_is_never_deleted_and_its_removal_frees_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host, source, live = _host(tmp_path, monkeypatch)
    assert host.install().exit_code == 0
    # Excluded names beside owned ones, in a removed directory and a kept one.
    (live / "gone" / "mine.local").write_bytes(b"mine\n")
    (live / "top.local").write_bytes(b"top\n")
    shutil.rmtree(source / "gone")

    assert host.proc_install().returncode == 0

    assert tree(live) == {
        "kept.txt": b"kept\n",
        "top.local": b"top\n",
        "gone": "<dir>",
        "gone/mine.local": b"mine\n",
        "sub": "<dir>",
        "sub/a.txt": b"one\n",
        "sub/deep": "<dir>",
        "sub/deep/b.txt": b"two\n",
    }

    (live / "gone" / "mine.local").unlink()
    freed = host.proc_install()

    assert freed.returncode == 0, (freed.stdout, freed.stderr)
    assert not (live / "gone").exists()
    assert (live / "top.local").read_bytes() == b"top\n"
    assert host.proc("compare", "--check").returncode == 0


def test_orphans_keep_leaves_the_directory_and_the_excluded_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host, source, live = _host(tmp_path, monkeypatch, policy="keep")
    assert host.install().exit_code == 0
    (live / "sub" / "deep" / "mine.local").write_bytes(b"mine\n")
    before = tree(live)
    _drop_from_source(source)

    assert host.proc_install("--dry-run").returncode == 0
    installed = host.proc_install()

    assert installed.returncode == 0, (installed.stdout, installed.stderr)
    assert tree(live) == before


@pytest.mark.parametrize("blocker", ["fifo", "unreadable-directory"])
def test_an_entry_setforge_cannot_manage_is_refused_by_name_in_a_dry_run_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocker: str
) -> None:
    if blocker == "unreadable-directory" and os.geteuid() == 0:
        pytest.skip("root reads a mode 000 directory")
    host, source, live = _host(tmp_path, monkeypatch)
    assert host.install().exit_code == 0
    entry = live / "sub" / "deep" / "blocker"
    if blocker == "fifo":
        os.mkfifo(entry)
    else:
        entry.mkdir()
        entry.chmod(0)
    _drop_from_source(source)
    try:
        dry = host.proc_install("--dry-run")
        installed = host.proc_install()
    finally:
        if blocker != "fifo":
            entry.chmod(0o700)

    for run in (dry, installed):
        assert run.returncode == 1, (run.stdout, run.stderr)
        assert str(entry) in run.stdout + run.stderr
    # Nothing was removed on the way to the refusal.
    assert (live / "sub" / "a.txt").read_bytes() == b"one\n"
    assert (live / "gone" / "c.txt").read_bytes() == b"three\n"
