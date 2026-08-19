"""Tests for SetForge's profile, lockfile, and global resource locks."""

import ast
import fcntl
import inspect
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import TypedDict

import pytest

from setforge.errors import SetforgeError
from setforge.locking import (
    _profile_lock_path,
    install_resources_lock,
    lockfile_lock,
    mutation_locks,
    profile_lock,
)
from setforge.transitions import state_root


class _MutationLockKwargs(TypedDict, total=False):
    resources: bool
    config_dir: Path
    config_dirs: tuple[Path, ...]
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


def test_install_resources_lock_serializes_cross_profile_resources(
    state_dir: Path,
) -> None:
    lock_path = Path.home() / ".cache/setforge/locks/install-resources.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = lock_path.open("a")
    try:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        with (
            pytest.raises(SetforgeError, match="global resource lock"),
            install_resources_lock(timeout=0.01),
        ):
            pass
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()


def test_global_resource_lock_ignores_transition_state_override(
    state_dir: Path,
) -> None:
    with install_resources_lock():
        lock_path = Path.home() / ".cache/setforge/locks/install-resources.lock"
        assert lock_path.exists()
        assert lock_path.parent != state_dir / "locks"


@pytest.mark.parametrize(
    "lock_kwargs",
    [
        {"config_dir": Path("config-a")},
        {"profile": "profile-b"},
    ],
)
def test_global_mutation_gate_serializes_prepublication_across_scopes(
    lock_kwargs: _MutationLockKwargs,
) -> None:
    """Different mutation scopes cannot both pass refusal before publishing."""
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
    ("lock_kwargs", "journal_resources"),
    [
        ({"resources": True, "profile": "other"}, True),
        ({"config_dir": Path("cfg")}, False),
    ],
)
def test_mutation_locks_refuse_cross_profile_active_journal(
    lock_kwargs: _MutationLockKwargs,
    journal_resources: bool,
    tmp_path: Path,
) -> None:
    from setforge import operations

    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    if "config_dir" in lock_kwargs:
        lock_kwargs["config_dir"] = config_dir
    journal = operations.prepare(
        command="install",
        profile="first",
        config_dir=config_dir,
        resources_lock=journal_resources,
        command_line=("install",),
        paths=(),
    )

    with (
        pytest.raises(SetforgeError, match="unfinished install"),
        mutation_locks(**lock_kwargs),
    ):
        pass

    operations.complete(journal)


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
        revert.revert,
        revert._revert_to_before,
    )
    missing: list[str] = []
    for writer in writers:
        tree = ast.parse(inspect.getsource(writer))
        calls = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        if not {"install_resources_lock", "mutation_locks"}.intersection(calls):
            missing.append(writer.__name__)

    assert missing == []


def test_mutating_cli_surfaces_use_ordered_lock_composition() -> None:
    """Closed inventory: mutation entrypoints declare scopes through one API."""
    from setforge.cli import (
        cleanup,
        config,
        init,
        install,
        lock,
        migrate,
        orphans,
        snapshot,
        stage,
        sync,
        upgrade,
        validate,
    )

    writers = (
        install.install,
        lock.lock,
        sync.capture,
        migrate.migrate,
        snapshot.snapshot_create,
        snapshot.snapshot_restore,
        stage._apply,
        stage._apply_structured,
        cleanup._apply_cleanup,
        orphans._apply_orphan_cleanup,
        config._run_add,
        config.config_remove,
        init.init,
        upgrade.upgrade,
        validate.fetch,
    )
    missing = []
    for writer in writers:
        calls = {
            node.func.id
            for node in ast.walk(ast.parse(inspect.getsource(writer)))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        if "mutation_locks" not in calls:
            missing.append(writer.__name__)
    assert missing == []


def test_live_reconcile_reloads_desired_state_inside_global_lock() -> None:
    """Extension/plugin PRUNE state is refreshed inside serialization."""
    from setforge.cli import ext, plugins

    for writer in (
        ext._run_ext_reconcile,
        plugins._run_plugin_reconcile,
        plugins.plugin_remove,
        plugins.sync_cache,
    ):
        tree = ast.parse(inspect.getsource(writer))
        locked_loads: list[ast.Call] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.With):
                continue
            lock_calls = {
                call.func.id
                for item in node.items
                for call in ast.walk(item.context_expr)
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
            }
            if not {"install_resources_lock", "mutation_locks"}.intersection(
                lock_calls
            ):
                continue
            locked_loads.extend(
                call
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "load_config"
            )
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
        monkeypatch.setattr(ext, "mutation_locks", recording_lock)
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
        monkeypatch.setattr(plugins, "mutation_locks", recording_lock)
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
    monkeypatch.setattr(ext, "mutation_locks", recording_lock)
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
    monkeypatch.setattr(plugins, "mutation_locks", recording_lock)
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
    monkeypatch.setattr(plugins, "mutation_locks", recording_lock)
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


