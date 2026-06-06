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
from typing import Final

import pytest

from setforge.config import load_config
from setforge.errors import ConfigError
from setforge.migrations import (
    MIGRATIONS,
    ManifestEntry,
    ManifestType,
    Migration,
    MigrationRoots,
    RestampMigration,
    VersionStampMigration,
    current_expected_schema_version,
    detect_current_schema,
    find_migration_path,
)
from setforge.migrations._fs_ops import atomic_replace, backup_path
from setforge.migrations._yaml_ops import atomic_write_yaml, rename_key, yaml_rt

# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------


def test_current_expected_schema_version_is_one_two() -> None:
    """The build now expects schema 1.2 after the second expand migration."""
    assert current_expected_schema_version == "1.2"


def test_migrations_registry_has_the_version_stamp_chain() -> None:
    """The registry ships the 1.0 → 1.1 → 1.2 version-stamp chain, in order."""
    assert len(MIGRATIONS) == 2
    assert (MIGRATIONS[0].from_version, MIGRATIONS[0].to_version) == ("1.0", "1.1")
    assert (MIGRATIONS[1].from_version, MIGRATIONS[1].to_version) == ("1.1", "1.2")
    # Appended in from_version order so the forward walk never has to sort.
    assert isinstance(MIGRATIONS[1], RestampMigration)


def test_find_migration_path_empty_registry_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The empty-registry path still returns () (registry forced empty)."""
    monkeypatch.setattr("setforge.migrations.MIGRATIONS", ())
    assert find_migration_path(from_v="1.0", to_v="1.1") == ()


def test_find_migration_path_same_version_returns_empty() -> None:
    assert find_migration_path(from_v="1.0", to_v="1.0") == ()


# ---------------------------------------------------------------------------
# parse_schema_version — semantic, guarded
# ---------------------------------------------------------------------------


def test_parse_schema_version_returns_int_tuple() -> None:
    from setforge.migrations import parse_schema_version

    assert parse_schema_version("1.0") == (1, 0)
    assert parse_schema_version("2.13") == (2, 13)


def test_parse_schema_version_is_semantic_not_lexical() -> None:
    """1.10 must sort ABOVE 1.9 — the bug a string compare gets wrong."""
    from setforge.migrations import parse_schema_version

    assert parse_schema_version("1.10") > parse_schema_version("1.9")
    assert "1.10" < "1.9"  # the lexical trap this guards against


@pytest.mark.parametrize("bad", ["1", "1.2.3", "", "v2", "2.0.0", "1.x", "1."])
def test_parse_schema_version_rejects_malformed_cleanly(bad: str) -> None:
    """Malformed versions raise ConfigError, never ValueError/IndexError."""
    from setforge.migrations import parse_schema_version

    with pytest.raises(ConfigError, match="malformed schema_version"):
        parse_schema_version(bad)


# ---------------------------------------------------------------------------
# reverse + registry guard
# ---------------------------------------------------------------------------


def test_version_stamp_reverse_is_swapped() -> None:
    fwd = VersionStampMigration()
    rev = fwd.reverse
    assert (rev.from_version, rev.to_version) == (fwd.to_version, fwd.from_version)
    # reverse-of-reverse is the original forward direction (Protocol symmetric)
    assert (rev.reverse.from_version, rev.reverse.to_version) == (
        fwd.from_version,
        fwd.to_version,
    )


def test_every_registered_migration_has_a_swapped_reverse() -> None:
    """The contract the import-time guard enforces, asserted explicitly."""
    for migration in MIGRATIONS:
        rev = migration.reverse
        assert rev.from_version == migration.to_version
        assert rev.to_version == migration.from_version


def test_registry_guard_rejects_missing_or_misswapped_reverse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mis-swapped reverse fails loudly at the guard, not at downgrade time."""
    from setforge.migrations import _validate_registry

    @dataclass(frozen=True)
    class _BadReverse:
        from_version: str = "1.1"
        to_version: str = "9.9"  # NOT the swap of 1.0->1.1

    @dataclass(frozen=True)
    class _BadMigration:
        from_version: str = "1.0"
        to_version: str = "1.1"

        @property
        def reverse(self) -> _BadReverse:
            return _BadReverse()

    monkeypatch.setattr("setforge.migrations.MIGRATIONS", (_BadMigration(),))
    with pytest.raises(ConfigError, match="mis-swapped reverse"):
        _validate_registry()


