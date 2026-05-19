"""Tests for :mod:`setforge.migrations` — Protocol shape + multi-file lifecycle.

The load-bearing assertions:

- The v0.2.0 registry is empty.
- ``current_expected_schema_version`` is ``"1.0"``.
- ``detect_current_schema`` reads ``schema_version`` from the YAML
  and defaults to ``"1.0"`` on absence / missing file / empty file.
- ``find_migration_path`` returns ``()`` when the registry is empty
  (today's state) and walks a non-empty registry forward when one is
  injected via ``monkeypatch``.
- The Migration Protocol shape accepts a multi-file migration that
  touches ``setforge.yaml`` + ``local.yaml`` + a tracked content file
  simultaneously, and the full lifecycle (manifest / affected_paths /
  apply / backup / rollback) works at multi-file granularity. This is
  the spec-broadened-scope assertion: a Migration is NOT just a
  schema-YAML edit.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from setforge.migrations import (
    MIGRATIONS,
    ManifestEntry,
    ManifestType,
    Migration,
    MigrationRoots,
    current_expected_schema_version,
    detect_current_schema,
    find_migration_path,
)
from setforge.migrations._fs_ops import atomic_replace, backup_path
from setforge.migrations._yaml_ops import atomic_write_yaml, rename_key, yaml_rt

# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------


def test_current_expected_schema_version_is_one_zero() -> None:
    assert current_expected_schema_version == "1.0"


def test_migrations_registry_is_empty_in_v020() -> None:
    assert MIGRATIONS == ()


def test_find_migration_path_empty_registry_returns_empty() -> None:
    assert find_migration_path(from_v="1.0", to_v="1.1") == ()


def test_find_migration_path_same_version_returns_empty() -> None:
    assert find_migration_path(from_v="1.0", to_v="1.0") == ()


# ---------------------------------------------------------------------------
# detect_current_schema
# ---------------------------------------------------------------------------


def test_detect_current_schema_missing_file_returns_default(tmp_path: Path) -> None:
    assert detect_current_schema(tmp_path / "absent.yaml") == "1.0"


def test_detect_current_schema_no_field_returns_default(tmp_path: Path) -> None:
    yaml_path = tmp_path / "setforge.yaml"
    yaml_path.write_text("version: 1\ntracked_files: {}\n", encoding="utf-8")
    assert detect_current_schema(yaml_path) == "1.0"


def test_detect_current_schema_reads_declared_version(tmp_path: Path) -> None:
    yaml_path = tmp_path / "setforge.yaml"
    yaml_path.write_text(
        "schema_version: '1.1'\nversion: 1\ntracked_files: {}\n",
        encoding="utf-8",
    )
    assert detect_current_schema(yaml_path) == "1.1"


def test_detect_current_schema_empty_file_returns_default(tmp_path: Path) -> None:
    yaml_path = tmp_path / "empty.yaml"
    yaml_path.write_text("", encoding="utf-8")
    assert detect_current_schema(yaml_path) == "1.0"


# ---------------------------------------------------------------------------
# Protocol shape via runtime_checkable isinstance + injected registry walk
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class _NoopMigration:
    """Minimal Migration impl — Protocol shape only, no filesystem side effects."""

    from_version: str
    to_version: str

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        return (ManifestEntry(type=ManifestType.NOTE, description="noop"),)

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        return ()

    def apply(self, *, roots: MigrationRoots) -> None:
        return None


def test_noop_satisfies_migration_protocol() -> None:
    instance = _NoopMigration(from_version="1.0", to_version="1.1")
    assert isinstance(instance, Migration)


def test_find_migration_path_walks_chain_via_monkeypatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 2-step registry walks 1.0 → 1.1 → 1.2 correctly."""
    chain = (
        _NoopMigration(from_version="1.0", to_version="1.1"),
        _NoopMigration(from_version="1.1", to_version="1.2"),
    )
    monkeypatch.setattr("setforge.migrations.MIGRATIONS", chain)
    found = find_migration_path(from_v="1.0", to_v="1.2")
    assert tuple(m.to_version for m in found) == ("1.1", "1.2")


