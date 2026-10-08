"""Tests for SetForge's mutation gate, profile lock, and target guards."""

import ast
import errno
import fcntl
import inspect
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypedDict

import pytest
from typer.testing import CliRunner, Result

from setforge import locking
from setforge.cli import app
from setforge.errors import SetforgeError
from setforge.locking import (
    TargetLockRequest,
    _profile_lock_path,
    mutation_locks,
    profile_lock,
    require_resources_lock,
    target_guards,
)
from setforge.transitions import state_root
from tests.conftest import FakeClaude, FakeGit, _local_clone_yaml
from tests.shared_helpers import load_yaml


class _MutationLockKwargs(TypedDict, total=False):
    resources: bool
    config_dir: Path
    config_dirs: tuple[Path, ...]
    target_roots: tuple[Path, ...]
    profile: str


@pytest.fixture(autouse=True)
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    return tmp_path


def test_lock_creates_lockfile_and_runs_body() -> None:
    """profile_lock creates its digest-named sidecar and runs the body."""
    executed: list[bool] = []
    with profile_lock("p"):
        lock_path = _profile_lock_path("p")
        assert lock_path.exists(), "lockfile must exist while the lock is held"
        executed.append(True)
    assert executed == [True]


def test_lock_released_after_exit() -> None:
    """After the ``with`` block the fd is released; a second acquire succeeds."""
    with profile_lock("p"):
        pass
    # If the fd leaked we'd hang here because flock(LOCK_EX) on the same
    # file from the same process would block (flock is per open-file-
    # description, not per-path, so a second open + LOCK_NB below would
    # still succeed even with a leak — but open + LOCK_EX + LOCK_NB from
    # a second fd is the correct re-entrant test).
    lock_path = _profile_lock_path("p")
    fd = lock_path.open("a")
    try:
        # LOCK_NB: if the lock were still held this would raise BlockingIOError
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        # If we get here the fd is free — release it.
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    finally:
        fd.close()


def test_timeout_raises_on_contention(state_dir: Path) -> None:
    """Lock held by another fd: profile_lock(..., timeout=0.2) raises SetforgeError."""
    lock_path = _profile_lock_path("p")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch()

    # Hold the lock ourselves via a second fd (flock is per open-file-
    # description, so opening a second fd and locking it is exactly the
    # in-process contention signal the poll path sees).
    holder = lock_path.open("a")
    try:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        with pytest.raises(  # noqa: SIM117 — outer raises context cannot be merged with inner lock
            SetforgeError, match="another setforge process holds the lock"
        ):
            with profile_lock("p", timeout=0.2):
                pass
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()


