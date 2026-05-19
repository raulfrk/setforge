"""CLI-level tests for ``setforge migrate``.

Mocks the prompt_toolkit ``radiolist_dialog`` to the deterministic
``_FakeDialog`` pattern used by the rest of the suite (see
``tests/test_cli_section.py``) so the wizard runs headless.

The tests cover three call paths:

- ``--check``: empty-registry message (today's MIGRATIONS=() state)
  AND a chain-populated state injected via ``monkeypatch.setattr(
  "setforge.migrations.MIGRATIONS", ...)``.
- ``--apply``: short-circuit ``"nothing to apply"`` on empty registry,
  AND a full multi-file apply flow with the radiolist returning each
  of the three :class:`MigrateChoice` outcomes.
- ``--pin=X.Y``: writes ``schema_version: <pin>`` into setforge.yaml
  while preserving comments + key order.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from setforge.cli import app
from setforge.migrations import (
    ManifestEntry,
    ManifestType,
    MigrationRoots,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeDialog:
    """Stand-in for prompt_toolkit's ``radiolist_dialog`` callable.

    The CLI does ``radiolist_dialog(...).run()`` — we return an object
    whose ``.run()`` yields a preset value, so the test can drive the
    wizard headless.
    """

    def __init__(self, value: Any) -> None:
        self._value = value

    def __call__(self, *_args: Any, **_kwargs: Any) -> _FakeDialog:
        return self

    def run(self) -> Any:
        return self._value


@dataclass(slots=True, frozen=True)
class _SetforgeYamlEditMigration:
    """Fake one-step migration that renames a key in setforge.yaml.

    Mutates ``roots.cfg_path`` only, so the CLI's multi-file diff
    preview gets exercised against a single file (the simplest non-
    trivial chain).
    """

    from_version: str = "1.0"
    to_version: str = "1.1"

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        return (
            ManifestEntry(
                type=ManifestType.RENAME,
                description="rename old_key → new_key",
                affected_path=roots.cfg_path,
            ),
        )

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        return (roots.cfg_path,)

    def apply(self, *, roots: MigrationRoots) -> None:
        from setforge.migrations._yaml_ops import (
            atomic_write_yaml,
            rename_key,
            yaml_rt,
        )

        with roots.cfg_path.open("r", encoding="utf-8") as fh:
            data = yaml_rt().load(fh)
        rename_key(data, "old_key", "new_key")
        atomic_write_yaml(roots.cfg_path, data)


def _write_minimal_setforge_yaml(path: Path, *, with_old_key: bool = False) -> None:
    """Lay down a minimum-viable setforge.yaml at ``path``."""
    body = "version: 1\n"
    if with_old_key:
        body += "old_key: stays-for-rename\n"
    body += "tracked_files: {}\nprofiles: {p: {}}\n"
    path.write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# --check
# ---------------------------------------------------------------------------


def test_check_reports_no_migrations_today(tmp_path: Path) -> None:
    """Empty registry yields ``"no migrations available"`` and exits 0."""
    cfg = tmp_path / "setforge.yaml"
    _write_minimal_setforge_yaml(cfg)
    runner = CliRunner()
    result = runner.invoke(app, ["migrate", "--check", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    assert "no migrations available" in result.output


def test_check_lists_chain_when_registry_populated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chain-populated registry prints each migration's manifest."""
    cfg = tmp_path / "setforge.yaml"
    _write_minimal_setforge_yaml(cfg, with_old_key=True)
    chain = (_SetforgeYamlEditMigration(),)
    monkeypatch.setattr("setforge.migrations.MIGRATIONS", chain)
    monkeypatch.setattr("setforge.migrations.current_expected_schema_version", "1.1")
    monkeypatch.setattr("setforge.cli.migrate.current_expected_schema_version", "1.1")
    runner = CliRunner()
    result = runner.invoke(app, ["migrate", "--check", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    assert "1 migration(s) available" in result.output
    assert "1.0 → 1.1" in result.output
    assert "rename old_key → new_key" in result.output


# ---------------------------------------------------------------------------
# --apply
# ---------------------------------------------------------------------------


def test_apply_empty_registry_says_nothing_to_apply(tmp_path: Path) -> None:
    cfg = tmp_path / "setforge.yaml"
    _write_minimal_setforge_yaml(cfg)
    runner = CliRunner()
    result = runner.invoke(app, ["migrate", "--apply", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    assert "nothing to apply" in result.output


def test_apply_with_yes_applies_with_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--yes`` collapses to APPLY_WITH_BACKUP without TTY/radiolist."""
    cfg = tmp_path / "setforge.yaml"
    _write_minimal_setforge_yaml(cfg, with_old_key=True)
    chain = (_SetforgeYamlEditMigration(),)
    monkeypatch.setattr("setforge.migrations.MIGRATIONS", chain)
    monkeypatch.setattr("setforge.migrations.current_expected_schema_version", "1.1")
    monkeypatch.setattr("setforge.cli.migrate.current_expected_schema_version", "1.1")
    # Stub the post-apply validate shell-out so the test never depends
    # on the on-PATH ``setforge`` binary being current with the worktree.
    monkeypatch.setattr("setforge.cli.migrate.shutil.which", lambda _: None)

    runner = CliRunner()
    result = runner.invoke(app, ["migrate", "--apply", "--yes", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    assert "preview of changes" in result.output
    assert "applying" in result.output
    assert "backup:" in result.output
    assert (cfg.parent / "setforge.yaml.pre-1.1.bak").exists()
    assert "new_key:" in cfg.read_text(encoding="utf-8")
    assert "old_key:" not in cfg.read_text(encoding="utf-8")


def test_apply_radiolist_abort_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the user picks ABORT in the radiolist, no files are touched."""
    cfg = tmp_path / "setforge.yaml"
    _write_minimal_setforge_yaml(cfg, with_old_key=True)
    pre_bytes = cfg.read_bytes()
    chain = (_SetforgeYamlEditMigration(),)
    monkeypatch.setattr("setforge.migrations.MIGRATIONS", chain)
    monkeypatch.setattr("setforge.migrations.current_expected_schema_version", "1.1")
    monkeypatch.setattr("setforge.cli.migrate.current_expected_schema_version", "1.1")
    from setforge.cli.migrate import MigrateChoice

    monkeypatch.setattr(
        "setforge.cli.migrate.radiolist_dialog", _FakeDialog(MigrateChoice.ABORT)
    )

    # CliRunner installs a non-TTY StringIO as sys.stdin; we need the
    # ``_confirm_migrate`` TTY check to pass through so the radiolist
    # stub fires. Patch the module's ``sys`` to a stand-in whose
    # ``stdin.isatty()`` returns True.
    class _TtyStdin:
        @staticmethod
        def isatty() -> bool:
            return True

    class _Sys:
        stdin = _TtyStdin()

    monkeypatch.setattr("setforge.cli.migrate.sys", _Sys)

    runner = CliRunner()
    result = runner.invoke(app, ["migrate", "--apply", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    assert "aborted" in result.output
    assert cfg.read_bytes() == pre_bytes
    assert not (cfg.parent / "setforge.yaml.pre-1.1.bak").exists()


def test_apply_radiolist_no_backup_skips_backup_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``APPLY_NO_BACKUP`` mutates files but skips the .pre-X.Y.bak siblings."""
    cfg = tmp_path / "setforge.yaml"
    _write_minimal_setforge_yaml(cfg, with_old_key=True)
    chain = (_SetforgeYamlEditMigration(),)
    monkeypatch.setattr("setforge.migrations.MIGRATIONS", chain)
    monkeypatch.setattr("setforge.migrations.current_expected_schema_version", "1.1")
    monkeypatch.setattr("setforge.cli.migrate.current_expected_schema_version", "1.1")
    from setforge.cli.migrate import MigrateChoice

    monkeypatch.setattr(
        "setforge.cli.migrate.radiolist_dialog",
        _FakeDialog(MigrateChoice.APPLY_NO_BACKUP),
    )

    # CliRunner installs a non-TTY StringIO as sys.stdin; we need the
    # ``_confirm_migrate`` TTY check to pass through so the radiolist
    # stub fires. Patch the module's ``sys`` to a stand-in whose
    # ``stdin.isatty()`` returns True.
    class _TtyStdin:
        @staticmethod
        def isatty() -> bool:
            return True

    class _Sys:
        stdin = _TtyStdin()

    monkeypatch.setattr("setforge.cli.migrate.sys", _Sys)
    monkeypatch.setattr("setforge.cli.migrate.shutil.which", lambda _: None)

    runner = CliRunner()
    result = runner.invoke(app, ["migrate", "--apply", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    assert "new_key:" in cfg.read_text(encoding="utf-8")
    assert not (cfg.parent / "setforge.yaml.pre-1.1.bak").exists()


def test_apply_mutually_exclusive_with_check(tmp_path: Path) -> None:
    cfg = tmp_path / "setforge.yaml"
    _write_minimal_setforge_yaml(cfg)
    runner = CliRunner()
    result = runner.invoke(app, ["migrate", "--check", "--apply", f"--config={cfg}"])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# --pin
# ---------------------------------------------------------------------------


def test_pin_writes_schema_version_into_yaml(tmp_path: Path) -> None:
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(
        "# header\nversion: 1\ntracked_files: {}\nprofiles: {p: {}}\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["migrate", "--pin=1.0", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    content = cfg.read_text(encoding="utf-8")
    assert "schema_version: '1.0'" in content or "schema_version: 1.0" in content
    # Header comment must survive the round-trip write.
    assert "# header" in content


def test_pin_overwrites_existing_schema_version(tmp_path: Path) -> None:
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(
        "schema_version: '1.1'\nversion: 1\ntracked_files: {}\nprofiles: {p: {}}\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["migrate", "--pin=1.0", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    content = cfg.read_text(encoding="utf-8")
    assert "1.0" in content
    assert "1.1" not in content


# ---------------------------------------------------------------------------
# No-arg path
# ---------------------------------------------------------------------------


def test_bare_migrate_prints_check_report_and_specify_hint(tmp_path: Path) -> None:
    """Bare ``setforge migrate`` (no --check/--apply/--pin) prints the
    check report PLUS the ``specify --check, --apply, or --pin`` hint."""
    cfg = tmp_path / "setforge.yaml"
    _write_minimal_setforge_yaml(cfg)
    runner = CliRunner()
    result = runner.invoke(app, ["migrate", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    assert "specify --check, --apply, or --pin" in result.output
