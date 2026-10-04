"""Interrupted operations recover to the exact pre-state.

A SIGKILL is injected through a scriptable stand-in for the ``code`` binary,
so the real ``setforge`` process dies at a point where files are already
written. Everything is observed through the CLI and the resulting files."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from .support import (
    INSTALL_FLAGS,
    REPO_ROOT,
    Host,
    extension_host,
    tree,
)

pytestmark = pytest.mark.integration


def _live_tree(host: Host) -> dict[str, bytes | str]:
    return {k: v for k, v in tree(host.real_home).items() if k.startswith(".x")}


def _said(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def test_killed_install_recovers_to_the_exact_pre_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = extension_host(tmp_path, monkeypatch)
    before = _live_tree(host)
    expected = b"one\n"
    host.arm("install")

    killed = host.proc_install()

    assert killed.returncode != 0
    assert host.live("note.txt").read_bytes() == expected
    assert "unfinished install" in host.proc("status").stdout

    preview = host.proc("recover", config=False)
    assert preview.returncode == 0, preview.stderr
    assert "setforge recover --profile=p --apply" in preview.stdout
    assert host.live("note.txt").read_bytes() == expected

    recovered = host.proc("recover", "--apply", "--yes", config=False)
    assert recovered.returncode == 0, recovered.stderr
    assert _live_tree(host) == before
    assert "no unfinished operation" in _said(host.proc("recover", config=False))
    assert "unfinished" not in host.proc("status").stdout

    host.arm("")
    retried = host.proc_install()
    assert retried.returncode == 0, retried.stderr
    assert host.live("note.txt").read_bytes() == expected
    assert host.installed_extensions() == ["pub.name"]


def test_unfinished_operation_blocks_mutating_commands_until_recovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = extension_host(tmp_path, monkeypatch)
    host.arm("install")
    assert host.proc_install().returncode != 0
    stuck = _live_tree(host)
    tracked = host.tracked("note.txt").read_bytes()

    for argv in (
        ["install", *INSTALL_FLAGS],
        ["sync", "--auto=use-live", "--yes"],
        ["capture", "--auto=use-live", "--yes"],
        ["snapshot", "create", "blocked"],
    ):
        result = host.proc(*argv)
        assert result.returncode == 1, argv
        assert "unfinished install operation" in result.stderr, argv
        assert "setforge recover --profile=p" in result.stderr, argv
        assert _live_tree(host) == stuck
        assert host.tracked("note.txt").read_bytes() == tracked

    for argv in (["compare"], ["status"], ["stage", "--list"], ["validate"]):
        assert host.proc(*argv).returncode == 0, argv


def test_killed_revert_recovers_to_the_installed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = extension_host(tmp_path, monkeypatch)
    assert host.proc_install().returncode == 0
    installed = _live_tree(host)
    host.arm("uninstall")

    killed = host.proc("revert", "--yes")

    assert killed.returncode != 0
    assert not host.live("note.txt").exists()
    assert "unfinished revert" in host.proc("status").stdout
    blocked = host.proc("revert", "--yes")
    assert blocked.returncode == 1
    assert "unfinished revert operation" in blocked.stderr

    host.arm("")
    recovered = host.proc("recover", "--apply", "--yes", config=False)
    assert recovered.returncode == 0, recovered.stderr
    assert _live_tree(host) == installed
    assert host.installed_extensions() == ["pub.name"]
    assert "no unfinished operation" in _said(host.proc("recover", config=False))


def test_failed_install_rolls_back_without_leaving_an_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = Host(tmp_path, monkeypatch, tracked={"a.txt": "a\n", "z/b.txt": "b\n"})
    locked = host.live_dir / "z"
    locked.mkdir(parents=True)
    locked.chmod(0o500)
    before = _live_tree(host)
    try:
        failed = host.install()
        assert failed.exit_code == 1, failed.output
        locked.chmod(0o700)
        assert _live_tree(host) == before
        assert "no unfinished operation" in _said(host.proc("recover", config=False))
        assert "unfinished" not in host.proc("status").stdout
        assert host.install().exit_code == 0
        assert host.live("a.txt").read_bytes() == b"a\n"
        assert host.live("z/b.txt").read_bytes() == b"b\n"
    finally:
        locked.chmod(0o700)


def test_recovery_waits_for_a_running_operation_instead_of_undoing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = extension_host(tmp_path, monkeypatch)
    host.arm("hold")
    running = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "setforge.cli",
            "install",
            *INSTALL_FLAGS,
            f"--config={host.config}",
            "--profile=p",
        ],
        cwd=REPO_ROOT,
        env=host.proc_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 30
        while not host.live("note.txt").exists():
            assert time.monotonic() < deadline, "install never wrote its file"
            time.sleep(0.1)
        competitor = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "setforge.cli",
                "recover",
                "--profile=p",
                "--apply",
                "--yes",
            ],
            cwd=REPO_ROOT,
            env=host.proc_env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(4)
        assert competitor.poll() is None, "recovery ran beside a live operation"
        assert host.live("note.txt").read_bytes() == b"one\n"
        host.release()
        install_out, install_err = running.communicate(timeout=60)
        competitor.communicate(timeout=60)
    finally:
        running.kill()
        running.wait()

    assert running.returncode == 0, install_err + install_out
    assert host.live("note.txt").read_bytes() == b"one\n"
    assert host.installed_extensions() == ["pub.name"]
    assert "unfinished" not in host.proc("status").stdout


def test_journal_stays_loadable_when_a_config_ancestor_becomes_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = extension_host(tmp_path, monkeypatch, repo_parent="cfg")
    host.arm("install")
    assert host.proc_install().returncode != 0
    (tmp_path / "cfg").rename(tmp_path / "cfg-real")
    (tmp_path / "cfg").symlink_to("cfg-real", target_is_directory=True)

    status = host.proc("status")
    assert status.returncode == 0, status.stderr
    assert "unfinished install" in status.stdout
    assert "unreadable" not in status.stdout
    preview = host.proc("recover", config=False)
    assert preview.returncode == 0, preview.stderr
    recovered = host.proc("recover", "--apply", "--yes", config=False)
    assert recovered.returncode == 0, recovered.stderr
    assert not host.live("note.txt").exists()

    host.arm("")
    assert host.proc_install().returncode == 0
    assert host.live("note.txt").read_bytes() == b"one\n"


def test_a_corrupt_journal_gives_one_clear_line_and_no_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = extension_host(tmp_path, monkeypatch)
    host.arm("install")
    assert host.proc_install().returncode != 0
    (journal,) = (host.real_home / ".cache/setforge/operations").glob("*.json")
    journal.write_text("{ not json", encoding="utf-8")
    host.arm("")

    for argv in (["recover"], ["install", *INSTALL_FLAGS]):
        result = host.proc(*argv, config=argv[0] != "recover")
        assert result.returncode == 1, argv
        assert "Traceback" not in result.stderr
        errors = [ln for ln in result.stderr.splitlines() if ln.startswith("error:")]
        assert len(errors) == 1
        assert "corrupt operation journal" in errors[0]

    status = host.proc("status")
    assert status.returncode == 0
    assert "Traceback" not in status.stderr
    assert "unreadable journal" in status.stdout


@pytest.mark.parametrize(
    "argv", [["revert", "--yes"], ["sync", "--auto=use-live", "--yes"]]
)
def test_a_refused_command_leaves_nothing_to_recover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    host = Host(tmp_path, monkeypatch, tracked={"a.json": '{"a": 1}\n'})
    if argv[0] == "sync":
        assert host.install().exit_code == 0
        host.live("a.json").write_bytes(b'{"a": ')

    result = host.proc(*argv)

    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert "no unfinished operation" in _said(host.proc("recover", config=False))
    assert "unfinished" not in host.proc("status").stdout
