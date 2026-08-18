"""Unit and recovery-contract tests for write-ahead operation journals."""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from setforge import operations, transitions
from setforge.errors import SetforgeError


@pytest.fixture
def operation_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "state"
    monkeypatch.setattr(transitions, "state_root", lambda: root)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return root


def _prepare(
    tmp_path: Path, *, paths: tuple[Path, ...] = ()
) -> operations.OperationJournal:
    return operations.prepare(
        command="install",
        profile="p",
        config_dir=tmp_path,
        resources_lock=True,
        command_line=("install", "--profile=p"),
        paths=paths,
    )


def test_prepare_round_trips_exact_path_and_store_state(
    tmp_path: Path, operation_state: Path
) -> None:
    file_path = tmp_path / "live.txt"
    file_path.write_bytes(b"before\x00\n")
    file_path.chmod(0o640)
    link_path = tmp_path / "link"
    link_path.symlink_to("live.txt")
    directory = tmp_path / "directory"
    directory.mkdir(mode=0o750)
    absent = tmp_path / "absent"
    state = transitions.StateSnapshotEntry(
        store=transitions.SnapshotStore.BASE,
        profile="p",
        key="doc",
        payload=b"base\x00",
    )
    journal = operations.prepare(
        command="install",
        profile="p",
        config_dir=tmp_path,
        resources_lock=True,
        command_line=("install", "--profile=p"),
        paths=(file_path, link_path, directory, absent),
        state_snapshots=(state,),
    )

    assert operations.load("p") == journal
    assert operations.journal_path("p").stat().st_mode & 0o777 == 0o600
    assert tmp_path / "home" / ".cache" / "setforge" / "operations" in (
        operations.journal_path("p").parents
    )


@settings(
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    payload=st.binary(max_size=4096),
    mode=st.sampled_from([0o600, 0o640, 0o755]),
)
def test_journal_round_trips_arbitrary_binary_file_payload(
    tmp_path: Path,
    operation_state: Path,
    payload: bytes,
    mode: int,
) -> None:
    path = tmp_path / "payload"
    path.write_bytes(payload)
    path.chmod(mode)

    journal = _prepare(tmp_path, paths=(path,))

    assert operations.load("p") == journal
    assert journal.paths[0].payload == payload
    assert journal.paths[0].mode == mode
    operations.complete(journal)


def test_prepare_refuses_to_shadow_active_operation(
    tmp_path: Path, operation_state: Path
) -> None:
    _prepare(tmp_path)
    with pytest.raises(SetforgeError, match=r"unfinished install operation.*recover"):
        _prepare(tmp_path)


def test_active_operation_blocks_same_config_but_not_other_repo(
    tmp_path: Path, operation_state: Path
) -> None:
    _prepare(tmp_path)

    with pytest.raises(SetforgeError, match="blocks this config mutation"):
        operations.refuse_config_mutation(tmp_path)

    operations.refuse_config_mutation(tmp_path / "other")


def test_checkpoint_intent_is_durable_before_completion(
    tmp_path: Path, operation_state: Path
) -> None:
    journal = _prepare(tmp_path)
    applying = operations.begin_checkpoint(
        journal,
        name="tracked-files",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore captured paths",
    )
    assert operations.load("p") == applying
    assert not applying.checkpoints[-1].completed

    completed = operations.finish_checkpoint(applying)
    assert operations.load("p") == completed
    assert completed.checkpoints[-1].completed


