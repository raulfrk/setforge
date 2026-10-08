"""Ownership claims: consent, release and SetForge's own state.

A file another configuration (a different checkout) claimed, or one nobody
claimed yet, is never taken over silently; every change of authority is
explicit and visible through ``ownership``. Cleanup never proposes SetForge's
own state for deletion."""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import setforge.cli.install as install_mod
from setforge import operations, paths, transitions
from setforge.locking import mutation_locks
from setforge.ownership import OwnershipError, read_owner_id
from setforge.reconcile.types import check_profile_name

from .support import INSTALL_FLAGS, REPO_ROOT, Host, claim_ids_for, tree

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


class _Tty:
    stdin = SimpleNamespace(isatty=lambda: True)


def _journals() -> list[Path]:
    root = paths.journals_root()
    return sorted(root.glob("*.json")) if root.exists() else []


def test_declining_the_adoption_question_leaves_no_change_and_no_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = Host(tmp_path, monkeypatch)
    host.live_dir.mkdir()
    host.live("note.txt").write_bytes(b"local\n")
    asked: list[str] = []

    def decline(text: str, **_kwargs: object) -> bool:
        asked.append(text)
        return False

    monkeypatch.setattr(install_mod, "sys", _Tty)
    monkeypatch.setattr(install_mod.typer, "confirm", decline)

    declined = host.cli("install", *_NO_PROMPT)

    assert declined.exit_code == 1
    assert ["note.txt" in text for text in asked] == [True]
    assert "file ownership change declined" in str(declined.exception)
    assert tree(host.live_dir) == {"note.txt": b"local\n"}
    assert host.cli("ownership", "list", config=False, profile=False).output == (
        "(no ownership claims)\n"
    )
    assert _journals() == []
    assert transitions.list_transitions(profile_filter=[host.profile]) == []
    with pytest.raises(OwnershipError):
        read_owner_id(host.repo)
    # The lock was released: the next command runs instead of waiting.
    assert host.proc("install", *INSTALL_FLAGS).returncode == 0


def test_another_install_waits_while_the_adoption_question_is_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = Host(tmp_path, monkeypatch)
    host.live_dir.mkdir()
    host.live("note.txt").write_bytes(b"local\n")
    waiting: list[subprocess.Popen[str]] = []
    journals_while_asked: list[list[Path]] = []

    def answer_after_another_install_waited(*_args: object, **_kwargs: object) -> bool:
        other = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "setforge.cli",
                "install",
                *INSTALL_FLAGS,
                f"--config={host.config}",
                f"--profile={host.profile}",
            ],
            cwd=REPO_ROOT,
            env=host.proc_env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        waiting.append(other)
        with pytest.raises(subprocess.TimeoutExpired):
            other.wait(timeout=4)
        # Nothing is journalled before the answer.
        journals_while_asked.append(_journals())
        return True

    monkeypatch.setattr(install_mod, "sys", _Tty)
    monkeypatch.setattr(
        install_mod.typer, "confirm", answer_after_another_install_waited
    )
    try:
        accepted = host.cli("install", *_NO_PROMPT)
        assert len(waiting) == 1, accepted.output
        out, err = waiting[0].communicate(timeout=60)
    finally:
        for other in waiting:
            if other.poll() is None:
                other.kill()
                other.wait()

    assert accepted.exit_code == 0, accepted.output
    assert journals_while_asked == [[]]
    assert waiting[0].returncode == 0, (out, err)
    assert host.live("note.txt").read_bytes() == b"local\n"
    assert _states(host) == ["claimed"]


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
    kind: str = "plain",
) -> Host:
    host = Host(tmp_path, monkeypatch, state_in_home=state_in_home, kind=kind)
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


