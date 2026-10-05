"""A tree root that contains SetForge's own state installs on the first run."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.regression.support import _FAKE_CODE, Host, claim_ids_for
from tests.regression.test_ownership import _command_from, _tree_host


def test_first_install_of_a_root_holding_the_state_dir_needs_no_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _tree_host(tmp_path, monkeypatch, {"managed": "~/.managed"})
    state = host.real_home / ".managed" / ".sfstate"
    state.parent.mkdir()
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    host.state = state

    first = host.install()

    assert first.exit_code == 0, first.output


def test_ownership_revert_of_a_tree_holding_setforge_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _tree_host(tmp_path, monkeypatch, {"s": "~/.local/share"})
    snapshots = host.real_home / ".local" / "share" / "setforge" / "snapshots"
    snapshots.mkdir(parents=True)
    (snapshots / "x.txt").write_bytes(b"x")
    assert host.install().exit_code == 0
    listing = host.cli("ownership", "list", config=False, profile=False).output
    (claim,) = claim_ids_for(listing, "/.local/share")
    released = host.cli("ownership", "release", claim, "--yes", profile=False)
    assert released.exit_code == 0, released.output
    blocked = host.proc_install()
    assert blocked.returncode == 1

    back = host.cli(
        *_command_from(blocked.stderr, "revert"), config=False, profile=False
    )

    assert back.exit_code == 0, back.output


def _error_line(result: object) -> str:
    lines = [
        line
        for line in getattr(result, "stderr", "").splitlines()
        if line.startswith("error")
    ]
    return lines[-1] if lines else ""


def test_later_installs_of_a_root_holding_the_state_dir_keep_working(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _tree_host(tmp_path, monkeypatch, {"managed": "~/.managed"})
    root = host.real_home / ".managed"
    state = root / ".sfstate"
    root.mkdir()
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    host.state = state
    source = host.tracked_root / "managed" / "kept.txt"

    for index in range(4):
        if index:
            source.write_bytes(f"kept{index}\n".encode())
        result = host.proc_install()
        assert result.returncode == 0, (index, _error_line(result))

    assert (root / "kept.txt").read_bytes() == b"kept3\n"


def test_revert_of_a_cache_tree_ignores_setforge_operation_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _tree_host(tmp_path, monkeypatch, {"c": "~/.cache"})
    host.config.write_text(
        host.config.read_text(encoding="utf-8")
        .replace(
            "profiles:\n",
            "packages:\n  pe: {type: extension, extension: pub.name}\nprofiles:\n",
        )
        .replace("tracked_files: [c]\n", "tracked_files: [c]\n    packages: [pe]\n"),
        encoding="utf-8",
    )
    host.shim("code", _FAKE_CODE)
    root = host.real_home / ".cache"
    root.mkdir()
    (root / "other").mkdir()
    (root / "other" / "u.txt").write_bytes(b"u\n")
    (root / "setforge-notes.txt").write_bytes(b"n\n")
    source = host.tracked_root / "c" / "kept.txt"

    for index in range(4):
        if index >= 2:
            source.write_bytes(f"kept{index}\n".encode())
        result = host.proc_install()
        assert result.returncode == 0, (index, _error_line(result))
    reverted = host.proc("revert", "--yes")

    assert reverted.returncode == 0, _error_line(reverted)
    assert (root / "other" / "u.txt").read_bytes() == b"u\n"
    assert (root / "setforge-notes.txt").read_bytes() == b"n\n"


@pytest.mark.parametrize("kind", ["symlink", "ancestor"])
def test_first_install_with_the_state_dir_named_by_its_real_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    host = Host(tmp_path, monkeypatch, kind=kind)
    source = host.tracked_root / "managed"
    source.mkdir()
    (source / "kept.txt").write_bytes(b"kept\n")
    host.config.write_text(
        "schema_version: '6.2'\nminimum_version: '6.2'\ntracked_files:\n"
        "  managed: {src: managed, dst: '~/.managed', tree: {}}\n"
        "profiles:\n  p:\n    tracked_files: [managed]\n",
        encoding="utf-8",
    )
    (host.real_home / ".managed").mkdir()
    state = host.real_home / ".managed" / ".sfstate"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    host.state = state

    first = host.install()

    assert first.exit_code == 0, first.output