def test_recover_files_restores_file_symlink_directory_and_absence(
    tmp_path: Path, operation_state: Path
) -> None:
    file_path = tmp_path / "file"
    file_path.write_text("before", encoding="utf-8")
    file_path.chmod(0o640)
    target = tmp_path / "target"
    target.write_text("target", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to("target")
    directory = tmp_path / "kept-dir"
    directory.mkdir(mode=0o750)
    directory.chmod(0o750)
    created = tmp_path / "created"
    journal = operations.begin_checkpoint(
        _prepare(tmp_path, paths=(file_path, link, directory, created)),
        name="files",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore files",
    )

    file_path.write_text("after", encoding="utf-8")
    file_path.chmod(0o600)
    link.unlink()
    link.symlink_to("elsewhere")
    directory.chmod(0o700)
    created.write_text("new", encoding="utf-8")

    recovered = operations.recover_files(journal)

    assert file_path.read_text(encoding="utf-8") == "before"
    assert file_path.stat().st_mode & 0o777 == 0o640
    assert link.readlink() == Path("target")
    assert directory.stat().st_mode & 0o777 == 0o750
    assert not created.exists()
    assert recovered.phase is operations.OperationPhase.RECOVERING
    operations.complete(recovered)
    assert operations.active("p") is None


def test_recovery_refuses_nonempty_created_directory(
    tmp_path: Path, operation_state: Path
) -> None:
    created = tmp_path / "created"
    journal = operations.begin_checkpoint(
        _prepare(tmp_path, paths=(created,)),
        name="files",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore files",
    )
    created.mkdir()
    (created / "unknown").write_text("user", encoding="utf-8")

    with pytest.raises(SetforgeError, match="non-empty recovery directory"):
        operations.recover_files(journal)
    assert operations.active("p") is not None


def test_recovery_restores_mode_zero_exactly(
    tmp_path: Path, operation_state: Path
) -> None:
    path = tmp_path / "private"
    path.write_bytes(b"after")
    operations._restore_path(
        operations.PathSnapshot(
            path=path,
            kind=operations.SnapshotKind.FILE,
            mode=0o000,
            payload=b"before",
        )
    )

    assert path.stat().st_mode & 0o777 == 0o000
    path.chmod(0o600)
    assert path.read_bytes() == b"before"


def test_recovery_removes_missing_parent_directories_created_by_writer(
    tmp_path: Path, operation_state: Path
) -> None:
    parent = tmp_path / "new-parent" / "nested"
    created = parent / "file"
    journal = operations.begin_checkpoint(
        _prepare(tmp_path, paths=(created,)),
        name="files",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore files",
        paths=(created,),
    )
    parent.mkdir(parents=True)
    created.write_text("new", encoding="utf-8")

    operations.recover_files(journal)

    assert not (tmp_path / "new-parent").exists()


def test_snapshot_refuses_atomic_replacement_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "live"
    path.write_text("old", encoding="utf-8")
    replacement = tmp_path / "replacement"
    replacement.write_text("new", encoding="utf-8")
    real_open = operations.os.open

    def replace_then_open(target: Path, flags: int) -> int:
        replacement.replace(path)
        return real_open(target, flags)

    monkeypatch.setattr(operations.os, "open", replace_then_open)

    with pytest.raises(SetforgeError, match="changed while snapshotting"):
        operations.snapshot_path(path)


def test_snapshot_stat_detects_same_size_write_with_restored_mtime(
    tmp_path: Path,
) -> None:
    path = tmp_path / "live"
    path.write_bytes(b"old")
    before = path.stat()
    time.sleep(0.01)
    path.write_bytes(b"new")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

    assert not operations._same_snapshot_stat(before, path.stat())


def test_prepare_refuses_ancestor_topology_change_during_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "new-parent" / "file"
    calls = 0
    real_inventory = operations._paths_with_missing_ancestors

    def changing_inventory(paths: tuple[Path, ...]) -> tuple[Path, ...]:
        nonlocal calls
        calls += 1
        result = real_inventory(paths)
        if calls == 2:
            return (*result, tmp_path / "newly-missing-parent")
        return result

    monkeypatch.setattr(operations, "_paths_with_missing_ancestors", changing_inventory)

    with pytest.raises(SetforgeError, match="ancestor topology changed"):
        _prepare(tmp_path, paths=(path,))


def test_recovery_ignores_snapshots_for_checkpoints_that_never_began(
    tmp_path: Path, operation_state: Path
) -> None:
    untouched = tmp_path / "later"
    untouched.write_text("baseline", encoding="utf-8")
    journal = _prepare(tmp_path, paths=(untouched,))
    untouched.write_text("user change", encoding="utf-8")

    operations.recover_files(journal)

    assert untouched.read_text(encoding="utf-8") == "user change"


def test_recovery_removes_transition_committed_after_prepare(
    tmp_path: Path, operation_state: Path
) -> None:
    journal = operations.begin_checkpoint(
        _prepare(tmp_path),
        name="transition",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="remove transition",
    )
    transition = transitions.write_transition(
        transitions.make_meta(transitions.TransitionCommand.REVERT, "p"),
        {},
        {},
        None,
    )
    other = transitions.write_transition(
        transitions.make_meta(transitions.TransitionCommand.REVERT, "other"),
        {},
        {},
        None,
    )

    operations.recover_files(journal)

    assert not transition.exists()
    assert other.exists()


def test_recover_on_error_restores_and_preserves_primary_exception(
    tmp_path: Path, operation_state: Path
) -> None:
    path = tmp_path / "live"
    path.write_text("before", encoding="utf-8")

    def fail_during_apply() -> None:
        with operations.recover_on_error("p", "install"):
            journal = _prepare(tmp_path, paths=(path,))
            operations.begin_checkpoint(
                journal,
                name="files",
                kind=operations.CheckpointKind.REVERSIBLE,
                recovery="restore files",
            )
            path.write_text("after", encoding="utf-8")
            raise RuntimeError("apply failed")

    with pytest.raises(RuntimeError, match="apply failed"):
        fail_during_apply()

    assert path.read_text(encoding="utf-8") == "before"
    assert operations.active("p") is None


def test_recover_on_error_preserves_primary_when_recovery_subprocess_fails(
    tmp_path: Path,
    operation_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _prepare(tmp_path)
    operations.begin_checkpoint(
        journal,
        name="adapter",
        kind=operations.CheckpointKind.COMPENSATABLE,
        recovery="restore adapter",
    )
    monkeypatch.setattr(
        operations,
        "recover_adapters",
        lambda _journal: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, ["tool"])
        ),
    )

    with (
        pytest.raises(RuntimeError, match="primary") as caught,
        operations.recover_on_error("p", "install"),
    ):
        raise RuntimeError("primary")

    assert any("automatic recovery failed" in note for note in caught.value.__notes__)
    assert operations.active("p") is not None


