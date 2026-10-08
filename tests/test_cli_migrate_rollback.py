from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge import operations, scalar_base_store, transitions
from setforge.base_store_format import SIDECAR_NAME
from setforge.cli import app
from setforge.migrations import (
    ManifestEntry,
    ManifestType,
    Migration,
    MigrationRoots,
    registry,
)
from tests.shared_helpers import text_images
from tests.test_cli_migrate_revert import _write_chain_origin
from tests.test_marker_retire_migration import _host_local
from tests.test_marker_retire_migration import _setup as _write_marker_origin

runner = CliRunner()

_AT_1_0 = "version: 1\ntracked_files: {}\nprofiles:\n  default: {}\n"


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    return state


def _write_cfg(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(body, encoding="utf-8")
    return cfg


def test_migrate_journal_reserves_every_declared_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write_cfg(
        tmp_path,
        "version: 1\ntracked_files: {}\nprofiles:\n  default: {}\n  team/dev: {}\n",
    )
    monkeypatch.setattr("setforge.migrations.registry.MIGRATIONS", (_StampStep(),))
    captured: list[tuple[str, ...]] = []
    real_prepare = operations.prepare

    def recording_prepare(**kwargs: object) -> operations.OperationJournal:
        profiles = kwargs["profiles"]
        assert isinstance(profiles, tuple)
        captured.append(profiles)
        return real_prepare(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("setforge.cli.migrate.operations.prepare", recording_prepare)

    result = runner.invoke(
        app,
        ["migrate", "--config", str(cfg), "--to", "1.1", "--apply", "--yes"],
    )

    assert result.exit_code == 0, result.output
    assert captured == [("default", "migrate", "team/dev")]


@dataclass(slots=True, frozen=True)
class _StampStep:
    from_version: str = "1.0"
    to_version: str = "1.1"

    @property
    def reverse(self) -> _StampStep:
        return _StampStep(from_version=self.to_version, to_version=self.from_version)

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        return (
            ManifestEntry(
                type=ManifestType.ADD, description="stamp", affected_path=roots.cfg_path
            ),
        )

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        return (roots.cfg_path,)

    def apply(self, *, roots: MigrationRoots) -> None:
        from setforge.migrations._yaml_ops import atomic_write_yaml, yaml_rt

        data = yaml_rt().load(roots.cfg_path.read_text())
        data["schema_version"] = self.to_version
        atomic_write_yaml(roots.cfg_path, data)


@dataclass(slots=True, frozen=True)
class _InterruptStep:
    from_version: str = "1.1"
    to_version: str = "1.2"

    @property
    def reverse(self) -> _InterruptStep:
        return _InterruptStep(
            from_version=self.to_version, to_version=self.from_version
        )

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        return (ManifestEntry(type=ManifestType.NOTE, description="interrupt"),)

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        return (roots.cfg_path,)

    def apply(self, *, roots: MigrationRoots) -> None:
        if roots.pre_chain_snapshot is None:
            return
        raise KeyboardInterrupt


@dataclass(slots=True, frozen=True)
class _StoreCutoverStep:
    from_version: str = "1.1"
    to_version: str = "2.0"
    profile: str = "default"
    key: str = "cutover-key"
    writes_own_transition: bool = True

    @property
    def reverse(self) -> _StoreCutoverStep:
        return _StoreCutoverStep(
            from_version=self.to_version, to_version=self.from_version
        )

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        return (
            ManifestEntry(
                type=ManifestType.EDIT,
                description="store cutover",
                affected_path=roots.cfg_path,
            ),
        )

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        return (roots.cfg_path,)

    def rollback_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        return (
            transitions._snapshot_target(
                transitions.SnapshotStore.LOCAL_CONTENT, self.profile, self.key
            ),
        )

    def apply(self, *, roots: MigrationRoots) -> None:
        from setforge.migrations._yaml_ops import atomic_write_yaml, yaml_rt

        data = yaml_rt().load(roots.cfg_path.read_text(encoding="utf-8"))
        data["schema_version"] = self.to_version
        atomic_write_yaml(roots.cfg_path, data)

        if roots.pre_chain_snapshot is None:
            return

        target = transitions._snapshot_target(
            transitions.SnapshotStore.LOCAL_CONTENT, self.profile, self.key
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"CUTOVER-MUTATED\n")

        file_pre = dict(roots.pre_chain_snapshot)
        file_post = transitions.capture_files(tuple(file_pre))
        transitions.write_transition(
            transitions.make_meta(
                transitions.TransitionCommand.MIGRATE,
                transitions.MIGRATE_TRANSITION_PROFILE,
                record_end=True,
                command_line=None,
            ),
            file_pre,
            file_post,
            None,
            state_snapshots=(
                transitions.snapshot_store_state(
                    transitions.SnapshotStore.LOCAL_CONTENT, self.profile, self.key
                ),
            ),
        )


@dataclass(slots=True, frozen=True)
class _RaisingStep:
    from_version: str = "2.0"
    to_version: str = "2.1"

    @property
    def reverse(self) -> _RaisingStep:
        return _RaisingStep(from_version=self.to_version, to_version=self.from_version)

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        return (ManifestEntry(type=ManifestType.NOTE, description="boom"),)

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        return (roots.cfg_path,)

    def apply(self, *, roots: MigrationRoots) -> None:
        if roots.pre_chain_snapshot is None:
            return
        raise RuntimeError("terminal step deliberately fails")


def test_keyboard_interrupt_mid_chain_rolls_back_and_reraises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write_cfg(tmp_path, _AT_1_0)
    original = cfg.read_text()
    monkeypatch.setattr(
        "setforge.migrations.registry.MIGRATIONS", (_StampStep(), _InterruptStep())
    )
    result = runner.invoke(
        app,
        ["migrate", "--config", str(cfg), "--to", "1.2", "--apply", "--yes"],
        catch_exceptions=True,
    )
    assert cfg.read_text() == original, (
        f"file left half-applied after interrupt: {cfg.read_text()!r}"
    )
    assert result.exit_code != 1, (
        f"user-cancel misreported as migration error: exit_code={result.exit_code}"
    )
    assert result.exit_code == 130, f"expected SIGINT exit 130, got {result.exit_code}"


def test_store_cutover_then_failure_leaves_store_and_log_consistent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write_cfg(tmp_path, _AT_1_0)
    original = cfg.read_text()
    monkeypatch.setattr(
        "setforge.migrations.registry.MIGRATIONS",
        (_StampStep(), _StoreCutoverStep(), _RaisingStep()),
    )
    result = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", "2.1", "--apply", "--yes"]
    )
    assert result.exit_code == 1, result.output
    assert "rolled back" in result.output

    assert cfg.read_text() == original

    leg = transitions._snapshot_target(
        transitions.SnapshotStore.LOCAL_CONTENT, "default", "cutover-key"
    )
    assert not leg.exists(), (
        f"cutover store leg should be gone after rollback, but exists: "
        f"{leg.read_bytes()!r}"
    )

    assert (
        transitions.load_latest(
            transitions.MIGRATE_TRANSITION_PROFILE,
            command=transitions.TransitionCommand.MIGRATE,
        )
        is None
    ), "phantom cutover transition survived rollback"


@dataclass(slots=True, frozen=True)
class _ConcurrentInstallStep:
    from_version: str = "2.0"
    to_version: str = "2.1"
    installed_dir_holder: list[Path] | None = None

    @property
    def reverse(self) -> _ConcurrentInstallStep:
        return _ConcurrentInstallStep(
            from_version=self.to_version, to_version=self.from_version
        )

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        return (ManifestEntry(type=ManifestType.NOTE, description="concurrent"),)

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        return (roots.cfg_path,)

    def apply(self, *, roots: MigrationRoots) -> None:
        if roots.pre_chain_snapshot is None:
            return
        sentinel = roots.cfg_path.parent / "installed-file.txt"
        tx = transitions.write_transition(
            transitions.make_meta(
                transitions.TransitionCommand.INSTALL,
                "some-other-profile",
                record_end=True,
                command_line=None,
            ),
            text_images({sentinel: None}),
            text_images({sentinel: "installed\n"}),
            None,
        )
        if self.installed_dir_holder is not None:
            self.installed_dir_holder.append(Path(tx))


def test_rollback_sweep_spares_other_profiles_transition_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write_cfg(tmp_path, _AT_1_0)
    holder: list[Path] = []
    monkeypatch.setattr(
        "setforge.migrations.registry.MIGRATIONS",
        (
            _StampStep(),
            _StoreCutoverStep(),
            _ConcurrentInstallStep(installed_dir_holder=holder),
            _RaisingStep(from_version="2.1", to_version="2.2"),
        ),
    )
    result = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", "2.2", "--apply", "--yes"]
    )
    assert result.exit_code == 1, result.output

    assert holder, "concurrent install step did not run in the real apply"
    other_dir = holder[0]

    assert other_dir.exists(), (
        "concurrent non-migrate transition record was deleted by migrate rollback"
    )
    assert (other_dir / "meta.json").exists()

    assert (
        transitions.load_latest(
            transitions.MIGRATE_TRANSITION_PROFILE,
            command=transitions.TransitionCommand.MIGRATE,
        )
        is None
    ), "phantom cutover transition survived the scoped rollback"