# ---------------------------------------------------------------------------
# bidirectional find_migration_path
# ---------------------------------------------------------------------------


def test_find_migration_path_reverse_one_step() -> None:
    """1.1 -> 1.0 walks the registered migration's .reverse."""
    chain = find_migration_path(from_v="1.1", to_v="1.0")
    assert len(chain) == 1
    assert (chain[0].from_version, chain[0].to_version) == ("1.1", "1.0")


def test_find_migration_path_unreachable_target_returns_empty() -> None:
    """A target no chain reaches terminates with () — never hangs."""
    assert find_migration_path(from_v="1.1", to_v="0.9") == ()
    assert find_migration_path(from_v="1.0", to_v="5.0") == ()


def test_find_migration_path_malformed_version_raises_configerror() -> None:
    with pytest.raises(ConfigError, match="malformed schema_version"):
        find_migration_path(from_v="1.0", to_v="2")


def test_find_migration_path_semantic_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 1.9 -> 1.10 step resolves forward (semantic), not backward (lexical)."""

    @dataclass(frozen=True)
    class _Rev:
        from_version: str = "1.10"
        to_version: str = "1.9"

        @property
        def reverse(self) -> _Step:
            return _Step()

    @dataclass(frozen=True)
    class _Step:
        from_version: str = "1.9"
        to_version: str = "1.10"

        @property
        def reverse(self) -> _Rev:
            return _Rev()

    monkeypatch.setattr("setforge.migrations.MIGRATIONS", (_Step(),))
    fwd = find_migration_path(from_v="1.9", to_v="1.10")
    assert len(fwd) == 1
    assert fwd[0].to_version == "1.10"
    rev = find_migration_path(from_v="1.10", to_v="1.9")
    assert len(rev) == 1
    assert rev[0].to_version == "1.9"


def test_known_versions_covers_registry_and_expected() -> None:
    from setforge.migrations import known_versions

    kv = known_versions()
    assert "1.0" in kv
    assert "1.1" in kv
    assert "1.2" in kv


def test_atomic_write_yaml_preserves_file_mode(tmp_path: Path) -> None:
    """A migrate write must not narrow a 0644 config to mkstemp's 0600."""
    import stat

    p = tmp_path / "setforge.yaml"
    p.write_text("a: 1\n", encoding="utf-8")
    p.chmod(0o644)
    data = yaml_rt().load("a: 2\n")
    atomic_write_yaml(p, data)
    assert stat.S_IMODE(p.stat().st_mode) == 0o644


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

    @property
    def reverse(self) -> _NoopMigration:
        return _NoopMigration(
            from_version=self.to_version, to_version=self.from_version
        )

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


# ---------------------------------------------------------------------------
# First real migration — version-stamp 1.0 → 1.1 (+ reverse). B-M1…B-M8.
# ---------------------------------------------------------------------------

_CFG_BODY_NO_VERSION: Final[str] = (
    "# top comment\n"
    "version: 1\n"
    "tracked_files:\n"
    "  foo:\n"
    "    src: foo.md  # eol comment\n"
    "    dst: foo.md\n"
    "profiles:\n"
    "  base:\n"
    "    tracked_files:\n"
    "      - foo\n"
)


def _roots_for(cfg_path: Path) -> MigrationRoots:
    return MigrationRoots(
        cfg_path=cfg_path,
        repo_root=cfg_path.resolve().parent,
        home=cfg_path.resolve().parent / "home",
    )