@pytest.mark.parametrize("kind", ["plain", "symlink", "ancestor"])
@pytest.mark.parametrize(
    "layout",
    ["state-dir", "local-absent", "cache-dir", "nested-state-root", "data-root-first"],
)
def test_first_install_succeeds_when_setforge_roots_are_created_inside_the_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str, kind: str
) -> None:
    if layout == "nested-state-root":
        host = _tree_host(tmp_path, monkeypatch, {"t": "~/.managed"}, kind=kind)
        root = host.real_home / ".managed"
        state = root / "sub" / ".sfstate"
        monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
        host.state = state
    else:
        dst = {
            "state-dir": ".local/state",
            "local-absent": ".local",
            "cache-dir": ".cache",
            "data-root-first": ".local/share",
        }[layout]
        host = _tree_host(
            tmp_path, monkeypatch, {"t": f"~/{dst}"}, state_in_home=True, kind=kind
        )
        root = host.real_home / dst
        if layout == "data-root-first":
            assert host.cli("snapshot", "create", "s").exit_code == 0
            assert (root / "setforge").is_dir()

    first = host.cli("install", *_NO_PROMPT)

    assert first.exit_code == 0, (first.output, first.exception)
    assert (root / "kept.txt").read_bytes() == b"kept\n"
    assert host.cli("compare", "--check").exit_code == 0


@pytest.mark.parametrize(
    "layout",
    [
        "user-directory",
        "beside-the-state-root",
        "user-symlink",
        "excluded-user-file",
        "empty-without-setforge",
    ],
)
def test_a_root_that_holds_anything_of_the_users_is_adopted_only_with_consent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str
) -> None:
    if layout == "empty-without-setforge":
        host = _tree_host(tmp_path, monkeypatch, {"t": "~/.managed"})
        (host.real_home / ".managed").mkdir()
    else:
        host = _tree_host(tmp_path, monkeypatch, {"t": "~/.local"}, state_in_home=True)
        local = host.real_home / ".local"
        if layout == "user-directory":
            (local / "bin").mkdir(parents=True)
        elif layout == "beside-the-state-root":
            (local / "state" / "other").mkdir(parents=True)
        elif layout == "user-symlink":
            local.mkdir()
            (local / "link").symlink_to("elsewhere")
        else:
            local.mkdir()
            (local / "mine.log").write_bytes(b"mine\n")
            host.config.write_text(
                host.config.read_text(encoding="utf-8").replace(
                    "tree: {}", "tree: {exclude: [mine.log]}"
                ),
                encoding="utf-8",
            )

    refused = host.cli("install", *_NO_PROMPT)

    assert refused.exit_code == 1
    assert "file adoption requires confirmation" in str(refused.exception)
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


# Each step is the last thing the command completes before it is killed, in
# the order the command performs them.
_KILL_AFTER = """
import os
from setforge import operations
from setforge.cli import main
from setforge.ownership import OwnershipStore
from setforge.ownership_history import OwnershipHistoryStore

step = os.environ["SETFORGE_KILL_AFTER"]
holder, name = {
    "journal-written": (operations, "prepare"),
    "checkpoint-begun": (operations, "begin_checkpoint"),
    "claim-staged": (OwnershipStore, "_write_claim"),
    "claim-written": (OwnershipStore, "_write_claim"),
    "history-staged": (OwnershipHistoryStore, "_commit_transition"),
    "history-written": (OwnershipHistoryStore, "_commit_transition"),
    "checkpoint-finished": (operations, "finish_checkpoint"),
}[step]
real = getattr(holder, name)

def killed(*args, **kwargs):
    if step.endswith("-staged"):
        # The write dies with its temporary file staged but not yet renamed.
        os.replace = lambda *args, **kwargs: os._exit(79)
    real(*args, **kwargs)
    os._exit(79)

setattr(holder, name, killed)
main()
"""


def _staged_files(host: Host) -> list[str]:
    """Temporary files a killed atomic write left in SetForge state."""
    return [rel for rel in tree(host.state) if rel.endswith(".tmp")]


def _ownership_state(host: Host) -> dict[str, bytes | str]:
    """Every byte of SetForge state except lock files, plus the live file.

    An owner's history directory is made before the first release is
    journaled and a killed write may leave its temporary file behind; recovery
    keeps both, and neither holds a record, so they are left out.
    """
    root = "ownership-history"
    owner = f"{root}/{read_owner_id(host.repo)}"
    state = {
        rel: content
        for rel, content in tree(host.state).items()
        if not rel.startswith("locks")
        and not (content == "<dir>" and rel in (root, owner, f"{owner}/transitions"))
    }
    return {**state, "live": host.live("note.txt").read_bytes()}