def test_find_migration_path_no_chain_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the registry cannot bridge from_v → to_v, return ()."""
    chain = (_NoopMigration(from_version="1.0", to_version="1.1"),)
    monkeypatch.setattr("setforge.migrations.MIGRATIONS", chain)
    assert find_migration_path(from_v="1.0", to_v="9.9") == ()


# ---------------------------------------------------------------------------
# Multi-file lifecycle — the broadened-scope assertion (spec annotation 2026-05-19).
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class _MultiFileMigration:
    """Fake Migration that mutates setforge.yaml + local.yaml + a tracked file.

    Proves the Protocol's broadened scope covers the FULL set of local-file
    changes for a single version bump, not just the schema YAML.
    """

    from_version: str = "1.0"
    to_version: str = "1.1"

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        return (
            ManifestEntry(
                type=ManifestType.EDIT,
                description="rename old_key → new_key",
                affected_path=roots.cfg_path,
            ),
            ManifestEntry(
                type=ManifestType.EDIT,
                description="add fresh_local_field",
                affected_path=roots.home / ".config" / "setforge" / "local.yaml",
            ),
            ManifestEntry(
                type=ManifestType.EDIT,
                description="rewrite legacy sentinel",
                affected_path=roots.repo_root / "tracked" / "claude" / "CLAUDE.md",
            ),
        )

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        return (
            roots.cfg_path,
            roots.home / ".config" / "setforge" / "local.yaml",
            roots.repo_root / "tracked" / "claude" / "CLAUDE.md",
        )

    def apply(self, *, roots: MigrationRoots) -> None:
        # 1. setforge.yaml rename via ruamel rt + atomic write.
        yaml = yaml_rt()
        with roots.cfg_path.open("r", encoding="utf-8") as fh:
            data = yaml.load(fh)
        rename_key(data, "old_key", "new_key")
        atomic_write_yaml(roots.cfg_path, data)

        # 2. local.yaml additive field.
        local_path = roots.home / ".config" / "setforge" / "local.yaml"
        local_path.parent.mkdir(parents=True, exist_ok=True)
        if local_path.exists():
            with local_path.open("r", encoding="utf-8") as fh:
                local_data = yaml.load(fh) or {}
        else:
            local_data = yaml.load("{}\n")
        local_data["fresh_local_field"] = "added-by-migration"
        atomic_write_yaml(local_path, local_data)

        # 3. tracked content file: rewrite a sentinel via atomic_replace.
        tracked = roots.repo_root / "tracked" / "claude" / "CLAUDE.md"
        tmp = tracked.with_suffix(".md.migration.tmp")
        before = tracked.read_text(encoding="utf-8") if tracked.exists() else ""
        tmp.write_text(before.replace("legacy", "migrated"), encoding="utf-8")
        atomic_replace(tmp, tracked)


def _seed_multi_file_fixture(roots: MigrationRoots) -> None:
    """Lay down the pre-migration filesystem state the fake migration mutates."""
    roots.cfg_path.parent.mkdir(parents=True, exist_ok=True)
    roots.cfg_path.write_text(
        "# top\n"
        "version: 1\n"
        "# comment above old_key\n"
        "old_key: value  # eol\n"
        "tracked_files: {}\n",
        encoding="utf-8",
    )
    local = roots.home / ".config" / "setforge" / "local.yaml"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("source: /tmp/source\n", encoding="utf-8")
    tracked = roots.repo_root / "tracked" / "claude" / "CLAUDE.md"
    tracked.parent.mkdir(parents=True, exist_ok=True)
    tracked.write_text("This is a legacy marker.\n", encoding="utf-8")


def test_multi_file_migration_full_lifecycle(tmp_path: Path) -> None:
    """End-to-end: manifest + apply + per-file backup + rollback for THREE files.

    This is the spec's load-bearing "broadened-scope" assertion: a
    Migration touches setforge.yaml + local.yaml + a tracked file
    simultaneously, and every piece of the v0.2.0 Protocol surface
    works at multi-file granularity.
    """
    roots = MigrationRoots(
        cfg_path=tmp_path / "repo" / "setforge.yaml",
        repo_root=tmp_path / "repo",
        home=tmp_path / "home",
    )
    _seed_multi_file_fixture(roots)

    migration = _MultiFileMigration()

    # Manifest covers every file the apply step will touch.
    manifest = migration.manifest(roots=roots)
    manifest_paths = {entry.affected_path for entry in manifest}
    assert roots.cfg_path in manifest_paths
    assert roots.home / ".config" / "setforge" / "local.yaml" in manifest_paths
    assert roots.repo_root / "tracked" / "claude" / "CLAUDE.md" in manifest_paths

    # affected_paths matches the manifest's file set.
    paths = migration.affected_paths(roots=roots)
    assert set(paths) == manifest_paths

    # Snapshot pre-state for rollback.
    pre_snapshots: dict[Path, str] = {p: p.read_text(encoding="utf-8") for p in paths}

    # Per-file backup (APPLY_WITH_BACKUP semantics).
    for p in paths:
        backup = backup_path(p, migration.to_version)
        backup.write_bytes(p.read_bytes())

    # Apply the migration.
    migration.apply(roots=roots)

    # Verify each file was actually mutated.
    cfg_after = roots.cfg_path.read_text(encoding="utf-8")
    assert "new_key:" in cfg_after
    assert "old_key:" not in cfg_after
    # Comments survive the rename — research brief §4 invariant.
    assert "# comment above old_key" in cfg_after
    assert "# eol" in cfg_after

    local_after = (roots.home / ".config" / "setforge" / "local.yaml").read_text(
        encoding="utf-8"
    )
    assert "fresh_local_field" in local_after
    assert "added-by-migration" in local_after

    tracked_after = (roots.repo_root / "tracked" / "claude" / "CLAUDE.md").read_text(
        encoding="utf-8"
    )
    assert "migrated" in tracked_after
    assert "legacy" not in tracked_after

    # Every backup sibling exists and matches the pre-state bytes.
    for p, snapshot in pre_snapshots.items():
        backup = backup_path(p, migration.to_version)
        assert backup.exists(), f"missing backup for {p}"
        assert backup.read_text(encoding="utf-8") == snapshot

    # Rollback: restore each file from its sibling backup.
    for p in paths:
        backup = backup_path(p, migration.to_version)
        backup.replace(p)

    # State now identical to pre-migration.
    for p, snapshot in pre_snapshots.items():
        assert p.read_text(encoding="utf-8") == snapshot
