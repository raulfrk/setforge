"""CLI-level tests for ``setforge migrate``.

Mocks the themed ``button_bar`` widget with a deterministic stub
(``_fake_button_bar``) so the wizard runs headless.

The tests cover three call paths:

- ``--check``: empty-registry message (today's MIGRATIONS=() state)
  AND a chain-populated state injected via ``monkeypatch.setattr(
  "setforge.migrations.MIGRATIONS", ...)``.
- ``--apply``: short-circuit ``"nothing to apply"`` on empty registry,
  AND a full multi-file apply flow with the button bar returning each
  of the three :class:`MigrateChoice` outcomes, plus the ``CANCEL``
  (Esc) path.
- ``--pin=X.Y``: writes ``schema_version: <pin>`` into setforge.yaml
  while preserving comments + key order.
"""

from __future__ import annotations

import hashlib
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


def _fake_button_bar(value: Any) -> Any:
    return lambda *_args, **_kwargs: value


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
    # ``migrate --apply`` writes a real transition via transitions_root() →
    # Path.home(); pin SETFORGE_STATE_DIR so the record lands in a per-test
    # tmp tree independent of the autouse HOME-isolation fixture (belt-and-
    # suspenders — the HOME fixture alone is a single point of failure).
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
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


def test_contract_preview_and_apply_resolve_sources_from_tracked_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge.migrations._contract_2_0 import Contract20Migration
    from setforge.migrations._yaml_ops import yaml_rt

    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    source = tmp_path / "tracked" / "docs" / "AGENTS.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        "<!-- setforge:user-section start shared notes -->\n"
        "preserved body\n"
        "<!-- setforge:user-section end shared notes -->\n",
        encoding="utf-8",
    )
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(
        'minimum_version: "2.0"\n'
        "schema_version: '1.2'\n"
        "tracked_files:\n"
        "  instructions:\n"
        "    src: docs/AGENTS.md\n"
        "    dst: ~/.config/AGENTS.md\n"
        "    preserve_user_sections: true\n"
        "profiles: {}\n",
        encoding="utf-8",
    )
    chain = (Contract20Migration(),)
    monkeypatch.setattr("setforge.migrations.MIGRATIONS", chain)
    monkeypatch.setattr("setforge.migrations.current_expected_schema_version", "2.0")
    monkeypatch.setattr("setforge.cli.migrate.current_expected_schema_version", "2.0")
    monkeypatch.setattr("setforge.cli.migrate.shutil.which", lambda _: None)

    result = CliRunner().invoke(app, ["migrate", "--apply", "--yes", f"--config={cfg}"])

    assert result.exit_code == 0, result.output
    migrated = yaml_rt().load(cfg.read_text(encoding="utf-8"))
    spans = migrated["tracked_files"]["instructions"].get("spans", [])
    assert ("## notes" in result.output, [span["anchor"] for span in spans]) == (
        True,
        ["## notes"],
    )


def test_apply_button_bar_abort_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "setforge.yaml"
    _write_minimal_setforge_yaml(cfg, with_old_key=True)
    pre_bytes = cfg.read_bytes()
    chain = (_SetforgeYamlEditMigration(),)
    monkeypatch.setattr("setforge.migrations.MIGRATIONS", chain)
    monkeypatch.setattr("setforge.migrations.current_expected_schema_version", "1.1")
    monkeypatch.setattr("setforge.cli.migrate.current_expected_schema_version", "1.1")
    from setforge.cli.migrate import MigrateChoice

    monkeypatch.setattr(
        "setforge.cli.migrate.button_bar", _fake_button_bar(MigrateChoice.ABORT)
    )

    # CliRunner installs a non-TTY StringIO as sys.stdin; we need the
    # ``_confirm_migrate`` TTY check to pass through so the button-bar
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


def test_apply_button_bar_cancel_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge.ui.widgets import CANCEL

    cfg = tmp_path / "setforge.yaml"
    _write_minimal_setforge_yaml(cfg, with_old_key=True)
    pre_bytes = cfg.read_bytes()
    chain = (_SetforgeYamlEditMigration(),)
    monkeypatch.setattr("setforge.migrations.MIGRATIONS", chain)
    monkeypatch.setattr("setforge.migrations.current_expected_schema_version", "1.1")
    monkeypatch.setattr("setforge.cli.migrate.current_expected_schema_version", "1.1")
    monkeypatch.setattr("setforge.cli.migrate.button_bar", _fake_button_bar(CANCEL))

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


