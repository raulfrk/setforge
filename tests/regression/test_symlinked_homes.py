"""HOME, or an ancestor of it, is a symlink (NFS and automounted homes).

Every verb must behave exactly as under a plain home. A directory swapped for
a symlink after the operation was planned must still be refused."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from .support import HOME_KINDS, Host, claim_ids_for

pytestmark = pytest.mark.integration


def _host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, **kw: object
) -> Host:
    return Host(tmp_path, monkeypatch, kind=kind, **kw)  # type: ignore[arg-type]


def test_cross_device_home_is_on_another_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _host(tmp_path, monkeypatch, "cross-device")
    assert host.home.is_symlink()
    assert host.real_home.stat().st_dev != host.repo.stat().st_dev
    assert host.real_home.stat().st_dev != host.state.parent.stat().st_dev


@pytest.mark.parametrize("kind", HOME_KINDS)
@pytest.mark.parametrize(
    "state_in_home", [True, False], ids=["state-in-home", "state-apart"]
)
def test_install_sync_capture_revert_match_a_plain_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, state_in_home: bool
) -> None:
    host = _host(tmp_path, monkeypatch, kind, state_in_home=state_in_home)
    live = host.live("note.txt")

    first = host.install()
    assert first.exit_code == 0, first.output
    assert live.read_bytes() == b"one\n"
    assert host.cli("compare", "--check").exit_code == 0

    host.tracked("note.txt").write_bytes(b"two\n")
    updated = host.install()
    assert updated.exit_code == 0, updated.output
    assert live.read_bytes() == b"two\n"

    reverted = host.cli("revert", "--yes")
    assert reverted.exit_code == 0, reverted.output
    assert live.read_bytes() == b"one\n"

    live.write_bytes(b"three\n")
    synced = host.cli("sync", "--auto=use-live", "--yes")
    assert synced.exit_code == 0, synced.output
    assert host.tracked("note.txt").read_bytes() == b"three\n"

    live.write_bytes(b"four\n")
    captured = host.cli("capture", "--auto=use-live", "--yes")
    assert captured.exit_code == 0, captured.output
    assert host.tracked("note.txt").read_bytes() == b"four\n"
    assert sorted(p.name for p in host.live_dir.iterdir()) == [
        "note.txt",
        "note.txt.bak",
    ]


@pytest.mark.parametrize(
    "kind", ["symlink", "trailing-slash", "ancestor", "cross-device"]
)
def test_snapshot_create_and_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    host = _host(tmp_path, monkeypatch, kind, state_in_home=True)
    assert host.install().exit_code == 0
    created = host.cli("snapshot", "create", "before", profile=True)
    assert created.exit_code == 0, created.output
    assert "before" in host.cli("snapshot", "list", config=False, profile=False).output

    host.live("note.txt").write_bytes(b"changed\n")
    restored = host.cli("snapshot", "restore", "before", "--yes")
    assert restored.exit_code == 0, restored.output
    assert host.live("note.txt").read_bytes() == b"one\n"


@pytest.mark.parametrize("kind", ["symlink", "relative", "ancestor", "cross-device"])
def test_orphan_cleanup_removes_only_the_dropped_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    host = _host(
        tmp_path,
        monkeypatch,
        kind,
        state_in_home=True,
        tracked={"keep.txt": "keep\n", "drop.txt": "drop\n"},
    )
    assert host.install().exit_code == 0
    host.config.write_text(
        host.config.read_text().replace("      - drop_txt\n", ""), encoding="utf-8"
    )

    preview = host.cli("cleanup-orphans")
    assert preview.exit_code == 0, preview.output
    assert "drop.txt" in preview.output
    assert host.live("drop.txt").exists()

    listing = host.cli("ownership", "list", config=False, profile=False).output
    (claim,) = claim_ids_for(listing, "/.x/drop.txt")
    released = host.cli("ownership", "release", claim, "--yes", profile=False)
    assert released.exit_code == 0, released.output

    applied = host.cli("cleanup-orphans", "--apply", "--yes")
    assert applied.exit_code == 0, applied.output
    assert not host.live("drop.txt").exists()
    assert host.live("keep.txt").read_bytes() == b"keep\n"


_PROJECT = (
    "project_profiles:\n"
    "  demo:\n"
    "    files:\n"
    "      agents:\n"
    "        src: AGENTS.md\n"
    "        dst: AGENTS.md\n"
)


@pytest.mark.parametrize(
    "kind", ["symlink", "trailing-slash", "ancestor", "cross-device"]
)
def test_project_inject_and_remove_inside_a_symlinked_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    host = _host(tmp_path, monkeypatch, kind, state_in_home=True, extra_yaml=_PROJECT)
    source = host.repo / "project" / "demo"
    source.mkdir(parents=True)
    (source / "AGENTS.md").write_bytes(b"managed\n")
    project = host.home / "work" / "proj"
    project.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(project)], check=True)
    args = ["demo", str(project), f"--config={host.config}", "--yes"]

    injected = host.cli("project", "inject", *args, config=False, profile=False)
    assert injected.exit_code == 0, injected.output
    assert (project / "AGENTS.md").read_bytes() == b"managed\n"
    assert (
        "no project injections"
        not in host.cli("project", "list", config=False, profile=False).output
    )

    removed = host.cli("project", "remove", *args, config=False, profile=False)
    assert removed.exit_code == 0, removed.output
    assert not (project / "AGENTS.md").exists()
    assert (
        host.cli("project", "list", config=False, profile=False).output
        == "no project injections recorded\n"
    )


_EXTENSION = "packages:\n  pe: {type: extension, extension: pub.name}\n"
_KILL_ON_EXTENSION = (
    'case "$1" in --install-extension) kill -9 $PPID; sleep 5;; esac; exit 0'
)


@pytest.mark.parametrize("kind", ["symlink", "ancestor", "cross-device"])
def test_recover_after_a_killed_install_restores_the_pre_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    host = _host(
        tmp_path,
        monkeypatch,
        kind,
        state_in_home=True,
        extra_yaml=_EXTENSION,
        profile_yaml="    packages: [pe]",
    )
    host.shim("code", _KILL_ON_EXTENSION)

    killed = host.proc_install()
    assert killed.returncode != 0
    assert host.live("note.txt").read_bytes() == b"one\n"
    assert "unfinished" in host.proc("status").stdout
    refused = host.proc_install()
    assert refused.returncode != 0
    assert "unfinished install" in refused.stderr + refused.stdout

    recovered = host.proc("recover", "--apply", "--yes", config=False)
    assert recovered.returncode == 0, recovered.stderr
    assert not host.live("note.txt").exists()
    again = host.proc("recover", config=False)
    assert "no unfinished operation" in again.stderr + again.stdout


@pytest.mark.parametrize("kind", ["plain", "symlink", "cross-device"])
def test_directory_swapped_for_a_symlink_after_planning_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    host = _host(tmp_path, monkeypatch, kind)
    host.live_dir.mkdir()
    host.live("note.txt").write_bytes(b"old\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    live_dir = host.live_dir
    host.shim(
        "gitleaks",
        f"mv '{live_dir}' '{live_dir}.real' && ln -s '{outside}' '{live_dir}'",
    )

    result = host.proc("install", "--yes", "--no-fetch", "--no-git-check")

    assert result.returncode != 0
    assert "changed after planning" in result.stderr + result.stdout
    assert list(outside.iterdir()) == []
    assert (Path(f"{host.live_dir}.real") / "note.txt").read_bytes() == b"old\n"