def test_recover_on_error_preserves_primary_when_journal_load_fails(
    tmp_path: Path,
    operation_state: Path,
) -> None:
    _prepare(tmp_path)
    operations.journal_path("p").write_text("not-json", encoding="utf-8")

    with (
        pytest.raises(RuntimeError, match="primary") as caught,
        operations.recover_on_error("p", "install"),
    ):
        raise RuntimeError("primary")

    assert any("automatic recovery failed" in note for note in caught.value.__notes__)


def test_adapter_recovery_restores_extension_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from setforge import vscode_extensions

    installed = {"extra.ext"}
    monkeypatch.setattr(vscode_extensions, "list_installed", lambda: set(installed))
    monkeypatch.setattr(vscode_extensions, "install_one", installed.add)
    monkeypatch.setattr(vscode_extensions, "uninstall_one", installed.remove)
    journal = operations.OperationJournal(
        operation_id="op",
        command="install",
        profile="p",
        config_dir=None,
        state_dir=transitions.state_root().resolve(),
        resources_lock=True,
        phase=operations.OperationPhase.APPLYING,
        created_at="2026-01-01T00:00:00+00:00",
        command_line=(),
        paths=(),
        state_snapshots=(),
        adapters=(
            operations.AdapterSnapshot(
                operations.AdapterKind.EXTENSIONS, '["expected.ext"]'
            ),
        ),
        checkpoints=(
            operations.OperationCheckpoint(
                "extensions",
                operations.CheckpointKind.COMPENSATABLE,
                "restore extensions",
                adapters=(operations.AdapterKind.EXTENSIONS,),
            ),
        ),
    )

    operations.recover_adapters(journal)

    assert installed == {"expected.ext"}