def _seed_cfg(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(body, encoding="utf-8")
    return cfg


def test_version_stamp_migration_is_registered() -> None:
    """The 1.0→1.1 step is the FIRST registered migration (a VersionStampMigration)."""
    assert MIGRATIONS[0] == VersionStampMigration()
    assert isinstance(MIGRATIONS[0], Migration)


def test_version_stamp_apply_stamps_schema_version(tmp_path: Path) -> None:
    """``apply`` writes ``schema_version: '1.1'`` into setforge.yaml."""
    cfg = _seed_cfg(tmp_path, _CFG_BODY_NO_VERSION)
    VersionStampMigration().apply(roots=_roots_for(cfg))
    assert detect_current_schema(cfg) == "1.1"


def test_version_stamp_apply_is_identity_on_data(tmp_path: Path) -> None:
    """Apply touches ONLY schema_version — every other key/comment survives."""
    cfg = _seed_cfg(tmp_path, _CFG_BODY_NO_VERSION)
    VersionStampMigration().apply(roots=_roots_for(cfg))
    after = cfg.read_text(encoding="utf-8")
    assert "# top comment" in after
    assert "# eol comment" in after
    assert "version: 1" in after
    assert "- foo" in after
    # The only new top-level key is schema_version.
    yaml = yaml_rt()
    with cfg.open("r", encoding="utf-8") as fh:
        data = yaml.load(fh)
    assert data["schema_version"] == "1.1"
    assert set(data.keys()) == {
        "version",
        "schema_version",
        "tracked_files",
        "profiles",
    }


def test_version_stamp_apply_idempotent_on_replay(tmp_path: Path) -> None:
    """B-M2: applying twice equals applying once (overwrite-or-insert)."""
    cfg = _seed_cfg(tmp_path, _CFG_BODY_NO_VERSION)
    roots = _roots_for(cfg)
    VersionStampMigration().apply(roots=roots)
    once = cfg.read_text(encoding="utf-8")
    VersionStampMigration().apply(roots=roots)
    twice = cfg.read_text(encoding="utf-8")
    assert once == twice
    assert detect_current_schema(cfg) == "1.1"


def test_version_stamp_apply_overwrites_present_key(tmp_path: Path) -> None:
    """B-M2: a stale schema_version is overwritten, not duplicated or raised on."""
    cfg = _seed_cfg(tmp_path, "schema_version: '0.9'\n" + _CFG_BODY_NO_VERSION)
    VersionStampMigration().apply(roots=_roots_for(cfg))
    assert detect_current_schema(cfg) == "1.1"
    assert cfg.read_text(encoding="utf-8").count("schema_version") == 1


def test_version_stamp_apply_single_atomic_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B-M8: exactly one ``atomic_write_yaml`` per ``apply`` (no two-write skew)."""
    cfg = _seed_cfg(tmp_path, _CFG_BODY_NO_VERSION)
    import setforge.migrations as _mig

    calls: list[Path] = []
    real = _mig.atomic_write_yaml

    def _spy(path: Path, data: object) -> None:
        calls.append(path)
        real(path, data)

    monkeypatch.setattr("setforge.migrations.atomic_write_yaml", _spy)
    VersionStampMigration().apply(roots=_roots_for(cfg))
    assert calls == [cfg]


def test_version_stamp_manifest_and_affected_paths(tmp_path: Path) -> None:
    """manifest()/affected_paths() describe exactly the single-file stamp."""
    cfg = _seed_cfg(tmp_path, _CFG_BODY_NO_VERSION)
    roots = _roots_for(cfg)
    migration = VersionStampMigration()
    assert migration.affected_paths(roots=roots) == (cfg,)
    manifest = migration.manifest(roots=roots)
    assert len(manifest) == 1
    (entry,) = manifest
    assert entry.affected_path == cfg
    assert "schema_version" in entry.description


def test_reverse_strips_stamp_restoring_absence_when_originally_absent(
    tmp_path: Path,
) -> None:
    """B-M1: down→up→down on a key-ABSENT config restores absence byte-identically.

    The reverse simply removes the schema_version key. Because up→down on a
    key-absent config removes the very key the up inserted, the down→up→down
    cycle returns to the post-first-down bytes (a ruamel round-trip
    normalizes the hand-written source on the very first load→dump; what
    matters for byte-identity is that up→down adds-then-removes nothing
    net, and that the key absence is restored).
    """
    cfg = _seed_cfg(tmp_path, _CFG_BODY_NO_VERSION)
    roots = _roots_for(cfg)
    migration = VersionStampMigration()
    reverse = migration.reverse

    # First down (no-op strip on an absent key) normalizes the document.
    reverse.apply(roots=roots)
    normalized = cfg.read_bytes()
    assert b"schema_version" not in normalized

    # up (stamp) → down (strip) must return to the normalized bytes.
    migration.apply(roots=roots)
    reverse.apply(roots=roots)

    assert cfg.read_bytes() == normalized
    # Absence restored — detect_current_schema falls back to the 1.0 baseline.
    assert detect_current_schema(cfg) == "1.0"


def test_reverse_restores_value_when_key_present(tmp_path: Path) -> None:
    """B-M1: on a key-PRESENT config, the reverse leaves no schema_version key.

    A config that already declared a schema_version (e.g. a downgraded 1.1)
    has its stamp stripped by the reverse; re-applying up + down round-trips
    cleanly because the down removes whatever the up wrote.
    """
    cfg = _seed_cfg(tmp_path, "schema_version: '1.1'\n" + _CFG_BODY_NO_VERSION)
    roots = _roots_for(cfg)
    reverse = VersionStampMigration().reverse
    reverse.apply(roots=roots)
    yaml = yaml_rt()
    with cfg.open("r", encoding="utf-8") as fh:
        data = yaml.load(fh)
    assert "schema_version" not in data
    assert detect_current_schema(cfg) == "1.0"


def test_reverse_is_not_in_forward_registry() -> None:
    """The reverse is NOT a forward MIGRATIONS entry — no 1.0↔1.1 cycle."""
    assert all(m.from_version != "1.1" or m.to_version != "1.0" for m in MIGRATIONS)
    reverse = VersionStampMigration().reverse
    assert reverse.from_version == "1.1"
    assert reverse.to_version == "1.0"


def test_find_migration_path_one_step_no_loop() -> None:
    """B-M4: 1.0 → 1.1 resolves to exactly one step and does not loop."""
    found = find_migration_path(from_v="1.0", to_v="1.1")
    assert len(found) == 1
    assert found[0].from_version == "1.0"
    assert found[0].to_version == "1.1"


def test_find_migration_path_future_sibling_does_not_perturb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-M4: appending a future 1.2 → 2.0 entry leaves 1.0 → 1.1 intact.

    The sibling extends the END of the real 1.0 → 1.1 → 1.2 chain (its
    from_version is 1.2, not 1.1 — appending from 1.1 would collide with
    the registered 1.1 → 1.2 step and create an ambiguous branch).
    """
    from setforge.migrations import MIGRATIONS as _real

    extended = (*_real, _NoopMigration(from_version="1.2", to_version="2.0"))
    monkeypatch.setattr("setforge.migrations.MIGRATIONS", extended)
    found = find_migration_path(from_v="1.0", to_v="1.1")
    assert len(found) == 1
    assert found[0].to_version == "1.1"
    # And the longer chain still resolves end-to-end without looping.
    full = find_migration_path(from_v="1.0", to_v="2.0")
    assert tuple(m.to_version for m in full) == ("1.1", "1.2", "2.0")


# ---------------------------------------------------------------------------
# Config-load interaction — B-M3 (extra=forbid), B-M6 (mismatch warning),
# B-M7 (frozen 1.0 fixture still loads).
# ---------------------------------------------------------------------------

_LOADABLE_CFG: Final[str] = (
    "version: 1\n"
    "tracked_files:\n"
    "  foo:\n"
    "    src: foo.md\n"
    "    dst: foo.md\n"
    "profiles:\n"
    "  base:\n"
    "    tracked_files:\n"
    "      - foo\n"
)


def test_post_migration_config_loads_under_extra_forbid(tmp_path: Path) -> None:
    """B-M3: a stamped schema_version: '1.1' config loads with no ValidationError."""
    cfg = _seed_cfg(tmp_path, _LOADABLE_CFG)
    VersionStampMigration().apply(roots=_roots_for(cfg))
    config = load_config(cfg)
    assert config.schema_version == "1.1"


def test_frozen_1_0_fixture_still_loads(tmp_path: Path) -> None:
    """B-M7: a frozen 1.0 fixture (no schema_version key) still loads."""
    cfg = _seed_cfg(tmp_path, _LOADABLE_CFG)
    config = load_config(cfg)
    # Absent key defaults to the 1.0 baseline.
    assert config.schema_version == "1.0"


def test_unmigrated_1_0_config_warns_once_non_fatal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """B-M6: a 1.0-default config emits exactly one non-fatal mismatch warning."""
    cfg = _seed_cfg(tmp_path, _LOADABLE_CFG)
    config = load_config(cfg)  # must NOT raise
    assert config.schema_version == "1.0"
    captured = capsys.readouterr()
    assert captured.err.count("warning:") == 1
    assert "schema_version" in captured.err
    assert "1.2" in captured.err


# ---------------------------------------------------------------------------
# Trust-boundary shape validation — a hand-edited non-mapping root raises a
# domain ConfigError, not a bare TypeError / AttributeError.
# ---------------------------------------------------------------------------

_NON_MAPPING_ROOTS: Final[tuple[str, ...]] = (
    "- one\n- two\n",  # YAML sequence root
    "just-a-scalar\n",  # bare scalar root
)


@pytest.mark.parametrize("body", _NON_MAPPING_ROOTS)
def test_apply_non_mapping_root_raises_config_error(tmp_path: Path, body: str) -> None:
    """``apply`` on a non-mapping setforge.yaml raises ConfigError, not TypeError."""
    cfg = _seed_cfg(tmp_path, body)
    with pytest.raises(ConfigError):
        VersionStampMigration().apply(roots=_roots_for(cfg))


@pytest.mark.parametrize("body", _NON_MAPPING_ROOTS)
def test_reverse_non_mapping_root_raises_config_error(
    tmp_path: Path, body: str
) -> None:
    """The reverse on a non-mapping root raises ConfigError, not TypeError."""
    cfg = _seed_cfg(tmp_path, body)
    with pytest.raises(ConfigError):
        VersionStampMigration().reverse.apply(roots=_roots_for(cfg))


@pytest.mark.parametrize("body", _NON_MAPPING_ROOTS)
def test_detect_current_schema_non_mapping_root_raises_config_error(
    tmp_path: Path, body: str
) -> None:
    """``detect_current_schema`` on a non-mapping root raises ConfigError."""
    cfg = _seed_cfg(tmp_path, body)
    with pytest.raises(ConfigError):
        detect_current_schema(cfg)


# ---------------------------------------------------------------------------
# Second real migration — restamp 1.1 → 1.2 (+ symmetric reverse). 14.13.
#
# RestampMigration differs from VersionStampMigration in the load-bearing
# way: its reverse RESTAMPS the older version (overwrite-in-place) rather
# than STRIPPING the key. The 1.0 endpoint is key-absent (so the 1.0↔1.1
# reverse strips); 1.1 and 1.2 both carry the key (so the 1.1↔1.2 reverse
# must restamp, never delete — else a downgrade silently reads as 1.0).
# ---------------------------------------------------------------------------

# A config that ALREADY carries schema_version, with keys BEFORE and AFTER
# it. The mid-document position is what makes the byte-identity test a real
# reorder proof: a del-then-reinsert would move the key to the document end.
_CFG_BODY_AT_1_1: Final[str] = (
    "# top comment\n"
    "version: 1\n"
    "schema_version: '1.1'\n"
    "tracked_files:\n"
    "  foo:\n"
    "    src: foo.md  # eol comment\n"
    "    dst: foo.md\n"
    "profiles:\n"
    "  base:\n"
    "    tracked_files:\n"
    "      - foo\n"
)

_RESTAMP_1_1_TO_1_2: Final = RestampMigration(from_version="1.1", to_version="1.2")


def test_restamp_satisfies_migration_protocol() -> None:
    assert isinstance(_RESTAMP_1_1_TO_1_2, Migration)


def test_restamp_reverse_is_swapped_and_symmetric() -> None:
    """The reverse swaps from/to and is itself a (symmetric) RestampMigration."""
    rev = _RESTAMP_1_1_TO_1_2.reverse
    assert isinstance(rev, RestampMigration)
    assert (rev.from_version, rev.to_version) == ("1.2", "1.1")
    # reverse-of-reverse is the original forward direction.
    assert (rev.reverse.from_version, rev.reverse.to_version) == ("1.1", "1.2")


def test_restamp_apply_stamps_in_place_preserving_order(tmp_path: Path) -> None:
    """``apply`` overwrites schema_version in place — key position unchanged."""
    cfg = _seed_cfg(tmp_path, _CFG_BODY_AT_1_1)
    yaml = yaml_rt()
    before_keys = list(yaml.load(_CFG_BODY_AT_1_1).keys())

    _RESTAMP_1_1_TO_1_2.apply(roots=_roots_for(cfg))

    assert detect_current_schema(cfg) == "1.2"
    after = cfg.read_text(encoding="utf-8")
    # Identity on every other key + comment.
    assert "# top comment" in after
    assert "# eol comment" in after
    assert "- foo" in after
    with cfg.open("r", encoding="utf-8") as fh:
        data = yaml.load(fh)
    # The key kept its slot (version, schema_version, tracked_files, profiles)
    # — a del + reinsert would have shoved schema_version to the end.
    assert list(data.keys()) == before_keys
    assert data["schema_version"] == "1.2"


def test_restamp_reverse_restamps_older_version_not_strips(tmp_path: Path) -> None:
    """The load-bearing assertion: a 1.2 → 1.1 reverse leaves '1.1', NOT absence.

    Reusing VersionStampMigration here would DELETE the key, and
    detect_current_schema would then misread the result as the 1.0
    baseline — a silent two-version downgrade. The restamp must keep the
    key present at the older value.
    """
    cfg = _seed_cfg(tmp_path, "schema_version: '1.2'\n" + _CFG_BODY_NO_VERSION)
    _RESTAMP_1_1_TO_1_2.reverse.apply(roots=_roots_for(cfg))
    yaml = yaml_rt()
    with cfg.open("r", encoding="utf-8") as fh:
        data = yaml.load(fh)
    assert "schema_version" in data  # NOT stripped
    assert data["schema_version"] == "1.1"
    assert detect_current_schema(cfg) == "1.1"  # NOT "1.0"


def test_restamp_up_down_up_is_byte_identical(tmp_path: Path) -> None:
    """up → down → up on a key-present config is byte-identical + reorder-safe.

    The pitfall this guards (14.13): the existing VersionStampMigration
    round-trip test only covers the down → up → down key-ABSENT cycle. A
    config that already carries schema_version with keys after it can be
    reordered by a del + reinsert. Overwrite-in-place must not.

    Baseline is the post-first-up document (ruamel normalizes the
    hand-written source on the first load → dump), mirroring
    test_reverse_strips_stamp_restoring_absence_when_originally_absent.
    """
    cfg = _seed_cfg(tmp_path, _CFG_BODY_AT_1_1)
    roots = _roots_for(cfg)
    fwd = _RESTAMP_1_1_TO_1_2
    rev = fwd.reverse

    fwd.apply(roots=roots)  # 1.1 → 1.2 (normalizes + stamps)
    after_first_up = cfg.read_bytes()

    rev.apply(roots=roots)  # 1.2 → 1.1
    fwd.apply(roots=roots)  # 1.1 → 1.2 again

    assert cfg.read_bytes() == after_first_up
    assert detect_current_schema(cfg) == "1.2"


def test_restamp_apply_idempotent_on_replay(tmp_path: Path) -> None:
    """Applying the restamp twice equals applying it once (overwrite-or-insert)."""
    cfg = _seed_cfg(tmp_path, _CFG_BODY_AT_1_1)
    roots = _roots_for(cfg)
    _RESTAMP_1_1_TO_1_2.apply(roots=roots)
    once = cfg.read_bytes()
    _RESTAMP_1_1_TO_1_2.apply(roots=roots)
    assert cfg.read_bytes() == once
    assert detect_current_schema(cfg) == "1.2"


def test_restamp_manifest_and_affected_paths(tmp_path: Path) -> None:
    """manifest()/affected_paths() describe exactly the single-file in-place stamp."""
    cfg = _seed_cfg(tmp_path, _CFG_BODY_AT_1_1)
    roots = _roots_for(cfg)
    assert _RESTAMP_1_1_TO_1_2.affected_paths(roots=roots) == (cfg,)
    (entry,) = _RESTAMP_1_1_TO_1_2.manifest(roots=roots)
    assert entry.type is ManifestType.EDIT
    assert entry.affected_path == cfg
    assert "schema_version" in entry.description


def test_two_step_chain_applies_1_0_to_1_2_and_reverses_to_absent(
    tmp_path: Path,
) -> None:
    """The REAL registry chain applies 1.0 → 1.2 forward, then 1.2 → 1.0 back.

    Drives the production find_migration_path output (not a hand-built
    chain): the forward walk stamps through both steps to 1.2; the reverse
    walk restamps to 1.1 then strips to the key-absent 1.0 baseline.
    """
    cfg = _seed_cfg(tmp_path, _CFG_BODY_NO_VERSION)  # key-absent 1.0
    roots = _roots_for(cfg)

    forward = find_migration_path(from_v="1.0", to_v="1.2")
    assert tuple(m.to_version for m in forward) == ("1.1", "1.2")
    for migration in forward:
        migration.apply(roots=roots)
    assert detect_current_schema(cfg) == "1.2"

    backward = find_migration_path(from_v="1.2", to_v="1.0")
    assert tuple(m.to_version for m in backward) == ("1.1", "1.0")
    for migration in backward:
        migration.apply(roots=roots)
    # Back to the 1.0 baseline: the key is absent again.
    yaml = yaml_rt()
    with cfg.open("r", encoding="utf-8") as fh:
        data = yaml.load(fh)
    assert "schema_version" not in data
    assert detect_current_schema(cfg) == "1.0"


@pytest.mark.parametrize("body", _NON_MAPPING_ROOTS)
def test_restamp_apply_non_mapping_root_raises_config_error(
    tmp_path: Path, body: str
) -> None:
    """``RestampMigration.apply`` on a non-mapping root raises ConfigError."""
    cfg = _seed_cfg(tmp_path, body)
    with pytest.raises(ConfigError):
        _RESTAMP_1_1_TO_1_2.apply(roots=_roots_for(cfg))


@pytest.mark.parametrize("body", _NON_MAPPING_ROOTS)
def test_restamp_reverse_non_mapping_root_raises_config_error(
    tmp_path: Path, body: str
) -> None:
    """The restamp reverse on a non-mapping root raises ConfigError, not TypeError."""
    cfg = _seed_cfg(tmp_path, body)
    with pytest.raises(ConfigError):
        _RESTAMP_1_1_TO_1_2.reverse.apply(roots=_roots_for(cfg))
