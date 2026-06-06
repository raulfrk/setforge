"""Tests for the standalone CI schema-gate script.

The script ``scripts/check_schema_gates.py`` runs three non-bypassable
gates in CI:

1. migration-coverage — ``current_expected_schema_version`` must have a
   bridging chain in ``MIGRATIONS`` (the empty-path / off-by-one trap is
   the load-bearing case here).
2. field-removal — the live schema must not drop a frozen field
   (reuses :func:`additivity_violations`).
3. reverse-required — every registered migration must carry a correctly
   swapped ``reverse`` (reuses :func:`_validate_registry`).

Each gate is factored into an importable function returning a list of
violation strings; ``main()`` is a thin exit wrapper. These tests drive
the gate functions directly, simulating each violation and asserting the
clean current tree passes.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.check_schema_gates import (
    gate_field_removal,
    gate_migration_coverage,
    gate_reverse_required,
    run_all_gates,
)
from setforge.errors import ConfigError
from setforge.migrations import (
    ManifestEntry,
    ManifestType,
    MigrationRoots,
)
from setforge.schema_manifest import FROZEN_FIELD_MANIFEST

# ---------------------------------------------------------------------------
# gate 1: migration-coverage
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class _Step:
    """Full Migration-Protocol impl for coverage-gate path simulation.

    Mirrors ``tests/test_migrations.py``'s ``_NoopMigration``: a complete
    Protocol surface so a tuple of these satisfies ``tuple[Migration, ...]``
    when monkeypatched onto ``setforge.migrations.MIGRATIONS``.
    """

    from_version: str
    to_version: str

    @property
    def reverse(self) -> _Step:
        return _Step(from_version=self.to_version, to_version=self.from_version)

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        return (ManifestEntry(type=ManifestType.NOTE, description="step"),)

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        return ()

    def apply(self, *, roots: MigrationRoots) -> None:
        return None


def test_migration_coverage_equal_version_passes() -> None:
    """The off-by-one trap: baseline == expected ⇒ empty path is VALID.

    ``find_migration_path`` returns ``()`` for BOTH "no bridge needed"
    and "unreachable"; the equal-version short-circuit must treat the
    empty path as a pass, not a failure. No registry monkeypatch is
    needed — the short-circuit returns before any path resolution.
    """
    assert gate_migration_coverage(baseline="1.1", expected="1.1") == []


def test_migration_coverage_real_bridged_bump_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine baseline→expected bump with a bridging chain passes."""
    monkeypatch.setattr(
        "setforge.migrations.MIGRATIONS",
        (_Step(from_version="1.0", to_version="1.1"),),
    )
    assert gate_migration_coverage(baseline="1.0", expected="1.1") == []


def test_migration_coverage_unbridged_bump_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``expected`` bumped beyond what ``MIGRATIONS`` bridges ⇒ failure."""
    monkeypatch.setattr(
        "setforge.migrations.MIGRATIONS",
        (_Step(from_version="1.0", to_version="1.1"),),
    )
    violations = gate_migration_coverage(baseline="1.0", expected="1.2")
    assert violations != []
    assert any("1.2" in v for v in violations)


def test_migration_coverage_partial_chain_not_reaching_target_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registry that bridges 1.0→1.2 but not to ``expected`` 1.3 fails.

    ``find_migration_path`` returns ``()`` here (1.3 unreachable), so this
    exercises the empty-path branch with a *multi-step* registry — distinct
    from ``unbridged_bump`` which has a single-step registry.
    """
    monkeypatch.setattr(
        "setforge.migrations.MIGRATIONS",
        (
            _Step(from_version="1.0", to_version="1.1"),
            _Step(from_version="1.1", to_version="1.2"),
        ),
    )
    violations = gate_migration_coverage(baseline="1.0", expected="1.3")
    assert violations != []


def test_migration_coverage_non_empty_path_ending_off_target_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defensive guard: a non-empty path whose last step misses ``expected``.

    ``find_migration_path``'s contract is to return a path that reaches the
    target or ``()``, so this branch is unreachable through the real walker.
    Monkeypatch it to return a chain ending at 1.1 while asking for 1.2 —
    confirming the last-step ``to_version`` guard fires rather than silently
    passing a non-empty-but-wrong path.
    """
    monkeypatch.setattr(
        "scripts.check_schema_gates.find_migration_path",
        lambda *, from_v, to_v: (_Step(from_version="1.0", to_version="1.1"),),
    )
    violations = gate_migration_coverage(baseline="1.0", expected="1.2")
    assert violations != []
    assert any("ends at" in v for v in violations)


def test_migration_coverage_semantic_not_lexical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1.9 → 1.10 bridged chain passes (semantic compare, not string sort)."""
    monkeypatch.setattr(
        "setforge.migrations.MIGRATIONS",
        (_Step(from_version="1.9", to_version="1.10"),),
    )
    assert gate_migration_coverage(baseline="1.9", expected="1.10") == []


def test_migration_coverage_malformed_version_raises() -> None:
    """A malformed version surfaces as ConfigError, never ValueError."""
    with pytest.raises(ConfigError, match="malformed schema_version"):
        gate_migration_coverage(baseline="1", expected="1.1")


def test_migration_coverage_real_tree_passes() -> None:
    """The live registry bridges its own baseline→expected (real tree)."""
    assert gate_migration_coverage() == []


# ---------------------------------------------------------------------------
# gate 2: field-removal
# ---------------------------------------------------------------------------


def test_field_removal_clean_tree_passes() -> None:
    """The shipped schema matches the frozen manifest (clean tree)."""
    assert gate_field_removal() == []


def test_field_removal_simulated_removal_fails() -> None:
    """Deleting a frozen field surfaces a violation (mirrors additivity test)."""
    live = copy.deepcopy(FROZEN_FIELD_MANIFEST)
    del live["Config"]["schema_version"]
    violations = gate_field_removal(frozen=FROZEN_FIELD_MANIFEST, live=live)
    assert any("Config.schema_version: field removed" in v for v in violations)


# ---------------------------------------------------------------------------
# gate 3: reverse-required
# ---------------------------------------------------------------------------


def test_reverse_required_clean_tree_passes() -> None:
    """The live registry's reverses are all correctly swapped (clean tree)."""
    assert gate_reverse_required() == []


def test_reverse_required_misswapped_reverse_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mis-swapped reverse ⇒ ``_validate_registry`` raises ⇒ gate fails."""

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
    violations = gate_reverse_required()
    assert any("mis-swapped reverse" in v for v in violations)


# ---------------------------------------------------------------------------
# run_all_gates — aggregate
# ---------------------------------------------------------------------------


def test_run_all_gates_clean_tree_passes() -> None:
    """All three gates pass on the real current tree."""
    assert run_all_gates() == []