def test_adapter_recovery_restores_mcp_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from setforge import mcp_servers
    from setforge.config import McpScope

    current: dict[str, tuple[tuple[str, ...], McpScope]] = {
        "server": (("new",), McpScope.USER)
    }
    monkeypatch.setattr(mcp_servers, "mcp_get_command", current.get)
    monkeypatch.setattr(
        mcp_servers, "mcp_remove", lambda name, **_kwargs: current.pop(name)
    )
    monkeypatch.setattr(
        mcp_servers,
        "mcp_add",
        lambda name, ref: current.__setitem__(name, (tuple(ref.command), ref.scope)),
    )
    journal = operations.OperationJournal(
        operation_id="op",
        command="install",
        profile="p",
        config_dir=None,
        state_dir=transitions.state_root().resolve(),
        resources_lock=True,
        phase=operations.OperationPhase.APPLYING,
        created_at="2026-01-01T00:00:00+00:00",
        command_line=(),
        paths=(),
        state_snapshots=(),
        adapters=(
            operations.AdapterSnapshot(
                operations.AdapterKind.MCP,
                '[{"name":"server","prior":[["old","--flag"],"user"]}]',
            ),
        ),
        checkpoints=(
            operations.OperationCheckpoint(
                "mcp",
                operations.CheckpointKind.COMPENSATABLE,
                "restore MCP",
                adapters=(operations.AdapterKind.MCP,),
            ),
        ),
    )

    operations.recover_adapters(journal)

    assert current == {"server": (("old", "--flag"), McpScope.USER)}


def test_plugin_recovery_respects_dependencies_and_replaces_drifted_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from setforge import claude_plugins

    events: list[str] = []
    marketplaces: dict[str, dict[str, object]] = {
        "extra": {"source": "path:/x"},
        "expected": {"source": "github:wrong/repo"},
    }
    plugins: dict[str, dict[str, object]] = {
        "extra-tool@extra": {"enabled": True},
        "tool@expected": {"enabled": True},
    }
    add_attempts = 0
    monkeypatch.setattr(claude_plugins, "list_marketplaces", lambda: dict(marketplaces))
    monkeypatch.setattr(claude_plugins, "list_installed", lambda: dict(plugins))

    def remove_marketplace(name: str) -> None:
        assert not any(plugin.endswith(f"@{name}") for plugin in plugins)
        events.append(f"remove-marketplace:{name}")
        marketplaces.pop(name)

    def add_marketplace(name: str, source: object) -> None:
        nonlocal add_attempts
        add_attempts += 1
        events.append(f"add-marketplace:{name}")
        if add_attempts == 1:
            raise RuntimeError("interrupted marketplace replacement")
        marketplaces[name] = {"source": "github:owner/repo", "model": source}

    def install_plugin(name: str, marketplace: str) -> None:
        assert marketplace in marketplaces
        events.append(f"install:{name}@{marketplace}")
        plugins[f"{name}@{marketplace}"] = {"enabled": True}

    def uninstall_plugin(plugin_id: str) -> None:
        events.append(f"uninstall:{plugin_id}")
        plugins.pop(plugin_id)

    monkeypatch.setattr(claude_plugins, "marketplace_remove", remove_marketplace)
    monkeypatch.setattr(claude_plugins, "marketplace_add", add_marketplace)
    monkeypatch.setattr(claude_plugins, "plugin_install", install_plugin)
    monkeypatch.setattr(claude_plugins, "plugin_uninstall", uninstall_plugin)
    monkeypatch.setattr(claude_plugins, "plugin_enable", lambda _name: None)
    monkeypatch.setattr(claude_plugins, "plugin_disable", lambda _name: None)
    journal = operations.OperationJournal(
        operation_id="op",
        command="install",
        profile="p",
        config_dir=None,
        state_dir=transitions.state_root().resolve(),
        resources_lock=True,
        phase=operations.OperationPhase.APPLYING,
        created_at="2026-01-01T00:00:00+00:00",
        command_line=(),
        paths=(),
        state_snapshots=(),
        adapters=(
            operations.AdapterSnapshot(
                operations.AdapterKind.PLUGINS,
                json.dumps(
                    {
                        "marketplaces": {"expected": {"source": "github:owner/repo"}},
                        "plugins": {"tool@expected": {"enabled": True}},
                    }
                ),
            ),
        ),
        checkpoints=(
            operations.OperationCheckpoint(
                "plugins",
                operations.CheckpointKind.COMPENSATABLE,
                "restore plugins",
                adapters=(operations.AdapterKind.PLUGINS,),
            ),
        ),
    )

    with pytest.raises(RuntimeError, match="interrupted marketplace"):
        operations.recover_adapters(journal)
    operations.recover_adapters(journal)

    assert events == [
        "uninstall:extra-tool@extra",
        "uninstall:tool@expected",
        "remove-marketplace:expected",
        "remove-marketplace:extra",
        "add-marketplace:expected",
        "add-marketplace:expected",
        "install:tool@expected",
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda raw: raw.update(schema_version=999),
        lambda raw: raw.update(profile="other"),
        lambda raw: raw.update(paths="not-a-list"),
    ],
)
def test_load_fails_closed_on_invalid_journal(
    tmp_path: Path,
    operation_state: Path,
    mutation: Callable[[dict[str, object]], None],
) -> None:
    _prepare(tmp_path)
    path = operations.journal_path("p")
    raw = json.loads(path.read_text(encoding="utf-8"))
    mutation(raw)
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(SetforgeError, match="operation journal"):
        operations.load("p")