def test_store_snapshot_captured_at_chain_start_preserves_prior_leg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write_cfg(tmp_path, _AT_1_0)

    leg = transitions._snapshot_target(
        transitions.SnapshotStore.LOCAL_CONTENT, "default", "cutover-key"
    )
    leg.parent.mkdir(parents=True, exist_ok=True)
    leg.write_bytes(b"PRIOR-INSTALL-CONTENT\n")

    monkeypatch.setattr(
        "setforge.migrations.registry.MIGRATIONS",
        (_StampStep(), _StoreCutoverStep(), _RaisingStep()),
    )
    result = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", "2.1", "--apply", "--yes"]
    )
    assert result.exit_code == 1, result.output

    assert leg.exists(), "legitimate prior store leg was deleted by rollback"
    assert leg.read_bytes() == b"PRIOR-INSTALL-CONTENT\n", (
        f"rollback restored wrong bytes (late snapshot?): {leg.read_bytes()!r}"
    )


@dataclass(slots=True, frozen=True)
class _LateLegStep:
    from_version: str = "1.1"
    to_version: str = "2.0"

    @property
    def reverse(self) -> _LateLegStep:
        return _LateLegStep(from_version=self.to_version, to_version=self.from_version)

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        return (ManifestEntry(type=ManifestType.NOTE, description="late leg"),)

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        if "1.1" not in roots.cfg_path.read_text(encoding="utf-8"):
            return (roots.cfg_path,)
        return (roots.cfg_path, _late_leg())

    def apply(self, *, roots: MigrationRoots) -> None:
        if roots.pre_chain_snapshot is None:
            return
        _late_leg().write_bytes(b"LATE-MUTATED\n")


