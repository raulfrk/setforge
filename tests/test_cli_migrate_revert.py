"""End-to-end tests: a successful ``setforge migrate`` is revertible.

A migrate that mutates ``setforge.yaml`` (and any other affected file)
records a transition whose recorded patch, reversed by ``setforge
revert``, restores every mutated file — including the ``schema_version``
stamp — to its exact pre-migration bytes. The byte-restore is the sole
reverse authority: revert never re-runs the down-migration, so the
restored content matches the pre-migration bytes byte-for-byte (no
ruamel re-dump skew).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge import transitions
from setforge.cli import app
from setforge.migrations import (
    ManifestEntry,
    ManifestType,
    MigrationRoots,
)

runner = CliRunner()

_AT_1_0 = "version: 1\ntracked_files: {}\nprofiles:\n  default: {}\n"
_AT_1_1 = (
    'version: 1\nschema_version: "1.1"\ntracked_files: {}\nprofiles:\n  default: {}\n'
)


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the transition state dir to a per-test sandbox."""
    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    return state


def _write_cfg(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(body, encoding="utf-8")
    return cfg


@dataclass(slots=True, frozen=True)
class _StampStep:
    """Forward migration that stamps ``schema_version`` into setforge.yaml."""

    from_version: str = "1.0"
    to_version: str = "1.1"

    @property
    def reverse(self) -> _StampStep:
        return _StampStep(from_version=self.to_version, to_version=self.from_version)

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        return (
            ManifestEntry(
                type=ManifestType.ADD,
                description="stamp schema_version",
                affected_path=roots.cfg_path,
            ),
        )

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        return (roots.cfg_path,)

    def apply(self, *, roots: MigrationRoots) -> None:
        from setforge.migrations._yaml_ops import atomic_write_yaml, yaml_rt

        data = yaml_rt().load(roots.cfg_path.read_text())
        if self.to_version == "1.0":
            data.pop("schema_version", None)
        else:
            data["schema_version"] = self.to_version
        atomic_write_yaml(roots.cfg_path, data)


@dataclass(slots=True, frozen=True)
class _SidecarStep:
    """Forward migration that edits setforge.yaml AND a tracked sidecar file."""

    from_version: str = "1.0"
    to_version: str = "1.1"

    @property
    def reverse(self) -> _SidecarStep:
        return _SidecarStep(from_version=self.to_version, to_version=self.from_version)

    def _sidecar(self, roots: MigrationRoots) -> Path:
        return roots.repo_root / "tracked" / "note.md"

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        return (
            ManifestEntry(
                type=ManifestType.EDIT,
                description="bump schema + sidecar",
                affected_path=roots.cfg_path,
            ),
        )

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        return (roots.cfg_path, self._sidecar(roots))

    def apply(self, *, roots: MigrationRoots) -> None:
        from setforge.migrations._yaml_ops import atomic_write_yaml, yaml_rt

        data = yaml_rt().load(roots.cfg_path.read_text())
        data["schema_version"] = self.to_version
        atomic_write_yaml(roots.cfg_path, data)
        sidecar = self._sidecar(roots)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text("migrated body\n", encoding="utf-8")


def _latest_migrate() -> transitions.TransitionDir | None:
    return transitions.load_latest(
        "migrate", command=transitions.TransitionCommand.MIGRATE
    )


def test_migrate_apply_records_revertible_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state_dir: Path,
) -> None:
    """A migrate --apply records a MIGRATE transition; revert restores bytes."""
    cfg = _write_cfg(tmp_path, _AT_1_0)
    pre_bytes = cfg.read_bytes()
    monkeypatch.setattr("setforge.migrations.MIGRATIONS", (_StampStep(),))

    result = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", "1.1", "--apply", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert cfg.read_text().count("schema_version") == 1  # forward applied

    recorded = _latest_migrate()
    assert recorded is not None, "no migrate transition was recorded"

    revert = runner.invoke(
        app, ["revert", "--profile=migrate", f"--config={cfg}", "--yes"]
    )
    assert revert.exit_code == 0, revert.output
    assert cfg.read_bytes() == pre_bytes


def test_migrate_downgrade_records_revertible_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state_dir: Path,
) -> None:
    """A migrate --to=<older> downgrade is revertible to pre-downgrade bytes."""
    cfg = _write_cfg(tmp_path, _AT_1_1)
    pre_bytes = cfg.read_bytes()
    monkeypatch.setattr("setforge.migrations.MIGRATIONS", (_StampStep(),))

    result = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", "1.0", "--apply", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert "schema_version" not in cfg.read_text()  # downgraded

    recorded = _latest_migrate()
    assert recorded is not None

    revert = runner.invoke(
        app, ["revert", "--profile=migrate", f"--config={cfg}", "--yes"]
    )
    assert revert.exit_code == 0, revert.output
    assert cfg.read_bytes() == pre_bytes


def test_migrate_revert_round_trip_is_byte_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state_dir: Path,
) -> None:
    """revert(migrate(X)) == X byte-for-byte across setforge.yaml + sidecar.

    The sidecar did not exist pre-migration, so revert must delete it
    (restore to absent), proving the byte-restore covers creations too.
    """
    cfg = _write_cfg(tmp_path, _AT_1_0)
    pre_cfg = cfg.read_bytes()
    sidecar = tmp_path / "tracked" / "note.md"
    assert not sidecar.exists()
    monkeypatch.setattr("setforge.migrations.MIGRATIONS", (_SidecarStep(),))

    result = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", "1.1", "--apply", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert sidecar.exists()  # forward created the sidecar

    revert = runner.invoke(
        app, ["revert", "--profile=migrate", f"--config={cfg}", "--yes"]
    )
    assert revert.exit_code == 0, revert.output
    assert cfg.read_bytes() == pre_cfg
    assert not sidecar.exists(), "revert must remove the migration-created sidecar"


def test_migrate_transition_round_trips_through_metadata_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state_dir: Path,
) -> None:
    """The MIGRATE enum member deserializes via load_meta / load_latest."""
    cfg = _write_cfg(tmp_path, _AT_1_0)
    monkeypatch.setattr("setforge.migrations.MIGRATIONS", (_StampStep(),))

    result = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", "1.1", "--apply", "--yes"]
    )
    assert result.exit_code == 0, result.output

    recorded = _latest_migrate()
    assert recorded is not None
    meta = transitions.load_meta(recorded)
    assert meta.command is transitions.TransitionCommand.MIGRATE
    assert meta.profile == "migrate"