def _set_nested(
    raw: dict[str, object], section: str, index: int, key: str, value: object
) -> None:
    raw[section][index][key] = value  # type: ignore[index]


def _duplicate_row(raw: dict[str, object], section: str) -> None:
    rows = raw[section]
    rows.append(deepcopy(rows[0]))  # type: ignore[attr-defined,index]


def _duplicate_state_alias(raw: dict[str, object]) -> None:
    rows = raw["state_snapshots"]
    alias = deepcopy(rows[0])  # type: ignore[index]
    alias["key"] = "./file"
    rows.append(alias)  # type: ignore[attr-defined]


def _make_path_noncanonical(raw: dict[str, object], key: str) -> None:
    value = raw[key]
    assert isinstance(value, str)
    raw[key] = f"{value}/child/.."


@pytest.mark.parametrize(
    "mutation",
    [
        lambda raw: _set_nested(raw, "paths", 0, "path", "relative"),
        lambda raw: _set_nested(raw, "paths", 0, "mode", True),
        lambda raw: _set_nested(raw, "paths", 0, "mode", 0o10000),
        lambda raw: _set_nested(raw, "paths", 0, "payload", None),
        lambda raw: _duplicate_row(raw, "paths"),
        lambda raw: _set_nested(raw, "state_snapshots", 0, "store", "unknown"),
        lambda raw: _set_nested(raw, "state_snapshots", 0, "key", "../escape"),
        lambda raw: _set_nested(raw, "state_snapshots", 0, "key", "."),
        lambda raw: _set_nested(raw, "state_snapshots", 0, "profile", "../../escape"),
        lambda raw: _set_nested(raw, "state_snapshots", 0, "profile", "."),
        lambda raw: _duplicate_row(raw, "state_snapshots"),
        _duplicate_state_alias,
        lambda raw: _set_nested(raw, "adapters", 0, "payload_json", "{"),
        lambda raw: _set_nested(raw, "adapters", 0, "payload_json", '[""]'),
        lambda raw: _set_nested(raw, "checkpoints", 0, "paths", ["/unknown"]),
        lambda raw: _set_nested(raw, "checkpoints", 0, "adapters", ["mcp"]),
        lambda raw: _set_nested(raw, "checkpoints", 0, "restore_state", "yes"),
        lambda raw: raw.update(config_dir="relative"),
        lambda raw: raw.update(state_dir="relative"),
        lambda raw: _make_path_noncanonical(raw, "config_dir"),
        lambda raw: _make_path_noncanonical(raw, "state_dir"),
        lambda raw: raw.update(resources_lock="yes"),
        lambda raw: raw.update(resources_lock=False),
        lambda raw: raw.update(reserved_profiles=[]),
        lambda raw: raw.update(reserved_profiles=["p", "p"]),
        lambda raw: raw.update(reserved_profiles=["z", "p"]),
        lambda raw: raw.update(command_line="install"),
        lambda raw: raw.update(schema_version=True),
    ],
)
def test_load_rejects_semantically_invalid_recovery_rows(
    tmp_path: Path,
    operation_state: Path,
    mutation: Callable[[dict[str, object]], None],
) -> None:
    path = tmp_path / "file"
    path.write_text("before", encoding="utf-8")
    state = transitions.StateSnapshotEntry(
        store=transitions.SnapshotStore.BASE,
        profile="p",
        key="file",
        payload=b"base",
    )
    journal = operations.prepare(
        command="install",
        profile="p",
        config_dir=tmp_path,
        resources_lock=True,
        command_line=("install",),
        paths=(path,),
        state_snapshots=(state,),
        adapters=(
            operations.AdapterSnapshot(
                operations.AdapterKind.EXTENSIONS, '["expected.ext"]'
            ),
        ),
    )
    operations.begin_checkpoint(
        journal,
        name="effect",
        kind=operations.CheckpointKind.COMPENSATABLE,
        recovery="restore effect",
    )
    journal_path = operations.journal_path("p")
    raw = json.loads(journal_path.read_text(encoding="utf-8"))
    mutation(raw)
    journal_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(SetforgeError, match="invalid operation journal"):
        operations.load("p")


