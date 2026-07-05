"""Tests for the 3.0 -> 4.0 span-surface-retire migration SKELETON.

Task C1 stands up the migration class + registers it + bumps the expected
schema version so :func:`find_migration_path` resolves 3.0 -> 4.0. The heavy
fold/strip ``apply`` body is a SEPARATE later task, so these tests pin ONLY the
identity / registration surface and deliberately do NOT call ``apply`` (the
skeleton raises ``NotImplementedError``).
"""

from __future__ import annotations

import pytest

from setforge.errors import ConfigError
from setforge.migrations import (
    MigrationRoots,
    current_expected_schema_version,
    find_migration_path,
    parse_schema_version,
)
from setforge.migrations._span_surface_retire import (
    SpanSurfaceRetireMigration,
    _SpanSurfaceRetireReverse,
)


def _roots(tmp_path):
    return MigrationRoots(
        cfg_path=tmp_path / "setforge.yaml", repo_root=tmp_path, home=tmp_path
    )


def test_expected_version_is_four_zero() -> None:
    """The build now expects schema 4.0 after the span-surface-retire bump."""
    assert current_expected_schema_version == "4.0"


def test_parse_four_zero() -> None:
    assert parse_schema_version("4.0") == (4, 0)


def test_find_migration_path_three_zero_to_four_zero_is_one_step() -> None:
    """3.0 -> 4.0 resolves to a single registered migration."""
    chain = find_migration_path(from_v="3.0", to_v="4.0")
    assert len(chain) == 1
    assert (chain[0].from_version, chain[0].to_version) == ("3.0", "4.0")


def test_forward_versions() -> None:
    m = SpanSurfaceRetireMigration()
    assert (m.from_version, m.to_version) == ("3.0", "4.0")
    assert m.writes_own_transition is True


def test_reverse_is_swapped() -> None:
    rev = SpanSurfaceRetireMigration().reverse
    assert isinstance(rev, _SpanSurfaceRetireReverse)
    assert (rev.from_version, rev.to_version) == ("4.0", "3.0")
    # reverse-of-reverse is the forward again (Protocol symmetry).
    assert (rev.reverse.from_version, rev.reverse.to_version) == ("3.0", "4.0")


def test_forward_exposes_manifest_and_affected_paths(tmp_path) -> None:
    """manifest()/affected_paths() are callable and describe the footprint."""
    roots = _roots(tmp_path)
    m = SpanSurfaceRetireMigration()
    manifest = m.manifest(roots=roots)
    assert isinstance(manifest, tuple)
    # The schema stamp is always in the footprint.
    assert any(e.affected_path == roots.cfg_path for e in manifest)
    paths = m.affected_paths(roots=roots)
    assert isinstance(paths, tuple)
    assert roots.cfg_path in paths


def test_reverse_manifest_is_note_only_and_touches_nothing(tmp_path) -> None:
    roots = _roots(tmp_path)
    rev = _SpanSurfaceRetireReverse()
    entries = rev.manifest(roots=roots)
    assert len(entries) == 1
    assert entries[0].affected_path == roots.cfg_path
    assert rev.affected_paths(roots=roots) == ()


def test_reverse_refuses_cleanly(tmp_path) -> None:
    """The schema reverse is lossy — it refuses and points at the transition."""
    rev = _SpanSurfaceRetireReverse()
    with pytest.raises(ConfigError, match=r"setforge revert --profile=migrate"):
        rev.apply(roots=_roots(tmp_path))


def test_forward_apply_is_not_yet_implemented(tmp_path) -> None:
    """The fold/strip body lands in a later task; the skeleton refuses to run."""
    with pytest.raises(NotImplementedError):
        SpanSurfaceRetireMigration().apply(roots=_roots(tmp_path))