def _late_leg() -> Path:
    return transitions._snapshot_target(
        transitions.SnapshotStore.LOCAL_CONTENT, "default", "late-key"
    )


def test_leg_named_only_after_an_earlier_step_is_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write_cfg(tmp_path, _AT_1_0)
    original = cfg.read_bytes()
    leg = _late_leg()
    leg.parent.mkdir(parents=True)
    leg.write_bytes(b"PRIOR\n")
    monkeypatch.setattr(
        "setforge.migrations.registry.MIGRATIONS",
        (_StampStep(), _LateLegStep(), _RaisingStep()),
    )

    result = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", "2.1", "--apply", "--yes"]
    )

    assert result.exit_code == 1, result.output
    assert leg.read_bytes() == b"PRIOR\n"
    assert cfg.read_bytes() == original
    assert operations.active(transitions.MIGRATE_TRANSITION_PROFILE) is None


@dataclass(slots=True, frozen=True)
class _FailAfterApply:
    inner: Migration

    def __getattr__(self, name: str) -> object:
        return getattr(self.inner, name)

    def apply(self, *, roots: MigrationRoots) -> None:
        self.inner.apply(roots=roots)
        if roots.pre_chain_snapshot is not None:
            raise RuntimeError("step deliberately fails after its writes")


def _state_files(state: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(state)): path.read_bytes()
        for path in sorted(state.rglob("*"))
        if path.is_file()
        and path.relative_to(state).parts[0] != "locks"
        and ".pre-" not in path.name
    }


def _fail_after(monkeypatch: pytest.MonkeyPatch, from_version: str) -> str:
    failing = next(m for m in registry.MIGRATIONS if m.from_version == from_version)
    monkeypatch.setattr(
        "setforge.migrations.registry.MIGRATIONS",
        tuple(_FailAfterApply(m) if m is failing else m for m in registry.MIGRATIONS),
    )
    return failing.to_version


