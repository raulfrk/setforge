"""CLI tests for ``setforge migrate --to`` + migration write-safety.

Covers the ``--to=<version>`` target modifier
(up/down), its guards (mutex with ``--pin``, unknown-target rejection,
already-at no-op), and the partial-chain rollback / backup no-clobber
write-safety behaviors.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import app
from setforge.migrations import MigrationRoots, detect_current_schema

runner = CliRunner()

_AT_1_1 = (
    'version: 1\nschema_version: "1.1"\ntracked_files: {}\nprofiles:\n  default: {}\n'
)
_AT_1_0 = "version: 1\ntracked_files: {}\nprofiles:\n  default: {}\n"


def _write(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(body, encoding="utf-8")
    return cfg


# ---------------------------------------------------------------------------
# --to guards
# ---------------------------------------------------------------------------


def test_to_equals_current_is_noop(tmp_path: Path) -> None:
    cfg = _write(tmp_path, _AT_1_0)  # absent schema_version -> 1.0
    result = runner.invoke(app, ["migrate", "--config", str(cfg), "--to", "1.0"])
    assert result.exit_code == 0
    assert "already at schema_version 1.0" in result.stdout
    # no write — schema_version still absent
    assert detect_current_schema(cfg) == "1.0"


def test_to_unknown_version_rejected(tmp_path: Path) -> None:
    cfg = _write(tmp_path, _AT_1_1)
    result = runner.invoke(app, ["migrate", "--config", str(cfg), "--to", "9.9"])
    assert result.exit_code != 0
    assert "unknown schema version" in result.output


def test_pin_rejects_non_mapping_root(tmp_path: Path) -> None:
    """A list/scalar-root config yields a clean CLI error, not a TypeError."""
    cfg = _write(tmp_path, "- just\n- a\n- list\n")
    result = runner.invoke(app, ["migrate", "--config", str(cfg), "--pin", "1.1"])
    assert result.exit_code != 0
    assert "root must be a mapping" in result.output


def test_to_and_pin_mutually_exclusive(tmp_path: Path) -> None:
    cfg = _write(tmp_path, _AT_1_1)
    result = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--pin", "1.0", "--to", "1.0"]
    )
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output
    # the pin must NOT have been written despite being parsed first
    assert detect_current_schema(cfg) == "1.1"


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def test_to_downgrade_check_previews_reverse(tmp_path: Path) -> None:
    cfg = _write(tmp_path, _AT_1_1)
    result = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", "1.0", "--check"]
    )
    assert result.exit_code == 0
    assert "1.1 → 1.0" in result.stdout
    # check never writes
    assert detect_current_schema(cfg) == "1.1"


def test_to_downgrade_apply_yes_strips_stamp(tmp_path: Path) -> None:
    cfg = _write(tmp_path, _AT_1_1)
    result = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", "1.0", "--apply", "--yes"]
    )
    assert result.exit_code == 0
    # down-converted: schema_version stamp removed -> detect defaults to 1.0
    assert detect_current_schema(cfg) == "1.0"


def test_downgrade_non_tty_without_yes_requires_interactive(tmp_path: Path) -> None:
    cfg = _write(tmp_path, _AT_1_1)
    # CliRunner stdin is not a TTY; no --yes -> ConfirmRequiresInteractive
    result = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", "1.0", "--apply"]
    )
    assert result.exit_code != 0
    assert detect_current_schema(cfg) == "1.1"  # unchanged


# ---------------------------------------------------------------------------
# partial-chain rollback
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class _StampStep:
    """Forward step that stamps schema_version (1.0->1.1)."""

    from_version: str = "1.0"
    to_version: str = "1.1"

    @property
    def reverse(self) -> _StampStep:
        return _StampStep(from_version=self.to_version, to_version=self.from_version)

    def manifest(self, *, roots: MigrationRoots) -> tuple:
        from setforge.migrations import ManifestEntry, ManifestType

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
class _RaisingStep:
    """Second step that always raises — to exercise mid-chain rollback."""

    from_version: str = "1.1"
    to_version: str = "1.2"

    @property
    def reverse(self) -> _RaisingStep:
        return _RaisingStep(from_version=self.to_version, to_version=self.from_version)

    def manifest(self, *, roots: MigrationRoots) -> tuple:
        from setforge.migrations import ManifestEntry, ManifestType

        return (ManifestEntry(type=ManifestType.NOTE, description="boom"),)

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        return (roots.cfg_path,)

    def apply(self, *, roots: MigrationRoots) -> None:
        raise RuntimeError("step 2 deliberately fails")


def test_partial_chain_failure_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step 2 raising must roll the file back to its pre-migration bytes."""
    cfg = _write(tmp_path, _AT_1_0)
    original = cfg.read_text()
    monkeypatch.setattr(
        "setforge.migrations.MIGRATIONS", (_StampStep(), _RaisingStep())
    )
    result = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--to", "1.2", "--apply", "--yes"]
    )
    assert result.exit_code == 1
    assert "rolled back" in result.output
    # file restored to original bytes — NOT left half-migrated at 1.1
    assert cfg.read_text() == original
    assert detect_current_schema(cfg) == "1.0"
