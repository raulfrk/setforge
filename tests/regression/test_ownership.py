"""Ownership claims: consent, release and SetForge's own state.

A file another configuration (a different checkout) claimed, or one nobody
claimed yet, is never taken over silently; every change of authority is
explicit and visible through ``ownership``. Cleanup never proposes SetForge's
own state for deletion."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from .support import INSTALL_FLAGS, Host, claim_ids_for

pytestmark = pytest.mark.integration

_NO_PROMPT = tuple(f for f in INSTALL_FLAGS if f != "--yes")


def _owners(host: Host) -> list[str]:
    listing = host.cli("ownership", "list", config=False, profile=False).output
    return re.findall(r"^\s+owner:\s+(\S+)", listing, flags=re.MULTILINE)


def _states(host: Host) -> list[str]:
    listing = host.cli("ownership", "list", config=False, profile=False).output
    return re.findall(r"^\s+state:\s+(\w+)", listing, flags=re.MULTILINE)


def _command_from(text: str, verb: str) -> list[str]:
    match = re.search(rf"`?(setforge ownership {verb} [^`\n]+)", text)
    assert match, text
    return shlex.split(match.group(1))[1:]


def test_adopting_an_existing_unowned_file_needs_consent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = Host(tmp_path, monkeypatch)
    host.live_dir.mkdir()
    host.live("note.txt").write_bytes(b"one\n")

    refused = host.cli("install", *_NO_PROMPT)

    assert refused.exit_code == 1
    assert host.cli("ownership", "list", config=False, profile=False).output == (
        "(no ownership claims)\n"
    )
    assert host.live("note.txt").read_bytes() == b"one\n"

    adopted = host.install()

    assert adopted.exit_code == 0, adopted.output
    assert host.live("note.txt").read_bytes() == b"one\n"
    assert _states(host) == ["claimed"]
    assert host.cli("compare", "--check").exit_code == 0


def test_a_file_claimed_by_another_configuration_is_transferred_only_with_consent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = Host(tmp_path, monkeypatch)
    other = host.other_checkout()
    assert host.install().exit_code == 0
    (first_owner,) = _owners(host)
    args = [f"--config={other}", f"--profile={host.profile}"]

    refused = host.cli("install", *_NO_PROMPT, *args, config=False, profile=False)

    assert refused.exit_code == 1
    assert "file ownership transfer" in refused.output
    assert _owners(host) == [first_owner]
    assert host.live("note.txt").read_bytes() == b"one\n"

    transferred = host.cli(
        "install", *INSTALL_FLAGS, *args, config=False, profile=False
    )

    assert transferred.exit_code == 0, transferred.output
    assert "transferred tracked file ownership" in transferred.output
    (second_owner,) = _owners(host)
    assert second_owner != first_owner
    assert host.live("note.txt").read_bytes() == b"one\n"


def test_release_is_explicit_keeps_the_file_and_install_names_the_way_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = Host(tmp_path, monkeypatch)
    assert host.install().exit_code == 0
    (claim,) = claim_ids_for(
        host.cli("ownership", "list", config=False, profile=False).output, "note.txt"
    )

    declined = host.cli("ownership", "release", claim, profile=False)
    assert declined.exit_code == 1
    assert _states(host) == ["claimed"]

    released = host.cli("ownership", "release", claim, "--yes", profile=False)
    assert released.exit_code == 0, released.output
    assert _states(host) == ["released"]
    assert host.live("note.txt").read_bytes() == b"one\n"
    assert "release" in host.cli("ownership", "history", profile=False).output
    pending = host.cli("ownership", "recover", profile=False)
    assert pending.exit_code == 0
    assert "no pending ownership transitions" in pending.output

    blocked = host.proc_install()
    assert blocked.returncode == 1
    assert "ownership revert" in blocked.stderr
    assert host.live("note.txt").read_bytes() == b"one\n"

    back = host.cli(
        *_command_from(blocked.stderr, "revert"), config=False, profile=False
    )
    assert back.exit_code == 0, back.output
    assert _states(host) == ["claimed"]
    assert host.install().exit_code == 0


def test_cleanup_apply_needs_the_claim_released_and_prints_the_exact_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = Host(
        tmp_path, monkeypatch, tracked={"keep.txt": "keep\n", "drop.txt": "drop\n"}
    )
    assert host.install().exit_code == 0
    host.config.write_text(
        host.config.read_text().replace("      - drop_txt\n", ""), encoding="utf-8"
    )

    preview = host.cli("cleanup-orphans")
    assert preview.exit_code == 0, preview.output
    assert "drop.txt" in preview.output

    refused = host.proc("cleanup-orphans", "--apply", "--yes")
    assert refused.returncode == 1
    assert host.live("drop.txt").exists()
    release = _command_from(refused.stderr, "release")
    assert release[2].startswith(
        claim_ids_for(
            host.cli("ownership", "list", config=False, profile=False).output,
            "/drop.txt",
        )[0][:8]
    )

    assert host.cli(*release, config=False, profile=False).exit_code == 0
    applied = host.cli("cleanup-orphans", "--apply", "--yes")
    assert applied.exit_code == 0, applied.output
    assert not host.live("drop.txt").exists()
    assert host.live("keep.txt").read_bytes() == b"keep\n"


_TREE_YAML = (
    "schema_version: '6.2'\n"
    "minimum_version: '6.2'\n"
    "tracked_files:\n"
    "{trees}"
    "profiles:\n"
    "  p:\n"
    "    tracked_files: [{ids}]\n"
)


def _tree_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    roots: dict[str, str],
    state_in_home: bool = False,
) -> Host:
    host = Host(tmp_path, monkeypatch, state_in_home=state_in_home)
    trees = ""
    for tree_id, dst in roots.items():
        source = host.tracked_root / tree_id
        source.mkdir()
        (source / "kept.txt").write_bytes(b"kept\n")
        trees += f"  {tree_id}: {{src: {tree_id}, dst: '{dst}', tree: {{}}}}\n"
    host.config.write_text(
        _TREE_YAML.format(trees=trees, ids=", ".join(roots)), encoding="utf-8"
    )
    return host


def _install_settled(host: Host) -> None:
    result = host.install()
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(
    "layout", ["state-dir", "local-no-state", "local-absent", "nested-state-root"]
)
def test_first_install_succeeds_when_setforge_roots_are_created_inside_the_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str
) -> None:
    if layout == "state-dir":
        host = _tree_host(
            tmp_path, monkeypatch, {"t": "~/.local/state"}, state_in_home=True
        )
    elif layout == "nested-state-root":
        host = _tree_host(tmp_path, monkeypatch, {"managed": "~/.managed"})
        state = host.real_home / ".managed" / "sub" / ".sfstate"
        (host.real_home / ".managed").mkdir()
        monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
        host.state = state
    else:
        host = _tree_host(tmp_path, monkeypatch, {"l": "~/.local"}, state_in_home=True)
        if layout == "local-no-state":
            (host.real_home / ".local" / "bin").mkdir(parents=True)

    _install_settled(host)


def test_scan_never_offers_state_that_sits_under_a_managed_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _tree_host(tmp_path, monkeypatch, {"managed": "~/.managed"})
    state = host.real_home / ".managed" / ".sfstate"
    state.parent.mkdir()
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    host.state = state
    _install_settled(host)
    stray = host.real_home / ".managed" / "stray.txt"
    stray.write_bytes(b"mine\n")
    assert host.cli("snapshot", "create", "s").exit_code == 0

    scan = host.cli("cleanup-orphans", "--scan")

    assert scan.exit_code == 0, scan.output
    flat = scan.output.replace("\n", "")
    assert str(stray) in flat
    assert ".sfstate" not in flat
    for entry in (state / "ownership", state / "transitions", state / "tree-inventory"):
        assert entry.exists()
    assert "no orphans" in host.cli("cleanup-orphans").output


def test_scan_never_offers_cache_journal_or_snapshot_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _tree_host(
        tmp_path,
        monkeypatch,
        {"c": "~/.cache", "s": "~/.local/share", "t": "~/.local/state"},
        state_in_home=True,
    )
    _install_settled(host)
    assert host.cli("snapshot", "create", "s").exit_code == 0
    (host.real_home / ".cache" / "stray.txt").write_bytes(b"mine\n")

    scan = host.cli("cleanup-orphans", "--scan")

    assert scan.exit_code == 0, scan.output
    assert "setforge/" not in scan.output.replace("\n", "")
    assert (host.real_home / ".cache" / "setforge" / "locks").is_dir()
    assert (host.real_home / ".local" / "share" / "setforge" / "snapshots").is_dir()
    assert (host.real_home / ".local" / "state" / "setforge" / "ownership").is_dir()