def test_apply_button_bar_no_backup_skips_backup_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``APPLY_NO_BACKUP`` mutates files but skips the .pre-X.Y.bak siblings."""
    # ``migrate --apply`` writes a real transition via transitions_root() →
    # Path.home(); pin SETFORGE_STATE_DIR so the record lands in a per-test
    # tmp tree independent of the autouse HOME-isolation fixture (belt-and-
    # suspenders — the HOME fixture alone is a single point of failure).
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    cfg = tmp_path / "setforge.yaml"
    _write_minimal_setforge_yaml(cfg, with_old_key=True)
    chain = (_SetforgeYamlEditMigration(),)
    monkeypatch.setattr("setforge.migrations.MIGRATIONS", chain)
    monkeypatch.setattr("setforge.migrations.current_expected_schema_version", "1.1")
    monkeypatch.setattr("setforge.cli.migrate.current_expected_schema_version", "1.1")
    from setforge.cli.migrate import MigrateChoice

    monkeypatch.setattr(
        "setforge.cli.migrate.button_bar",
        _fake_button_bar(MigrateChoice.APPLY_NO_BACKUP),
    )

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
    """B-M5: lists the complete real migration chain from schema 1.0."""
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(
        "version: 1\ntracked_files: {}\nprofiles: {p: {}}\n", encoding="utf-8"
    )
    runner = CliRunner()
    result = runner.invoke(app, ["migrate", "--check", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    assert "13 migration(s) available" in result.output
    assert "1.0 → 1.1" in result.output
    assert "1.1 → 1.2" in result.output
    assert "1.2 → 2.0" in result.output
    assert "2.0 → 2.1" in result.output
    assert "2.1 → 3.0" in result.output
    assert "3.0 → 4.0" in result.output
    assert "4.0 → 5.0" in result.output
    assert "5.0 → 6.0" in result.output
    assert "6.0 → 6.1" in result.output
    assert "6.1 → 6.2" in result.output
    assert "6.2 → 6.3" in result.output


def test_real_full_chain_latest_transition_reverts_to_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The latest owner records the final 6.5 image, so one revert reaches 1.0."""
    from setforge import atomicio, transitions

    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        "setforge.cli.migrate._run_post_apply_validate", lambda **_: None
    )
    cfg = tmp_path / "setforge.yaml"
    origin = (
        'version: 1\nminimum_version: "6.5"\ntracked_files: {}\nprofiles: {p: {}}\n'
    )
    cfg.write_text(origin, encoding="utf-8")
    retained_payloads: dict[str, str] = {}
    real_write = atomicio.atomic_write_text

    def record_retained_payloads(
        path: Path, text: str, **kwargs: object
    ) -> Path | None:
        if path.name == "changes.patch":
            retained_payloads.update(
                {
                    str(item.relative_to(path.parent)): hashlib.sha256(
                        item.read_bytes()
                    ).hexdigest()
                    for item in sorted(path.parent.rglob("*"))
                    if item.is_file() and item.name != "changes.patch"
                }
            )
        return real_write(path, text, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "setforge.cli.migrate.atomicio.atomic_write_text", record_retained_payloads
    )

    result = CliRunner().invoke(app, ["migrate", "--apply", "--yes", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    root = transitions.transitions_root()
    migrate_records = [
        path
        for path in root.iterdir()
        if path.is_dir()
        and transitions.load_meta(transitions.TransitionDir(path)).command
        is transitions.TransitionCommand.MIGRATE
    ]
    assert len(migrate_records) == 1
    assert retained_payloads
    assert {
        str(item.relative_to(migrate_records[0])): hashlib.sha256(
            item.read_bytes()
        ).hexdigest()
        for item in sorted(migrate_records[0].rglob("*"))
        if item.is_file() and item.name != "changes.patch"
    } == retained_payloads
    latest = transitions.load_latest(transitions.MIGRATE_TRANSITION_PROFILE)
    assert latest is not None

    transitions.apply_patch_reverse(latest, dry_run=True)
    transitions.apply_patch_reverse(latest)
    assert cfg.read_text(encoding="utf-8") == origin


def test_owned_transition_finalization_failure_recovers_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed chain-final patch replacement restores files and owner history."""
    from setforge import atomicio, transitions

    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    monkeypatch.setattr(
        "setforge.cli.migrate._run_post_apply_validate", lambda **_: None
    )
    cfg = tmp_path / "setforge.yaml"
    origin = (
        'version: 1\nminimum_version: "6.5"\ntracked_files: {}\nprofiles: {p: {}}\n'
    )
    cfg.write_text(origin, encoding="utf-8")
    real_write = atomicio.atomic_write_text

    def fail_final_patch(path: Path, text: str, **kwargs: object) -> Path | None:
        if path.name == "changes.patch":
            raise OSError("injected finalization failure")
        return real_write(path, text, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "setforge.cli.migrate.atomicio.atomic_write_text", fail_final_patch
    )
    result = CliRunner().invoke(app, ["migrate", "--apply", "--yes", f"--config={cfg}"])

    assert result.exit_code != 0
    assert cfg.read_text(encoding="utf-8") == origin
    root = transitions.transitions_root()
    assert not root.exists() or not any(root.iterdir())


def test_owned_transition_consolidation_failure_recovers_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed superseded-record removal restores origin and owner history."""
    from setforge import transitions
    from setforge.cli import migrate as migrate_cli

    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    monkeypatch.setattr(
        "setforge.cli.migrate._run_post_apply_validate", lambda **_: None
    )
    cfg = tmp_path / "setforge.yaml"
    origin = (
        'version: 1\nminimum_version: "6.5"\ntracked_files: {}\nprofiles: {p: {}}\n'
    )
    cfg.write_text(origin, encoding="utf-8")

    def fail_transition_removal(path: Path) -> None:
        raise OSError(f"injected consolidation failure: {path.name}")

    monkeypatch.setattr(
        migrate_cli,
        "_remove_superseded_transition",
        fail_transition_removal,
        raising=False,
    )
    result = CliRunner().invoke(app, ["migrate", "--apply", "--yes", f"--config={cfg}"])

    assert result.exit_code != 0
    assert cfg.read_text(encoding="utf-8") == origin
    root = transitions.transitions_root()
    assert not root.exists() or not any(root.iterdir())


def test_to_five_zero_from_four_zero_is_schema_only_and_round_trips(
    tmp_path: Path,
) -> None:
    """B-M8: ``--to=5.0`` on a 4.0 config is schema-only; 5.0 → 4.0 is reachable.

    Drives the REAL registry span-types-retire step. The 4.0 → 5.0 apply touches
    ONLY schema_version (every other field byte-identical), and the reverse
    5.0 → 4.0 restamps back — proving the down direction is reachable, not a refuse.
    """
    from setforge.migrations import detect_current_schema

    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(
        "# header\nversion: 1\nschema_version: '4.0'\n"
        "tracked_files: {}\nprofiles: {p: {}}\n",
        encoding="utf-8",
    )
    runner = CliRunner()

    # A helper to compare everything EXCEPT the schema_version stamp.
    def _non_schema_body(text: str) -> list[str]:
        return [ln for ln in text.splitlines() if "schema_version" not in ln]

    before_body = _non_schema_body(cfg.read_text(encoding="utf-8"))

    up = runner.invoke(
        app, ["migrate", f"--config={cfg}", "--to=5.0", "--apply", "--yes"]
    )
    assert up.exit_code == 0, up.output
    assert detect_current_schema(cfg) == "5.0"
    assert _non_schema_body(cfg.read_text(encoding="utf-8")) == before_body

    down = runner.invoke(
        app, ["migrate", f"--config={cfg}", "--to=4.0", "--apply", "--yes"]
    )
    assert down.exit_code == 0, down.output
    assert detect_current_schema(cfg) == "4.0"
    assert _non_schema_body(cfg.read_text(encoding="utf-8")) == before_body


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


def test_run_post_apply_validate_timeout_warns_not_raises(monkeypatch, capsys) -> None:
    """A timeout / exec failure in the best-effort post-apply validate must
    warn, not leak a raw traceback after the migration already applied."""
    import subprocess
    from pathlib import Path

    from setforge.cli import migrate as migrate_mod

    monkeypatch.setattr(migrate_mod.shutil, "which", lambda _n: "/usr/bin/setforge")

    def _boom(*_a: object, **_k: object) -> None:
        raise subprocess.TimeoutExpired(cmd="setforge validate", timeout=60)

    monkeypatch.setattr(migrate_mod.subprocess, "run", _boom)
    # Must NOT raise:
    migrate_mod._run_post_apply_validate(cfg_path=Path("/tmp/does-not-matter.yaml"))
    assert "did not complete" in capsys.readouterr().out