@pytest.mark.parametrize("failing_from", ["2.1", "3.0", "4.0"])
def test_real_cutover_chain_failure_restores_the_whole_state_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing_from: str,
) -> None:
    cfg, local_yaml = _write_chain_origin(tmp_path)
    spans = transitions._spans_manifest_path("default", "notes")
    scalar = scalar_base_store.manifest_path("default", "notes")
    for legacy in (spans, scalar, scalar.parent / SIDECAR_NAME):
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_bytes(b"{}")
    target = _fail_after(monkeypatch, failing_from)
    files_before = {path: path.read_bytes() for path in (cfg, local_yaml)}
    state = transitions.state_root()
    state_before = _state_files(state)

    result = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", target, "--apply", "--yes"]
    )

    assert result.exit_code == 1, result.output
    assert "automatic recovery failed" not in result.output
    assert {path: path.read_bytes() for path in files_before} == files_before
    assert _state_files(state) == state_before
    assert operations.active(transitions.MIGRATE_TRANSITION_PROFILE) is None


def test_failed_automatic_rollback_points_at_recover_and_keeps_the_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, local_yaml = _write_chain_origin(tmp_path)
    target = _fail_after(monkeypatch, "2.1")
    files_before = {path: path.read_bytes() for path in (cfg, local_yaml)}
    state = transitions.state_root()
    state_before = _state_files(state)

    def failing_recovery(journal: operations.OperationJournal) -> None:
        raise OSError("disk went away")

    with monkeypatch.context() as failing:
        failing.setattr(
            "setforge.cli.migrate._recover_migration_journal", failing_recovery
        )
        result = runner.invoke(
            app,
            ["migrate", "--config", str(cfg), "--to", target, "--apply", "--yes"],
        )

    assert result.exit_code == 1, result.output
    assert "setforge recover --profile=migrate --apply" in result.output
    assert "rolled back" not in result.output
    assert operations.active(transitions.MIGRATE_TRANSITION_PROFILE) is not None
    assert _state_files(state) != state_before

    recovered = runner.invoke(app, ["recover", "--profile=migrate", "--apply", "--yes"])

    assert recovered.exit_code == 0, recovered.output
    assert operations.active(transitions.MIGRATE_TRANSITION_PROFILE) is None
    assert {path: path.read_bytes() for path in files_before} == files_before
    assert _state_files(state) == state_before


def test_fold_failure_removes_the_format_sidecar_it_stamped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, local_yaml = _write_chain_origin(tmp_path)
    seeded = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", "3.0", "--apply", "--yes"]
    )
    assert seeded.exit_code == 0, seeded.output
    state = transitions.state_root()
    (state / "base" / "default" / SIDECAR_NAME).unlink()
    target = _fail_after(monkeypatch, "3.0")
    files_before = {path: path.read_bytes() for path in (cfg, local_yaml)}
    state_before = _state_files(state)

    result = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", target, "--apply", "--yes"]
    )

    assert result.exit_code == 1, result.output
    assert {path: path.read_bytes() for path in files_before} == files_before
    assert _state_files(state) == state_before


@pytest.mark.parametrize("failing_from", ["2.0", "2.1", "3.0", "4.0"])
def test_real_marker_chain_failure_restores_the_whole_state_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing_from: str,
) -> None:
    body = _host_local("mine", "## Mine\nhost line\n")
    roots = _write_marker_origin(tmp_path, tracked="# T\n" + body, live="# T\n" + body)
    local_yaml = Path.home() / ".config" / "setforge" / "local.yaml"
    watched = (roots.cfg_path, roots.repo_root / "tracked" / "claude" / "CLAUDE.md")
    target = _fail_after(monkeypatch, failing_from)
    files_before = {path: path.read_bytes() for path in watched}
    state = transitions.state_root()
    state_before = _state_files(state)

    result = runner.invoke(
        app,
        [
            "migrate",
            "--config",
            str(roots.cfg_path),
            "--to",
            target,
            "--apply",
            "--yes",
        ],
    )

    assert result.exit_code == 1, result.output
    assert "automatic recovery failed" not in result.output
    assert {path: path.read_bytes() for path in watched} == files_before
    assert not local_yaml.exists()
    assert _state_files(state) == state_before
    assert operations.active(transitions.MIGRATE_TRANSITION_PROFILE) is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root can enter any directory")
def test_migration_record_scan_skips_a_record_directory_it_cannot_enter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge.cli import migrate as migrate_mod

    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    locked = transitions.transitions_root() / "20260518T120000000000Z-install-p"
    locked.mkdir(parents=True)
    (locked / "meta.json").write_text("{}", encoding="utf-8")
    locked.chmod(0o000)
    try:
        found = migrate_mod._migration_transition_dirs()
    finally:
        locked.chmod(0o700)

    assert found == frozenset()