def test_invalid_later_snapshot_is_rejected_before_any_recovery_effect(
    tmp_path: Path,
    operation_state: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_text("before", encoding="utf-8")
    second.write_text("before", encoding="utf-8")
    journal = operations.begin_checkpoint(
        _prepare(tmp_path, paths=(first, second)),
        name="files",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore files",
    )
    first.write_text("after", encoding="utf-8")
    raw = json.loads(operations.journal_path("p").read_text(encoding="utf-8"))
    raw["paths"][1]["payload"] = "not-base64!"
    operations.journal_path("p").write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(SetforgeError, match="invalid operation journal"):
        operations.load(journal.profile)

    assert first.read_text(encoding="utf-8") == "after"


def test_state_root_mismatch_refuses_before_path_recovery(
    tmp_path: Path,
    operation_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "live"
    path.write_text("before", encoding="utf-8")
    journal = operations.begin_checkpoint(
        _prepare(tmp_path, paths=(path,)),
        name="files",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore files",
    )
    path.write_text("after", encoding="utf-8")
    monkeypatch.setattr(transitions, "state_root", lambda: tmp_path / "other-state")

    with pytest.raises(SetforgeError, match="SETFORGE_STATE_DIR"):
        operations.recover_files(journal)

    assert path.read_text(encoding="utf-8") == "after"


def test_invalid_later_adapter_is_rejected_before_earlier_adapter_calls(
    tmp_path: Path,
    operation_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from setforge import vscode_extensions

    journal = operations.prepare(
        command="install",
        profile="p",
        config_dir=tmp_path,
        resources_lock=True,
        command_line=("install",),
        paths=(),
        adapters=(
            operations.AdapterSnapshot(
                operations.AdapterKind.EXTENSIONS, '["expected.ext"]'
            ),
            operations.AdapterSnapshot(operations.AdapterKind.MCP, "[]"),
        ),
    )
    operations.begin_checkpoint(
        journal,
        name="adapters",
        kind=operations.CheckpointKind.COMPENSATABLE,
        recovery="restore adapters",
    )
    raw = json.loads(operations.journal_path("p").read_text(encoding="utf-8"))
    raw["adapters"][1]["payload_json"] = '[{"name":"bad","prior":[1]}]'
    operations.journal_path("p").write_text(json.dumps(raw), encoding="utf-8")
    calls = 0

    def list_installed() -> set[str]:
        nonlocal calls
        calls += 1
        return set()

    monkeypatch.setattr(vscode_extensions, "list_installed", list_installed)

    with pytest.raises(SetforgeError, match="invalid operation journal"):
        operations.load("p")

    assert calls == 0


def test_cross_profile_state_snapshot_reserves_its_profile_namespace(
    tmp_path: Path, operation_state: Path
) -> None:
    state = transitions.StateSnapshotEntry(
        store=transitions.SnapshotStore.BASE,
        profile="actual",
        key="file",
        payload=b"base",
    )
    journal = operations.prepare(
        command="revert",
        profile="migrate",
        config_dir=tmp_path,
        resources_lock=False,
        command_line=("revert",),
        paths=(),
        state_snapshots=(state,),
    )

    assert operations.locked_profiles(journal) == ("actual", "migrate")
    assert operations.conflicting_journals(
        resources=False,
        config_dir=None,
        profile="actual",
    ) == (journal,)


def test_extra_reserved_profile_survives_reload_and_blocks_mutation(
    tmp_path: Path, operation_state: Path
) -> None:
    journal = operations.prepare(
        command="migrate",
        profile="migrate",
        config_dir=tmp_path,
        resources_lock=False,
        command_line=("migrate",),
        paths=(),
        profiles=("team/dev",),
    )

    loaded = operations.load("migrate")

    assert loaded.reserved_profiles == ("migrate", "team/dev")
    assert operations.locked_profiles(loaded) == ("migrate", "team/dev")
    assert operations.conflicting_journals(
        resources=False,
        config_dir=None,
        profile="team/dev",
    ) == (journal,)


@pytest.mark.parametrize(
    ("kind", "valid_payload", "invalid_payload"),
    [
        (
            operations.AdapterKind.PLUGINS,
            {
                "marketplaces": {"market": {"source": "github:owner/repo"}},
                "plugins": {"tool@market": {"enabled": True}},
            },
            {
                "marketplaces": {"market": {"source": "github:owner/repo"}},
                "plugins": {"@market": {"enabled": True}},
            },
        ),
        (
            operations.AdapterKind.PLUGINS,
            {"marketplaces": {}, "plugins": {}},
            {
                "marketplaces": {"market": {"source": "github:"}},
                "plugins": {},
            },
        ),
        (
            operations.AdapterKind.PLUGINS,
            {
                "marketplaces": {"market": {"source": "github:owner/repo"}},
                "plugins": {"tool@market": {"enabled": True}},
            },
            {
                "marketplaces": {"market": {"source": "github:owner/repo"}},
                "plugins": {"tool@missing": {"enabled": True}},
            },
        ),
        (
            operations.AdapterKind.MCP,
            [{"name": "server", "prior": None}],
            [{"name": "", "prior": None}],
        ),
        (
            operations.AdapterKind.MCP,
            [{"name": "server", "prior": None}],
            [{"name": "server", "prior": [[""], "user"]}],
        ),
    ],
)
def test_load_rejects_invalid_adapter_identity_before_recovery(
    tmp_path: Path,
    operation_state: Path,
    kind: operations.AdapterKind,
    valid_payload: object,
    invalid_payload: object,
) -> None:
    journal = operations.prepare(
        command="install",
        profile="p",
        config_dir=tmp_path,
        resources_lock=True,
        command_line=("install",),
        paths=(),
        adapters=(operations.AdapterSnapshot(kind, json.dumps(valid_payload)),),
    )
    operations.begin_checkpoint(
        journal,
        name="adapter",
        kind=operations.CheckpointKind.COMPENSATABLE,
        recovery="restore adapter",
        adapters=(kind,),
    )
    path = operations.journal_path("p")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["adapters"][0]["payload_json"] = json.dumps(invalid_payload)
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(SetforgeError, match="invalid operation journal"):
        operations.load("p")


def test_state_root_mismatch_refuses_before_adapter_recovery(
    tmp_path: Path,
    operation_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from setforge import vscode_extensions

    journal = operations.prepare(
        command="install",
        profile="p",
        config_dir=tmp_path,
        resources_lock=True,
        command_line=("install",),
        paths=(),
        adapters=(
            operations.AdapterSnapshot(
                operations.AdapterKind.EXTENSIONS, '["expected.ext"]'
            ),
        ),
    )
    journal = operations.begin_checkpoint(
        journal,
        name="extensions",
        kind=operations.CheckpointKind.COMPENSATABLE,
        recovery="restore extensions",
    )
    calls = 0

    def list_installed() -> set[str]:
        nonlocal calls
        calls += 1
        return set()

    monkeypatch.setattr(vscode_extensions, "list_installed", list_installed)
    monkeypatch.setattr(transitions, "state_root", lambda: tmp_path / "other-state")

    with pytest.raises(SetforgeError, match="SETFORGE_STATE_DIR"):
        operations.validate_recovery(journal)

    assert calls == 0


def test_active_journal_is_visible_across_transition_state_roots(
    tmp_path: Path,
    operation_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _prepare(tmp_path)
    monkeypatch.setattr(transitions, "state_root", lambda: tmp_path / "other-state")

    assert operations.load("p").operation_id == journal.operation_id
    with pytest.raises(SetforgeError, match="unfinished install"):
        operations.refuse_conflicting_mutation(
            resources=True, config_dir=None, profile="other"
        )
