"""Fresh-process recovery for roots created during a mixed install."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from setforge import locking, operations, snapshots, transitions
from setforge.ownership import OwnershipStore, resolve_owner_common_dir

pytestmark = pytest.mark.test_infra


_CRASH_INSTALL = """
import importlib
import os
from pathlib import Path

import setforge
from setforge import locking, operations

root = Path(os.environ["SETFORGE_TEST_ROOT"]).resolve()
source = Path(os.environ["SETFORGE_TEST_SOURCE_ROOT"]).resolve()
assert Path(setforge.__file__).resolve().is_relative_to(source)
install = importlib.import_module("setforge.cli.install")
point = os.environ["SETFORGE_CRASH_POINT"]

if point in {"before", "after", "nonempty", "unbound-file"}:
    original = locking.TargetLockGuard.mkdir

    def crash_mkdir(self, mode=0o777):
        assert self.target.resolve().is_relative_to(root)
        journal = operations.active("p")
        assert journal is not None
        assert journal.checkpoints[-1].name == "prepare-target-roots"
        assert not journal.checkpoints[-1].completed
        if point == "before":
            os._exit(79)
        original(self, mode=mode)
        if point == "unbound-file":
            self.target.rmdir()
            self.target.write_bytes(b"preserve replacement file")
            os._exit(87)
        if point == "nonempty":
            foreign = self.target / "tree" / "item"
            assert foreign in {item.path for item in journal.paths}
            foreign.parent.mkdir()
            foreign.write_bytes(b"preserve unknown bytes")
            os._exit(81)
        os._exit(80)

    locking.TargetLockGuard.mkdir = crash_mkdir
elif point == "bound":
    original = operations.bind_install_roots

    def crash_bind(*args, **kwargs):
        journal = original(*args, **kwargs)
        assert journal.checkpoints[-1].name == "prepare-target-roots"
        assert not journal.checkpoints[-1].completed
        assert any(guard.exists for guard in journal.path_guards)
        os._exit(82)

    operations.bind_install_roots = crash_bind
elif point == "empty-root":
    def crash_after_prepare(_plan):
        os._exit(85)

    install._apply_secret_plan = crash_after_prepare
else:
    assert point in {"mode", "tree"}
    original = install.apply_tree

    def crash_tree(*args, **kwargs):
        result = original(*args, **kwargs)
        destination = Path(os.environ["SETFORGE_TEST_DESTINATION"])
        assert destination.resolve().is_relative_to(root)
        if point == "mode":
            assert destination.stat().st_mode & 0o777 == 0o700
            os._exit(84)
        assert (destination / "item").read_bytes() == b"tree\\n"
        os._exit(86)

    install.apply_tree = crash_tree

from setforge.cli import main
main()
"""


_CRASH_RECOVER_AFTER_ROOT_REMOVAL = """
import os
from pathlib import Path

import setforge
from setforge import operations

root = Path(os.environ["SETFORGE_TEST_ROOT"]).resolve()
source = Path(os.environ["SETFORGE_TEST_SOURCE_ROOT"]).resolve()
assert Path(setforge.__file__).resolve().is_relative_to(source)
prepared = Path(os.environ["SETFORGE_TEST_PREPARED_ROOT"]).resolve(strict=True)
assert prepared.is_relative_to(root)
restore = operations._restore_path

def interrupt_after_removal(snapshot, *args, **kwargs):
    result = restore(snapshot, *args, **kwargs)
    if snapshot.kind is operations.SnapshotKind.ABSENT and snapshot.path == prepared:
        assert not prepared.exists()
        assert operations.active("p") is not None
        os._exit(89)
    return result