@pytest.mark.parametrize("timeout", [None, 0.2])
def test_lock_refused_by_the_filesystem_names_the_lock_file(
    monkeypatch: pytest.MonkeyPatch, timeout: float | None
) -> None:
    def no_locks(fd: int, operation: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(fcntl, "flock", no_locks)

    with (
        pytest.raises(SetforgeError) as failure,
        profile_lock("p", timeout=timeout),
    ):
        pytest.fail("lock body ran without the lock")

    message = str(failure.value)
    assert f"cannot lock {_profile_lock_path('p')}: No locks available" in message
    assert "filesystem refused the lock" in message


def test_journal_registry_lock_refusal_is_a_clean_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from setforge import operations

    def no_locks(fd: int, operation: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(fcntl, "flock", no_locks)

    with pytest.raises(SetforgeError, match=r"cannot lock .*\.registry\.lock"):
        operations.prepare(
            command="sync",
            profile="p",
            config_dir=None,
            resources_lock=False,
            paths=(),
        )


def test_different_profiles_do_not_block_each_other(state_dir: Path) -> None:
    """profile_lock("a") held, profile_lock("b", timeout=0.2) must succeed."""
    lock_a = _profile_lock_path("a")
    lock_a.parent.mkdir(parents=True, exist_ok=True)
    lock_a.touch()

    holder = lock_a.open("a")
    try:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        # "b" is a different lockfile — should not be affected.
        executed: list[bool] = []
        with profile_lock("b", timeout=0.2):
            executed.append(True)
        assert executed == [True]
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()


def test_profile_lock_name_cannot_escape_for_nested_or_traversal_profile() -> None:
    for profile in ("team/dev", "../escape"):
        with profile_lock(profile):
            lock_path = _profile_lock_path(profile)
            assert lock_path.parent == state_root() / "locks"
            assert lock_path.name.startswith("profile-")
            assert lock_path.suffix == ".lock"


def test_target_guard_rebinds_a_target_it_created(tmp_path: Path) -> None:
    parent = tmp_path / "projects"
    parent.mkdir()
    target = parent / "demo"

    with target_guards((TargetLockRequest(target),)) as guards:
        guards[0].mkdir()

    with target_guards((TargetLockRequest(target),)):
        assert target.is_dir()


def test_target_guard_refuses_parent_or_target_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "projects"
    parent.mkdir()
    target = parent / "demo"
    original = __import__("setforge.locking", fromlist=["_target_snapshot"])
    real_snapshot = original._target_snapshot

    def replacing_snapshot(request: TargetLockRequest) -> object:
        snapshot = real_snapshot(request)
        target.mkdir()
        return snapshot

    monkeypatch.setattr("setforge.locking._target_snapshot", replacing_snapshot)
    with (
        pytest.raises(SetforgeError, match="target changed"),
        target_guards((TargetLockRequest(target),)),
    ):
        pass


def test_target_guard_refuses_dangling_symlink(tmp_path: Path) -> None:
    target = tmp_path / "dangling"
    target.symlink_to(tmp_path / "missing", target_is_directory=True)

    with (
        pytest.raises(SetforgeError, match="dangling symlink"),
        target_guards((TargetLockRequest(target),)),
    ):
        pass


def test_target_guard_anchors_publication_and_detects_parent_swap(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "projects"
    displaced = tmp_path / "displaced"
    parent.mkdir()
    target = parent / "demo"

    def swap_and_publish() -> None:
        with target_guards((TargetLockRequest(target),)) as guards:
            parent.rename(displaced)
            parent.mkdir()
            guards[0].mkdir()

    with pytest.raises(SetforgeError, match="parent binding changed"):
        swap_and_publish()

    assert not (displaced / "demo").exists()
    assert not target.exists()


def test_mutation_locks_exposes_prepublication_target_guards(tmp_path: Path) -> None:
    parent = tmp_path / "projects"
    displaced = tmp_path / "displaced"
    parent.mkdir()
    target = parent / "demo"
    published: list[bool] = []

    def attempt_publication() -> None:
        with mutation_locks(target_roots=(target,)) as guards:
            parent.rename(displaced)
            parent.mkdir()
            guards.verify_targets()
            published.append(True)

    with pytest.raises(SetforgeError, match="parent binding changed"):
        attempt_publication()
    assert published == []


@pytest.mark.parametrize("replace", [False, True])
def test_created_target_is_bound_until_lock_exit(tmp_path: Path, replace: bool) -> None:
    target = tmp_path / "demo"

    def create_then_change() -> None:
        with target_guards((TargetLockRequest(target),)) as guards:
            guards[0].mkdir()
            target.rmdir()
            if replace:
                target.mkdir()

    with pytest.raises(SetforgeError, match="target changed"):
        create_then_change()


def test_target_guard_binds_opened_parent_before_entering_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "projects"
    displaced = tmp_path / "displaced"
    parent.mkdir()
    target = parent / "demo"
    real_open = os.open
    swapped = False
    entered: list[bool] = []

    def swap_before_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if not swapped and path == parent and dir_fd is None:
            swapped = True
            parent.rename(displaced)
            parent.mkdir()
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr("setforge.locking.os.open", swap_before_open)

    def acquire() -> None:
        with target_guards((TargetLockRequest(target),)):
            entered.append(True)

    with pytest.raises(SetforgeError, match="before descriptor binding"):
        acquire()
    assert entered == []
    assert not target.exists()


@pytest.mark.parametrize(
    "lock_kwargs",
    [
        {},
        {"resources": True},
        {"config_dir": Path("config-a")},
        {"target_roots": (Path("target-c"),)},
        {"profile": "profile-b"},
    ],
)
def test_global_mutation_gate_serializes_prepublication_across_scopes(
    lock_kwargs: _MutationLockKwargs,
) -> None:
    """No mutation scope runs while another process holds the gate.

    The gate lives under the user's cache, not ``SETFORGE_STATE_DIR``, so an
    alternate transition root cannot split it.
    """
    gate = Path.home() / ".cache/setforge/locks/mutation-gate.lock"
    gate.parent.mkdir(parents=True, exist_ok=True)
    holder = gate.open("a")
    try:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        with (
            pytest.raises(SetforgeError, match="global mutation gate"),
            mutation_locks(timeout=0.01, **lock_kwargs),
        ):
            pass
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()


@pytest.mark.parametrize(
    "lock_kwargs",
    [
        {},
        {"resources": True},
        {"profile": "first"},
        {"profile": "other"},
        {"config_dir": Path("cfg")},
        {"config_dir": Path("unrelated")},
        {"target_roots": (Path("target"),)},
    ],
)
def test_pending_journal_refuses_every_mutation_except_its_own_recovery(
    lock_kwargs: _MutationLockKwargs,
    tmp_path: Path,
) -> None:
    from setforge import operations

    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    if "config_dir" in lock_kwargs:
        lock_kwargs["config_dir"] = tmp_path / lock_kwargs["config_dir"]
    if "target_roots" in lock_kwargs:
        lock_kwargs["target_roots"] = (tmp_path / "target",)
    journal = operations.prepare(
        command="install",
        profile="first",
        config_dir=config_dir,
        resources_lock=False,
        paths=(),
    )

    with (
        pytest.raises(
            SetforgeError,
            match=r"unfinished install .* run `setforge recover --profile=first`",
        ),
        mutation_locks(**lock_kwargs),
    ):
        pytest.fail("mutation ran beside an unfinished operation")

    with mutation_locks(**lock_kwargs, allow_operation_id=journal.operation_id):
        operations.complete(journal)
    with mutation_locks(**lock_kwargs):
        pass


_LOCK_ENTRIES = {"mutation_locks", "operations.transaction"}


def _with_entries(source: str) -> list[tuple[ast.With, list[ast.Call]]]:
    """Each ``with`` statement and its context-manager calls in entry order."""
    return [
        (
            node,
            [
                item.context_expr
                for item in node.items
                if isinstance(item.context_expr, ast.Call)
            ],
        )
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.With)
    ]


def _names(calls: list[ast.Call]) -> list[str]:
    return [ast.unparse(call.func) for call in calls]


def test_global_resource_writers_share_one_lock() -> None:
    """Every CLI writer of adapter/cache state enters the canonical lock."""
    from setforge.cli import ext, install, plugins, revert

    writers = (
        install.install,
        ext.ext_add,
        ext.ext_remove,
        ext._run_ext_reconcile,
        plugins.plugin_add,
        plugins.plugin_remove,
        plugins._run_plugin_reconcile,
        plugins.sync_cache,
        plugins.marketplace_add_cmd,
        plugins.marketplace_remove_cmd,
        plugins.marketplace_update_cmd,
        revert._apply_confirmed_reverts,
    )
    missing: list[str] = []
    for writer in writers:
        entered = {
            name
            for _node, calls in _with_entries(inspect.getsource(writer))
            for name in _names(calls)
        }
        if not _LOCK_ENTRIES.intersection(entered):
            missing.append(writer.__name__)

    assert missing == []


def test_mutating_cli_surfaces_use_ordered_lock_composition() -> None:
    """Closed inventory: every mutation entrypoint takes its locks through one API.

    A journaled writer names its journal in ``operations.transaction``, which
    refuses and rolls back inside the locks. The explicit forms are the
    lock-only writers and ``snapshot restore``, whose rollback is entered
    directly after its lock helper.
    """
    from setforge.cli import (
        cleanup,
        config,
        init,
        install,
        lock,
        migrate,
        orphans,
        revert,
        snapshot,
        stage,
        sync,
        upgrade,
        validate,
    )

    journaled: dict[Callable[..., object], str] = {
        install.install: "install",
        sync.sync: "sync",
        stage._apply: "stage",
        cleanup._apply_cleanup: "cleanup",
        orphans._apply_orphan_cleanup: "cleanup-orphans",
        orphans._execute_scan_cleanup: "cleanup-orphans",
        revert._apply_confirmed_reverts: "revert",
    }
    refusing = (lock.lock, migrate.migrate, snapshot.snapshot_create)
    lock_only = (
        config._run_add,
        config.config_remove,
        init.init,
        upgrade.upgrade,
        validate.fetch,
    )

    def transactions(source: str) -> list[str | None]:
        return [
            next(
                (
                    ast.unparse(keyword.value)
                    for keyword in calls[0].keywords
                    if keyword.arg == "recover"
                ),
                None,
            )
            for _node, calls in _with_entries(source)
            if _names(calls) == ["operations.transaction"]
        ]

    for writer, command in journaled.items():
        recovered = transactions(inspect.getsource(writer))
        assert f"(profile, '{command}')" in recovered, writer.__name__
    for writer in refusing:
        assert None in transactions(inspect.getsource(writer)), writer.__name__
    for writer in lock_only:
        assert ["mutation_locks"] in [
            _names(calls) for _node, calls in _with_entries(inspect.getsource(writer))
        ], writer.__name__

    rollbacks: list[tuple[str, list[str]]] = []
    for path in sorted(Path(install.__file__).parent.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert "refuse_pending" not in source, path.name
        for _node, calls in _with_entries(source):
            names = _names(calls)
            if "operations.recover_on_error" in names:
                rollbacks.append((path.name, names))
            elif _LOCK_ENTRIES.intersection(names):
                assert len(names) == 1, f"{path.name}: {names}"
        assert source.count("recover_on_error(") == int(path.name == "snapshot.py")
    assert rollbacks == [
        ("snapshot.py", ["snap_mod.restore_locks", "operations.recover_on_error"])
    ]
    assert ["mutation_locks"] in [
        _names(calls)
        for _node, calls in _with_entries(
            inspect.getsource(snapshot.snap_mod.restore_locks)
        )
    ]


def _parked_in_locking(thread: threading.Thread) -> bool:
    """Whether ``thread`` is inside the locking module, waiting for the gate."""
    frame = sys._current_frames().get(thread.ident or 0)
    return frame is not None and frame.f_code.co_filename == locking.__file__


@contextmanager
def _cli_waiting_for_the_lock(*args: str) -> Iterator[None]:
    """Run ``setforge *args`` on a thread while this thread holds the writer lock.

    The body runs once the command is parked in the locking module, so it has
    already done everything it does before taking the lock. Leaving the body
    releases the lock; the command must then finish with exit code 0.
    """
    results: list[Result] = []
    thread = threading.Thread(
        target=lambda: results.append(CliRunner().invoke(app, list(args))), daemon=True
    )
    try:
        with mutation_locks():
            thread.start()
            deadline = time.monotonic() + 10
            while not _parked_in_locking(thread):
                assert thread.is_alive(), "the command finished without waiting"
                assert time.monotonic() < deadline, "the command never reached the lock"
                time.sleep(0.005)
            yield
    finally:
        thread.join(timeout=30)
    assert not thread.is_alive(), "the command is stuck after the lock was released"
    assert results, "the command raised instead of exiting"
    assert results[0].exit_code == 0, results[0].output


_RECONCILE_CONFIG = """\
schema_version: '6.4'
minimum_version: '6.4'
tracked_files:
  d: {{src: x, dst: {dst}}}
marketplaces:
  mp: {{source: github, repo: owner/mp}}
claude_plugins:
  one: {{marketplace: mp}}
  two: {{marketplace: mp}}
packages:
  ext-one: {{type: extension, extension: pub.one}}
  ext-two: {{type: extension, extension: pub.two}}
  one: {{type: plugin, plugin: one}}
  two: {{type: plugin, plugin: two}}
profiles:
  p:
    tracked_files: [d]
    packages: {packages}
    reconcile:
      extensions: {{policy: prune}}
      plugins: {{policy: prune}}
"""


def _write_profile(tmp_path: Path, packages: list[str]) -> Path:
    """Write a config whose profile ``p`` declares ``packages``."""
    (tmp_path / "tracked").mkdir(exist_ok=True)
    (tmp_path / "tracked" / "x").write_text("data\n", encoding="utf-8")
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(
        _RECONCILE_CONFIG.format(dst=tmp_path / "live", packages=packages),
        encoding="utf-8",
    )
    return cfg


def test_extension_prune_reads_the_profile_as_it_is_after_the_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A waiting PRUNE must not uninstall what the lock holder just declared."""
    cfg = _write_profile(tmp_path, ["ext-one"])
    installed = {"pub.one", "pub.two"}

    def fake_code(
        args: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if args[1] == "--list-extensions":
            return subprocess.CompletedProcess(args, 0, "\n".join(installed) + "\n", "")
        if args[1] == "--uninstall-extension":
            installed.discard(args[2])
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)

    monkeypatch.setattr(
        "setforge.vscode_extensions.resolve_binary", lambda _: Path("/usr/bin/code")
    )
    monkeypatch.setattr("setforge.vscode_extensions.subprocess.run", fake_code)

    with _cli_waiting_for_the_lock(
        "ext", "reconcile", "--profile=p", f"--config={cfg}"
    ):
        assert installed == {"pub.one", "pub.two"}
        _write_profile(tmp_path, ["ext-one", "ext-two"])

    assert installed == {"pub.one", "pub.two"}


def test_plugin_prune_reads_the_profile_as_it_is_after_the_wait(
    tmp_path: Path, fake_claude: Callable[..., FakeClaude]
) -> None:
    """A waiting plugin PRUNE must not disable what the lock holder declared."""
    cfg = _write_profile(tmp_path, ["one"])
    claude = fake_claude(
        marketplaces=[{"name": "mp", "source": "owner/mp"}],
        plugins=[
            {"id": "one@mp", "enabled": True, "scope": "user"},
            {"id": "two@mp", "enabled": True, "scope": "user"},
        ],
    )

    with _cli_waiting_for_the_lock(
        "plugin", "reconcile", "--profile=p", f"--config={cfg}", "--yes"
    ):
        assert claude.disable_args() == []
        _write_profile(tmp_path, ["one", "two"])

    assert claude.disable_args() == []
    assert all(plugin["enabled"] for plugin in claude.installed_state().values())


def test_extension_remove_waits_for_the_lock_and_keeps_edits_made_meanwhile(
    tmp_path: Path,
) -> None:
    cfg = _write_profile(tmp_path, ["ext-one"])
    before = cfg.read_text(encoding="utf-8")

    with _cli_waiting_for_the_lock(
        "ext", "remove", "pub.one", "--profile=p", f"--config={cfg}", "--exclude"
    ):
        assert cfg.read_text(encoding="utf-8") == before
        cfg.write_text(before + "# edited while the command waited\n", encoding="utf-8")

    assert "# edited while the command waited" in cfg.read_text(encoding="utf-8")
    assert load_yaml(cfg)["profiles"]["p"]["packages"] == []


def test_plugin_remove_disables_the_id_the_config_names_after_the_wait(
    tmp_path: Path, fake_claude: Callable[..., FakeClaude]
) -> None:
    cfg = _write_profile(tmp_path, ["one"])
    claude = fake_claude(
        marketplaces=[{"name": "mp", "source": "owner/mp"}],
        plugins=[{"id": "one@mp", "enabled": True, "scope": "user"}],
    )

    with _cli_waiting_for_the_lock(
        "plugin", "remove", "one", "--disable", "--profile=p", f"--config={cfg}"
    ):
        assert claude.disable_args() == []
        cfg.write_text(
            cfg.read_text(encoding="utf-8").replace(
                "one: {marketplace: mp}", "one: {marketplace: moved}"
            ),
            encoding="utf-8",
        )

    assert claude.disable_args() == ["one@moved"]


def test_sync_cache_clones_the_marketplaces_declared_after_the_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_git: Callable[..., FakeGit]
) -> None:
    _local_clone_yaml(tmp_path, monkeypatch)
    git = fake_git(known_repos={"owner/mp", "owner/moved"})
    cfg = _write_profile(tmp_path, ["one"])

    with _cli_waiting_for_the_lock(
        "plugin", "sync-cache", "--profile=p", f"--config={cfg}"
    ):
        assert git.cloned == {}
        cfg.write_text(
            cfg.read_text(encoding="utf-8").replace("owner/mp", "owner/moved"),
            encoding="utf-8",
        )

    assert set(git.cloned.values()) == {"https://github.com/owner/moved"}


def _held_elsewhere(lock_path: Path) -> bool:
    """Return whether another open file description holds ``lock_path``."""
    with lock_path.open("a") as probe:
        try:
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
        return False


def test_mutation_locks_hold_gate_and_profile_for_the_whole_body(
    tmp_path: Path,
) -> None:
    gate = Path.home() / ".cache/setforge/locks/mutation-gate.lock"
    config_dir = tmp_path / "config-repo"
    config_dir.mkdir()
    target = tmp_path / "target"

    with mutation_locks(
        resources=True, config_dir=config_dir, target_roots=(target,), profile="p"
    ):
        assert _held_elsewhere(gate)
        assert _held_elsewhere(_profile_lock_path("p"))
        require_resources_lock()

    assert not _held_elsewhere(gate)
    assert not _held_elsewhere(_profile_lock_path("p"))
    assert list(config_dir.iterdir()) == []
    assert not target.exists()
    with pytest.raises(SetforgeError, match="global resource lock"):
        require_resources_lock()


def test_resource_mutation_requires_the_resources_scope() -> None:
    with (
        mutation_locks(profile="p"),
        pytest.raises(SetforgeError, match="global resource lock"),
    ):
        require_resources_lock()


def test_mutation_waiting_on_the_gate_holds_no_profile_lock() -> None:
    """The gate is acquired first, so a blocked writer cannot starve readers."""
    gate = Path.home() / ".cache/setforge/locks/mutation-gate.lock"
    gate.parent.mkdir(parents=True, exist_ok=True)
    holder = gate.open("a")
    try:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        with (
            pytest.raises(SetforgeError, match="global mutation gate"),
            mutation_locks(profile="p", timeout=0.01),
        ):
            pytest.fail("mutation ran without the gate")
        with profile_lock("p", timeout=0.01):
            pass
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()


def test_direct_lock_order_inversion_refuses() -> None:
    with (
        profile_lock("p"),
        pytest.raises(SetforgeError, match="inverted lock order"),
        mutation_locks(),
    ):
        pass


def test_mutation_locks_hold_every_requested_profile_once() -> None:
    """Unordered and repeated profile names are accepted and each is locked."""
    with mutation_locks(profiles=("z", "a", "z")):
        assert _held_elsewhere(_profile_lock_path("a"))
        assert _held_elsewhere(_profile_lock_path("z"))

    assert not _held_elsewhere(_profile_lock_path("a"))
    assert not _held_elsewhere(_profile_lock_path("z"))


def test_duplicate_rank_refuses_before_self_deadlock(tmp_path: Path) -> None:
    with (
        profile_lock("p"),
        pytest.raises(SetforgeError, match="duplicate or inverted"),
        profile_lock("p"),
    ):
        pass