@pytest.mark.parametrize("action", ["release", "revert"])
@pytest.mark.parametrize(
    "step",
    [
        "journal-written",
        "checkpoint-begun",
        "claim-staged",
        "claim-written",
        "history-staged",
        "history-written",
        "checkpoint-finished",
    ],
)
def test_killed_release_or_revert_is_undone_by_recover_and_can_be_repeated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str, step: str
) -> None:
    host = Host(tmp_path, monkeypatch)
    assert host.install().exit_code == 0
    (claim,) = claim_ids_for(
        host.cli("ownership", "list", config=False, profile=False).output, "note.txt"
    )
    command = ["ownership", "release", claim, "--yes", f"--config={host.config}"]
    if action == "revert":
        assert host.cli(*command[:-1], profile=False).exit_code == 0
        transition = host.cli("ownership", "history", profile=False).output.split()[0]
        command[1:3] = ["revert", transition]
    profile = f"ownership-{read_owner_id(host.repo)}"
    check_profile_name(profile)
    journals = host.home / ".cache" / "setforge" / "operations"
    before = _ownership_state(host)

    killed = subprocess.run(
        [sys.executable, "-c", _KILL_AFTER, *command],
        cwd=REPO_ROOT,
        env={**host.proc_env(), "SETFORGE_KILL_AFTER": step},
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert killed.returncode == 79, (killed.stdout, killed.stderr)
    assert len(tuple(journals.glob("*.json"))) == 1
    changed = step not in ("journal-written", "checkpoint-begun")
    assert (_ownership_state(host) != before) is changed
    staged = sorted(_staged_files(host))
    assert len(staged) == step.endswith("-staged")
    assert host.cli("ownership", "list", config=False, profile=False).exit_code == 0
    assert host.cli("status").exit_code == 0
    refusal = f"`setforge recover --profile={profile}`"
    blocked = host.install()
    assert blocked.exit_code == 1
    assert refusal in str(blocked.exception)
    again = host.cli(*command, profile=False)
    assert again.exit_code == 1
    assert refusal in str(again.exception)
    assert len(tuple(journals.glob("*.json"))) == 1
    legacy = host.cli("ownership", "recover", profile=False)
    assert legacy.exit_code == 0
    assert f"setforge recover --profile={profile} --apply" in legacy.output
    assert len(tuple(journals.glob("*.json"))) == 1

    recovered = host.proc(
        "recover",
        f"--profile={profile}",
        "--apply",
        "--yes",
        config=False,
        profile=False,
    )

    assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)
    after = _ownership_state(host)
    # Recovery keeps the one temporary file the kill left and adds none.
    assert sorted(_staged_files(host)) == staged
    for rel in staged:
        del after[rel]
    assert after == before
    assert host.cli("ownership", "history", profile=False).exit_code == 0
    assert not tuple(journals.glob("*.json"))
    assert _states(host) == ["released" if action == "revert" else "claimed"]
    repeated = host.proc(*command, config=False, profile=False)
    assert repeated.returncode == 0, (repeated.stdout, repeated.stderr)
    assert _states(host) == ["claimed" if action == "revert" else "released"]


@pytest.mark.parametrize("action", ["release", "revert"])
def test_release_or_revert_waits_for_a_running_operation_instead_of_refusing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    host = Host(tmp_path, monkeypatch)
    assert host.install().exit_code == 0
    (claim,) = claim_ids_for(
        host.cli("ownership", "list", config=False, profile=False).output, "note.txt"
    )
    command = ["ownership", "release", claim, "--yes", f"--config={host.config}"]
    if action == "revert":
        assert host.cli(*command[:-1], profile=False).exit_code == 0
        transition = host.cli("ownership", "history", profile=False).output.split()[0]
        command[1:3] = ["revert", transition]

    # Another command is mid-operation: it holds the gate and its journal is
    # on disk. That journal is healthy, not abandoned, so the command waits.
    waiting: subprocess.Popen[str] | None = None
    try:
        with mutation_locks():
            running = operations.prepare(
                command="sync",
                profile="p",
                config_dir=None,
                resources_lock=False,
                paths=(),
            )
            waiting = subprocess.Popen(
                [sys.executable, "-m", "setforge.cli", *command],
                cwd=REPO_ROOT,
                env=host.proc_env(),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            with pytest.raises(subprocess.TimeoutExpired):
                waiting.wait(timeout=4)
            operations.complete(running)
        out, err = waiting.communicate(timeout=60)
    finally:
        if waiting is not None and waiting.poll() is None:
            waiting.kill()
            waiting.wait()

    assert waiting.returncode == 0, (out, err)
    assert _states(host) == ["claimed" if action == "revert" else "released"]