operations._restore_path = interrupt_after_removal
from setforge.cli import main
main()
"""


def _fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    empty_tree: bool = False,
    existing_parent: bool = False,
    symlinked_home: bool = False,
) -> tuple[Path, Path, Path, dict[str, str]]:
    root = tmp_path.resolve()
    home = root / "home"
    if symlinked_home:
        (root / "realhome").mkdir()
        home.symlink_to(root / "realhome", target_is_directory=True)
    else:
        home.mkdir()
    live = home / "live"
    if existing_parent:
        live.mkdir()
    repo = root / "repo"
    tree = repo / "tracked/tree"
    tree.mkdir(parents=True)
    if not empty_tree:
        (tree / "item").write_bytes(b"tree\n")
    scanner = root / "gitleaks"
    scanner.write_text("#!/bin/sh\nexit 0\n")
    scanner.chmod(0o755)
    config = repo / "setforge.yaml"
    YAML().dump(
        {
            "schema_version": "6.5",
            "minimum_version": "6.4",
            "tracked_files": {
                "tree": {"src": "tree", "dst": str(live / "tree"), "tree": {}}
            },
            "profiles": {"p": {"tracked_files": ["tree"]}},
        },
        config,
    )
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(
        HOME=str(home),
        CODEX_HOME=str(home / ".codex"),
        SETFORGE_STATE_DIR=str(root / "state"),
        SETFORGE_GITLEAKS_BIN=str(scanner),
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_ALLOW_PROTOCOL="file",
        PYTHONPATH=str(Path(__file__).resolve().parents[2]),
        SETFORGE_TEST_ROOT=str(root),
        SETFORGE_TEST_SOURCE_ROOT=str(Path(__file__).resolve().parents[2]),
    )
    assert all(
        path.resolve().is_relative_to(root)
        for path in (home, repo, tree, config, live / "tree", scanner)
    )
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], env=env, check=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(root / "state"))
    assert all(
        path.resolve().is_relative_to(root)
        for path in (
            operations.journals_root(),
            transitions.state_root(),
            snapshots.snapshots_root(),
            OwnershipStore().root,
            resolve_owner_common_dir(repo),
            locking._user_global_locks_dir(),
        )
    )
    return config, live, repo, env


def _crash(
    config: Path, repo: Path, env: dict[str, str], point: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _CRASH_INSTALL,
            "install",
            "--profile=p",
            f"--config={config}",
            "--yes",
            "--no-fetch",
            "--no-git-check",
        ],
        cwd=repo,
        env={**env, "SETFORGE_CRASH_POINT": point},
        capture_output=True,
        text=True,
        timeout=45,
    )


def _recover(repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "setforge.cli",
            "recover",
            "--profile=p",
            "--apply",
            "--yes",
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
    )


def _crash_recover_after_root_removal(
    repo: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _CRASH_RECOVER_AFTER_ROOT_REMOVAL,
            "recover",
            "--profile=p",
            "--apply",
            "--yes",
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
    )


@pytest.mark.parametrize(
    ("point", "exit_code"),
    [
        ("before", 79),
        ("after", 80),
        ("nonempty", 81),
        ("bound", 82),
        ("mode", 84),
        ("empty-root", 85),
        ("unbound-file", 87),
    ],
)
def test_install_root_preparation_recovers_in_fresh_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    point: str,
    exit_code: int,
) -> None:
    existing = point in {"mode", "empty-root"}
    config, live, repo, env = _fixture(
        tmp_path,
        monkeypatch,
        empty_tree=point == "empty-root",
        existing_parent=existing,
    )
    if point == "mode":
        (repo / "tracked/tree").chmod(0o700)
    env["SETFORGE_TEST_DESTINATION"] = str(live / "tree")
    crashed = _crash(config, repo, env, point)
    assert crashed.returncode == exit_code, (crashed.stdout, crashed.stderr)
    journal = operations.active("p")
    assert journal is not None
    if point == "empty-root":
        root = live / "tree"
        assert any(guard.path == root for guard in journal.path_guards)
        assert {
            item.path
            for item in journal.paths
            if item.path == root or item.path.is_relative_to(root)
        } == {root}
    if point == "unbound-file":
        replacement = live
        assert replacement.read_bytes() == b"preserve replacement file"
        refused = _recover(repo, env)
        assert refused.returncode == 1
        assert "journaled absent parent changed" in refused.stderr
        assert "Traceback" not in refused.stderr
        assert replacement.read_bytes() == b"preserve replacement file"
        assert operations.active("p") is not None
        replacement.unlink()
    if point == "nonempty":
        refused = _recover(repo, env)
        assert refused.returncode == 1
        assert (
            f"unbound install root has content before recovery: {live}"
            in refused.stderr
        )
        assert "Traceback" not in refused.stderr
        foreign = live / "tree" / "item"
        assert foreign.read_bytes() == b"preserve unknown bytes"
        assert operations.active("p") is not None
        foreign.unlink()
        foreign.parent.rmdir()
    recovered = _recover(repo, env)
    assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)
    assert operations.active("p") is None
    assert not (live / "tree").exists()
    if not existing:
        assert not live.exists()
    else:
        assert live.is_dir()


def test_install_recovery_resumes_after_bound_root_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, live, repo, env = _fixture(tmp_path, monkeypatch)
    env["SETFORGE_TEST_DESTINATION"] = str(live / "tree")
    env["SETFORGE_TEST_PREPARED_ROOT"] = str(live)
    crashed = _crash(config, repo, env, "tree")
    assert crashed.returncode == 86, (crashed.stdout, crashed.stderr)
    assert (live / "tree/item").read_bytes() == b"tree\n"

    interrupted = _crash_recover_after_root_removal(repo, env)
    assert interrupted.returncode == 89, (interrupted.stdout, interrupted.stderr)
    assert not live.exists()
    assert operations.active("p") is not None

    resumed = _recover(repo, env)
    assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
    assert not live.exists()
    assert operations.active("p") is None


@pytest.mark.parametrize(
    ("depth", "retarget", "point"),
    [
        (1, False, "after"),
        (2, False, "after"),
        (1, False, "tree"),
        (2, False, "tree"),
        (1, True, "tree"),
    ],
)
def test_install_alias_recovers_in_fresh_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    depth: int,
    retarget: bool,
    point: str,
) -> None:
    config, _live, repo, env = _fixture(tmp_path, monkeypatch)
    nested_source = repo / "tracked/tree/folder/item"
    assert nested_source.resolve().is_relative_to(tmp_path.resolve())
    nested_source.parent.mkdir()
    nested_source.write_bytes(b"nested tree\n")
    home = tmp_path / "home"
    real = home / "real"
    real.mkdir()
    middle = home / "middle"
    if depth == 2:
        middle.symlink_to(real, target_is_directory=True)
    alias = home / "alias"
    alias.symlink_to(middle if depth == 2 else real, target_is_directory=True)
    document = YAML().load(config.read_text())
    document["tracked_files"]["tree"]["dst"] = str(alias / "tree")
    YAML().dump(document, config)
    destination = alias / "tree"
    env["SETFORGE_TEST_DESTINATION"] = str(destination)
    assert destination.resolve().is_relative_to(tmp_path.resolve())

    crashed = _crash(config, repo, env, point)
    assert crashed.returncode == (80 if point == "after" else 86), (
        crashed.stdout,
        crashed.stderr,
    )
    if point == "tree":
        assert (real / "tree/item").read_bytes() == b"tree\n"
        assert (real / "tree/folder/item").read_bytes() == b"nested tree\n"
    else:
        assert (real / "tree").is_dir()
        assert not (real / "tree/item").exists()
    assert operations.active("p") is not None
    if retarget:
        outside = tmp_path / "outside/tree"
        outside.mkdir(parents=True)
        sentinel = outside / "item"
        sentinel.write_bytes(b"foreign alias bytes")
        inode = sentinel.stat().st_ino
        alias.unlink()
        alias.symlink_to(outside.parent, target_is_directory=True)
        assert alias.resolve().is_relative_to(tmp_path.resolve())
        refused = _recover(repo, env)
        assert refused.returncode != 0
        assert sentinel.read_bytes() == b"foreign alias bytes"
        assert sentinel.stat().st_ino == inode
        assert operations.active("p") is not None
        alias.unlink()
        alias.symlink_to(real, target_is_directory=True)
    recovered = _recover(repo, env)
    assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)
    assert alias.is_symlink()
    if depth == 2:
        assert middle.is_symlink()
    assert not (real / "tree").exists()
    assert operations.active("p") is None
    if retarget:
        assert sentinel.read_bytes() == b"foreign alias bytes"
        assert sentinel.stat().st_ino == inode


def _manifest(directory: Path) -> dict[str, tuple[int, int, bytes | str | None]]:
    """Describe everything below ``directory`` except SetForge's own cache."""
    entries: dict[str, tuple[int, int, bytes | str | None]] = {}
    for path in sorted(directory.rglob("*")):
        if path.relative_to(directory).parts[0] == ".cache":
            continue
        info = path.lstat()
        content: bytes | str | None = None
        if path.is_symlink():
            content = str(path.readlink())
        elif path.is_file():
            content = path.read_bytes()
        entries[str(path.relative_to(directory))] = (
            info.st_ino,
            info.st_mode,
            content,
        )
    return entries