def test_lockfile_lock_keeps_sidecar_out_of_config_repo(
    state_dir: Path, tmp_path: Path
) -> None:
    config_dir = tmp_path / "config-repo"
    config_dir.mkdir()

    with lockfile_lock(config_dir):
        assert list((Path.home() / ".cache/setforge/locks").glob("config-*.lock"))

    assert not (config_dir / "setforge.lock.lock").exists()
    assert not list((state_dir / "locks").glob("config-*.lock"))


def test_lockfile_lock_ignores_transition_state_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config-repo"
    config_dir.mkdir()
    lock_dir = Path.home() / ".cache/setforge/locks"
    with lockfile_lock(config_dir):
        lock_path = next(lock_dir.glob("config-*.lock"))
    holder = lock_path.open("a")
    try:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "alternate-state"))
        with (
            pytest.raises(SetforgeError, match=r"setforge\.lock"),
            lockfile_lock(config_dir, timeout=0.01),
        ):
            pass
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()


def test_mutation_locks_acquire_canonical_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    @contextmanager
    def recording(name: str):
        events.append(f"enter:{name}")
        try:
            yield
        finally:
            events.append(f"exit:{name}")

    monkeypatch.setattr(
        "setforge.locking._mutation_gate_lock",
        lambda timeout=None: recording("mutation"),
    )
    monkeypatch.setattr(
        "setforge.locking.install_resources_lock",
        lambda timeout=None: recording("resources"),
    )
    monkeypatch.setattr(
        "setforge.locking.lockfile_lock",
        lambda config_dir, timeout=None: recording("config"),
    )
    monkeypatch.setattr(
        "setforge.locking.profile_lock",
        lambda profile, timeout=None: recording("profile"),
    )

    with mutation_locks(resources=True, config_dir=tmp_path, profile="p"):
        events.append("body")

    assert events == [
        "enter:mutation",
        "enter:resources",
        "enter:config",
        "enter:profile",
        "body",
        "exit:profile",
        "exit:config",
        "exit:resources",
        "exit:mutation",
    ]


def test_mutation_locks_acquire_multiple_config_dirs_in_sorted_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acquired: list[Path] = []

    @contextmanager
    def recording_config(config_dir: Path, timeout: float | None = None):
        del timeout
        acquired.append(config_dir)
        yield

    monkeypatch.setattr("setforge.locking.lockfile_lock", recording_config)

    with mutation_locks(config_dirs=(tmp_path / "z", tmp_path / "a", tmp_path / "z")):
        pass

    assert acquired == [(tmp_path / "a").resolve(), (tmp_path / "z").resolve()]


def test_direct_lock_order_inversion_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("setforge.locking.state_root", lambda: tmp_path / "state")
    with (
        profile_lock("p"),
        pytest.raises(SetforgeError, match="inverted lock order"),
        lockfile_lock(tmp_path),
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
