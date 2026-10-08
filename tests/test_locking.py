"""Tests for SetForge's mutation gate, profile lock, and target guards."""

import ast
import errno
import fcntl
import inspect
import os
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import TypedDict

import pytest

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
    refusing = (lock.lock, sync.capture, migrate.migrate, snapshot.snapshot_create)
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


def test_live_reconcile_reloads_desired_state_inside_global_lock() -> None:
    """Extension/plugin PRUNE state is refreshed inside serialization."""
    from setforge.cli import ext, plugins

    for writer in (
        ext._run_ext_reconcile,
        plugins._run_plugin_reconcile,
        plugins.plugin_remove,
        plugins.sync_cache,
    ):
        locked_loads = [
            call
            for node, calls in _with_entries(inspect.getsource(writer))
            if _LOCK_ENTRIES.intersection(_names(calls))
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "load_config"
        ]
        assert locked_loads, f"{writer.__name__} does not reload under lock"


@pytest.mark.parametrize("adapter_name", ["ext", "plugin"])
def test_live_reconcile_waits_for_lock_before_reloading(
    adapter_name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The post-wait desired-state load runs while serialization is held."""
    from setforge.cli import ext, plugins
    from setforge.config import Config, Extensions, ReconcilePolicy, ResolvedProfile

    held = False
    loads: list[bool] = []

    @contextmanager
    def recording_lock(**_kwargs: object):
        nonlocal held
        held = True
        try:
            yield
        finally:
            held = False

    cfg = Config.model_construct()
    resolved = ResolvedProfile()

    def locked_load(_path: Path) -> Config:
        loads.append(held)
        return cfg

    if adapter_name == "ext":
        monkeypatch.setattr("setforge.locking.mutation_locks", recording_lock)
        monkeypatch.setattr(ext, "load_config", locked_load)
        monkeypatch.setattr(
            ext,
            "resolve_effective_profile",
            lambda *_args: SimpleNamespace(resolved=resolved),
        )
        monkeypatch.setattr(
            ext.vscode_extensions,
            "reconcile",
            lambda *_args, **_kwargs: object(),
        )
        ext._run_ext_reconcile(
            tmp_path / "setforge.yaml",
            "p",
            tmp_path,
            Extensions(reconcile=ReconcilePolicy.PRUNE),
            dry_run=False,
        )
    else:
        monkeypatch.setattr("setforge.locking.mutation_locks", recording_lock)
        monkeypatch.setattr(plugins, "load_config", locked_load)
        monkeypatch.setattr(
            plugins,
            "resolve_effective_profile",
            lambda *_args: SimpleNamespace(resolved=resolved),
        )
        monkeypatch.setattr(
            plugins.reconcile_adapter, "plugin_ids", lambda *_args: set()
        )
        monkeypatch.setattr(
            plugins.claude_plugins_mod,
            "reconcile",
            lambda *_args, **_kwargs: object(),
        )
        plugins._run_plugin_reconcile(
            tmp_path / "setforge.yaml",
            "p",
            tmp_path,
            cfg,
            resolved,
            ReconcilePolicy.PRUNE,
            dry_run=False,
            auto=True,
        )

    assert loads == [True]


def test_extension_remove_edits_desired_state_inside_global_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from setforge.cli import ext

    held = False
    edits: list[bool] = []

    @contextmanager
    def recording_lock(**_kwargs: object):
        nonlocal held
        held = True
        try:
            yield
        finally:
            held = False

    def guarded_remove(*_args: object, **_kwargs: object) -> bool:
        edits.append(held)
        return True

    monkeypatch.setattr(ext, "_resolve_config_arg", lambda path: path)
    monkeypatch.setattr("setforge.locking.mutation_locks", recording_lock)
    monkeypatch.setattr(ext.vscode_extensions, "remove_from_include", guarded_remove)

    ext.ext_remove(
        extension_id="pub.ext",
        profile="profile",
        config=tmp_path / "setforge.yaml",
        exclude=True,
    )

    assert edits == [True]


def test_plugin_remove_resolves_disable_id_from_post_wait_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from setforge.cli import plugins

    held = False
    disabled: list[str] = []

    @contextmanager
    def recording_lock(**_kwargs: object):
        nonlocal held
        held = True
        try:
            yield
        finally:
            held = False

    def locked_load(_path: Path):
        assert held
        return SimpleNamespace(
            claude_plugins={"p": SimpleNamespace(marketplace="post-wait")}
        )

    monkeypatch.setattr(plugins, "_resolve_config_arg", lambda path: path)
    monkeypatch.setattr("setforge.locking.mutation_locks", recording_lock)
    monkeypatch.setattr(plugins, "load_config", locked_load)
    monkeypatch.setattr(
        plugins.claude_yaml_editor_mod,
        "yaml_remove_plugin_from_profile",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        plugins.claude_plugins_mod,
        "plugin_disable",
        lambda plugin_id: disabled.append(plugin_id),
    )

    plugins.plugin_remove(
        name="p",
        profile="profile",
        config=tmp_path / "setforge.yaml",
        disable=True,
    )

    assert disabled == ["p@post-wait"]


def test_sync_cache_resolves_marketplaces_after_wait(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from setforge.cli import plugins
    from setforge.config import ClaudeInstallMode

    held = False
    synced: list[object] = []
    cfg = object()
    resolved = object()

    @contextmanager
    def recording_lock(**_kwargs: object):
        nonlocal held
        held = True
        try:
            yield
        finally:
            held = False

    def locked_load(_path: Path):
        assert held
        return cfg

    def locked_sync(seen_cfg: object, seen_resolved: object):
        assert held
        synced.extend((seen_cfg, seen_resolved))
        return []

    monkeypatch.setattr(plugins, "_resolve_config_arg", lambda path: path)
    monkeypatch.setattr("setforge.locking.mutation_locks", recording_lock)
    monkeypatch.setattr(plugins, "load_config", locked_load)
    monkeypatch.setattr(
        plugins.binaries,
        "load_host_local_config",
        lambda: SimpleNamespace(
            claude=SimpleNamespace(install_mode=ClaudeInstallMode.LOCAL_CLONE)
        ),
    )
    monkeypatch.setattr(
        plugins,
        "resolve_effective_profile",
        lambda *_args: SimpleNamespace(resolved=resolved),
    )
    monkeypatch.setattr(
        plugins.claude_mp_cache_mod, "sync_marketplace_cache", locked_sync
    )

    plugins.sync_cache(profile="profile", config=tmp_path / "setforge.yaml")

    assert synced == [cfg, resolved]


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


def test_mutation_locks_acquire_multiple_profiles_in_sorted_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    acquired: list[str] = []

    @contextmanager
    def recording_profile(profile: str, timeout: float | None = None):
        del timeout
        acquired.append(profile)
        yield

    monkeypatch.setattr("setforge.locking.profile_lock", recording_profile)

    with mutation_locks(profiles=("z", "a", "z")):
        pass

    assert acquired == ["a", "z"]


def test_duplicate_rank_refuses_before_self_deadlock(tmp_path: Path) -> None:
    with (
        profile_lock("p"),
        pytest.raises(SetforgeError, match="duplicate or inverted"),
        profile_lock("p"),
    ):
        pass
