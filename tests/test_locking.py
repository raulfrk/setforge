"""Tests for SetForge's profile, lockfile, and global resource locks."""

import ast
import fcntl
import inspect
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from setforge.errors import SetforgeError
from setforge.locking import install_resources_lock, lockfile_lock, profile_lock
from setforge.transitions import state_root


@pytest.fixture(autouse=True)
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    return tmp_path


def test_lock_creates_lockfile_and_runs_body() -> None:
    """profile_lock("p") creates state_root()/locks/p.lock and the body runs."""
    executed: list[bool] = []
    with profile_lock("p"):
        lock_path = state_root() / "locks" / "p.lock"
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
    lock_path = state_root() / "locks" / "p.lock"
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
    lock_path = state_dir / "locks" / "p.lock"
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
    locks_dir = state_dir / "locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    lock_a = locks_dir / "a.lock"
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
        if "install_resources_lock" not in calls:
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
            if "install_resources_lock" not in lock_calls:
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
    def recording_lock():
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
        monkeypatch.setattr(ext, "install_resources_lock", recording_lock)
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
        monkeypatch.setattr(plugins, "install_resources_lock", recording_lock)
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
    def recording_lock():
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
    monkeypatch.setattr(ext, "install_resources_lock", recording_lock)
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
    def recording_lock():
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
    monkeypatch.setattr(plugins, "install_resources_lock", recording_lock)
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
    def recording_lock():
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
    monkeypatch.setattr(plugins, "install_resources_lock", recording_lock)
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
    with lockfile_lock(config_dir):
        monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "alternate-state"))
        with (
            pytest.raises(SetforgeError, match=r"setforge\.lock"),
            lockfile_lock(config_dir, timeout=0.01),
        ):
            pass