@pytest.mark.parametrize(
    ("point", "replaced"),
    [("after", False), ("bound", False), ("tree", False), ("tree", True)],
)
def test_install_below_symlinked_home_recovers_only_its_absent_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, point: str, replaced: bool
) -> None:
    config, _live, repo, env = _fixture(tmp_path, monkeypatch, symlinked_home=True)
    home = tmp_path / "home"
    real = tmp_path / "realhome"
    outside = tmp_path / "outside"
    for bystander in (real / "notes", outside / ".claude/tree/item"):
        bystander.parent.mkdir(parents=True, exist_ok=True)
        bystander.write_bytes(b"bystander\n")
    destination = home / ".claude/tree"
    document = YAML().load(config.read_text())
    document["tracked_files"]["tree"]["dst"] = str(destination)
    YAML().dump(document, config)
    env["SETFORGE_TEST_DESTINATION"] = str(destination)
    assert destination.resolve().is_relative_to(tmp_path.resolve())
    before = _manifest(real), _manifest(outside)
    assert set(before[0]) == {"notes"}

    crashed = _crash(config, repo, env, point)
    assert crashed.returncode == {"after": 80, "bound": 82, "tree": 86}[point], (
        crashed.stdout,
        crashed.stderr,
    )
    assert (real / ".claude").is_dir()
    if point == "tree":
        assert (real / ".claude/tree/item").read_bytes() == b"tree\n"
    assert operations.active("p") is not None
    if replaced:
        created = tmp_path / "created"
        (real / ".claude").rename(created)
        foreign = real / ".claude/tree/item"
        foreign.parent.mkdir(parents=True)
        foreign.write_bytes(b"foreign root bytes")
        replacement = _manifest(real)
        refused = _recover(repo, env)
        assert refused.returncode == 1
        assert "parent changed" in refused.stderr
        assert "Traceback" not in refused.stderr
        assert _manifest(real) == replacement
        assert (created / "tree/item").read_bytes() == b"tree\n"
        assert operations.active("p") is not None
        shutil.rmtree(real / ".claude")
        created.rename(real / ".claude")

    recovered = _recover(repo, env)

    assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)
    assert operations.active("p") is None
    assert home.is_symlink()
    assert home.resolve() == real.resolve()
    assert (_manifest(real), _manifest(outside)) == before
