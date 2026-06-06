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
from typing import Any, ClassVar

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


def test_check_reports_no_migrations_when_registry_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty registry yields ``"no migrations available"`` and exits 0.

    The real registry now ships the 1.0 → 1.1 stamp, so this empty-registry
    path is exercised via monkeypatch (keeping the original assertion alive
    rather than deleting it).
    """
    cfg = tmp_path / "setforge.yaml"
    _write_minimal_setforge_yaml(cfg)
    monkeypatch.setattr("setforge.migrations.MIGRATIONS", ())
    monkeypatch.setattr("setforge.migrations.current_expected_schema_version", "1.0")
    monkeypatch.setattr("setforge.cli.migrate.current_expected_schema_version", "1.0")
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


def test_apply_empty_registry_says_nothing_to_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The empty-registry ``--apply`` short-circuit (registry forced empty)."""
    cfg = tmp_path / "setforge.yaml"
    _write_minimal_setforge_yaml(cfg)
    monkeypatch.setattr("setforge.migrations.MIGRATIONS", ())
    monkeypatch.setattr("setforge.migrations.current_expected_schema_version", "1.0")
    monkeypatch.setattr("setforge.cli.migrate.current_expected_schema_version", "1.0")
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
    # With backups, the completion report DOES surface the rollback hint.
    assert "to undo" in result.output
    assert ".pre-1.1.bak" in result.output


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
        argv: ClassVar[list[str]] = ["setforge", "migrate", "--apply"]

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
        argv: ClassVar[list[str]] = ["setforge", "migrate", "--apply"]

    monkeypatch.setattr("setforge.cli.migrate.sys", _Sys)
    monkeypatch.setattr("setforge.cli.migrate.shutil.which", lambda _: None)

    runner = CliRunner()
    result = runner.invoke(app, ["migrate", "--apply", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    assert "new_key:" in cfg.read_text(encoding="utf-8")
    assert not (cfg.parent / "setforge.yaml.pre-1.1.bak").exists()
    # No backups were written, so the completion report omits the
    # ``.pre-X.Y.bak`` rollback hint entirely.
    assert "to undo" not in result.output
    assert ".pre-1.1.bak" not in result.output


def test_apply_backup_failure_aborts_before_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing ``shutil.copy2`` during backup aborts BEFORE ``apply()``.

    SPEC 4 forbids shortcutting on the first failure: when even one
    backup raises, the driver must collect failures across the full
    path list, then abort with exit code 1 WITHOUT touching any
    migration's ``apply()`` — better to leave files untouched than
    to mutate with an incomplete safety net.
    """
    cfg = tmp_path / "setforge.yaml"
    _write_minimal_setforge_yaml(cfg, with_old_key=True)

    # The preview pass calls apply() against a shadow tree; we only
    # care about calls against the REAL cfg_path (the user's file).
    real_apply_calls: list[str] = []

    @dataclass(slots=True, frozen=True)
    class _TrackingMigration:
        from_version: str = "1.0"
        to_version: str = "1.1"
        _real_cfg: Path = cfg

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
            if roots.cfg_path == self._real_cfg:
                real_apply_calls.append(f"{self.from_version}→{self.to_version}")

    chain = (_TrackingMigration(),)
    monkeypatch.setattr("setforge.migrations.MIGRATIONS", chain)
    monkeypatch.setattr("setforge.migrations.current_expected_schema_version", "1.1")
    monkeypatch.setattr("setforge.cli.migrate.current_expected_schema_version", "1.1")

    # Force the backup-loop copy2 call to raise. The preview pass uses
    # copy2 too — patch only the second call (the real backup pass)
    # by routing through a counter so the preview render succeeds.
    import shutil as _shutil

    real_copy2 = _shutil.copy2
    call_count = {"n": 0}

    def _failing_copy2(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
        call_count["n"] += 1
        # The preview pass copies into the tmp shadow tree first; let
        # those succeed. The .pre-1.1.bak sibling lives next to the
        # config file, so we detect the backup pass by destination path.
        if str(dst).endswith(".pre-1.1.bak"):
            raise OSError("simulated backup failure")
        return real_copy2(src, dst, *args, **kwargs)

    monkeypatch.setattr("setforge.cli.migrate.shutil.copy2", _failing_copy2)
    monkeypatch.setattr("setforge.cli.migrate.shutil.which", lambda _: None)

    runner = CliRunner()
    result = runner.invoke(app, ["migrate", "--apply", "--yes", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    assert "backup FAILED" in result.output
    assert "aborting migration" in result.output
    # apply() against the REAL cfg must NEVER fire when a backup
    # failed — only the preview pass (shadow roots) may have run.
    assert real_apply_calls == [], (
        f"expected no real apply() calls on backup failure; got {real_apply_calls!r}"
    )
    # The .pre-1.1.bak sibling must not exist (the copy2 call raised).
    assert not (cfg.parent / "setforge.yaml.pre-1.1.bak").exists()
    # Original file must be untouched.
    assert "old_key:" in cfg.read_text(encoding="utf-8")
    assert "new_key:" not in cfg.read_text(encoding="utf-8")


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


@pytest.mark.parametrize(
    "bad_pin",
    [
        "hello",  # non-version garbage
        "9.9",  # well-formed but unregistered version
        " 1.0 ",  # surrounding whitespace
        "1.0\nmalicious: x",  # YAML-injection payload
        "!!python/object",  # YAML tag metacharacters
    ],
)
def test_pin_rejects_invalid_value_before_writing(tmp_path: Path, bad_pin: str) -> None:
    """An invalid --pin raises a usage error and never mutates setforge.yaml."""
    cfg = tmp_path / "setforge.yaml"
    original = "# header\nversion: 1\ntracked_files: {}\nprofiles: {p: {}}\n"
    cfg.write_text(original, encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(app, ["migrate", f"--pin={bad_pin}", f"--config={cfg}"])
    assert result.exit_code != 0
    # File is byte-for-byte unchanged — validation fires before any write.
    assert cfg.read_text(encoding="utf-8") == original
    assert "schema_version" not in cfg.read_text(encoding="utf-8")


def test_pin_accepts_one_one_real_registry(tmp_path: Path) -> None:
    """B-M5: ``--pin=1.1`` (to_version + current_expected) exits 0 — real registry."""
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(
        "version: 1\ntracked_files: {}\nprofiles: {p: {}}\n", encoding="utf-8"
    )
    runner = CliRunner()
    result = runner.invoke(app, ["migrate", "--pin=1.1", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    assert "schema_version" in cfg.read_text(encoding="utf-8")
    assert "1.1" in cfg.read_text(encoding="utf-8")


def test_pin_accepts_one_zero_real_registry(tmp_path: Path) -> None:
    """B-M5: ``--pin=1.0`` (the migration's from_version) exits 0 — real registry."""
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(
        "version: 1\ntracked_files: {}\nprofiles: {p: {}}\n", encoding="utf-8"
    )
    runner = CliRunner()
    result = runner.invoke(app, ["migrate", "--pin=1.0", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    assert "1.0" in cfg.read_text(encoding="utf-8")


def test_pin_rejects_unknown_version_real_registry(tmp_path: Path) -> None:
    """B-M5: an unregistered version (``9.9``) is rejected against the real registry."""
    cfg = tmp_path / "setforge.yaml"
    original = "version: 1\ntracked_files: {}\nprofiles: {p: {}}\n"
    cfg.write_text(original, encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(app, ["migrate", "--pin=9.9", f"--config={cfg}"])
    assert result.exit_code != 0
    assert cfg.read_text(encoding="utf-8") == original


def test_check_lists_real_registry_migration(tmp_path: Path) -> None:
    """B-M5: ``migrate --check`` lists the real 1.0 → 1.1 stamp on a 1.0 config."""
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(
        "version: 1\ntracked_files: {}\nprofiles: {p: {}}\n", encoding="utf-8"
    )
    runner = CliRunner()
    result = runner.invoke(app, ["migrate", "--check", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    assert "1 migration(s) available" in result.output
    assert "1.0 → 1.1" in result.output


def test_pin_accepts_current_known_version(tmp_path: Path) -> None:
    """The build's current schema version is accepted and written."""
    from setforge.migrations import current_expected_schema_version

    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(
        "# header\nversion: 1\ntracked_files: {}\nprofiles: {p: {}}\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(
        app, ["migrate", f"--pin={current_expected_schema_version}", f"--config={cfg}"]
    )
    assert result.exit_code == 0, result.output
    assert "schema_version" in cfg.read_text(encoding="utf-8")


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
