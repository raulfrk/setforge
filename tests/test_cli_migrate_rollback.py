from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge import operations, transitions
from setforge.cli import app
from setforge.migrations import ManifestEntry, ManifestType, MigrationRoots

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
    monkeypatch.setattr("setforge.migrations.MIGRATIONS", (_StampStep(),))
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

    def apply(self, *, roots: MigrationRoots) -> None:
        from setforge.migrations._yaml_ops import atomic_write_yaml, yaml_rt

        cfg_pre = roots.cfg_path.read_text(encoding="utf-8")
        data = yaml_rt().load(cfg_pre)
        data["schema_version"] = self.to_version
        atomic_write_yaml(roots.cfg_path, data)

        if roots.pre_chain_snapshot is None:
            return

        target = transitions._snapshot_target(
            transitions.SnapshotStore.LOCAL_CONTENT, self.profile, self.key
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"CUTOVER-MUTATED\n")

        pre = roots.pre_chain_snapshot
        file_pre: dict[Path, str | None] = (
            dict(pre) if pre is not None else {roots.cfg_path: cfg_pre}
        )
        file_post = transitions.snapshot_paths(tuple(file_pre))
        transitions.write_transition(
            transitions.make_meta(
                transitions.TransitionCommand.MIGRATE,
                transitions.MIGRATE_TRANSITION_PROFILE,
                end_timestamp=transitions.now_utc().isoformat(),
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
        "setforge.migrations.MIGRATIONS", (_StampStep(), _InterruptStep())
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
        "setforge.migrations.MIGRATIONS",
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
                end_timestamp=transitions.now_utc().isoformat(),
                command_line=None,
            ),
            {sentinel: None},
            {sentinel: "installed\n"},
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
        "setforge.migrations.MIGRATIONS",
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
        "setforge.migrations.MIGRATIONS",
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
