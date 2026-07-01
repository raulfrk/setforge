"""Tests for the 2.1 -> 3.0 disposition-retire cutover migration.

Wave 1 pins the migration's identity + its one-way reverse refusal (the
collapse of disposition/spans into the binary SHARED/LOCAL store is lossy, so
the schema reverse must refuse and point at the transition-based recovery, NOT
the nonexistent ``migrate --revert`` — the d7728b6 bug this must not repeat).
The forward apply is built + tested in later waves.
"""

from __future__ import annotations

import pytest

from setforge.errors import ConfigError
from setforge.migrations import MigrationRoots
from setforge.migrations._disposition_retire import (
    DispositionRetireMigration,
    _DispositionRetireReverse,
)


def _roots(tmp_path):
    return MigrationRoots(
        cfg_path=tmp_path / "setforge.yaml", repo_root=tmp_path, home=tmp_path
    )


def test_forward_versions() -> None:
    m = DispositionRetireMigration()
    assert (m.from_version, m.to_version) == ("2.1", "3.0")


def test_reverse_is_swapped() -> None:
    rev = DispositionRetireMigration().reverse
    assert isinstance(rev, _DispositionRetireReverse)
    assert (rev.from_version, rev.to_version) == ("3.0", "2.1")
    # reverse-of-reverse is the forward again (Protocol symmetry).
    assert (rev.reverse.from_version, rev.reverse.to_version) == ("2.1", "3.0")


def test_reverse_refuses_cleanly(tmp_path) -> None:
    rev = _DispositionRetireReverse()
    with pytest.raises(ConfigError, match=r"setforge revert --profile=migrate"):
        rev.apply(roots=_roots(tmp_path))


def test_reverse_refusal_does_not_name_the_bogus_command(tmp_path) -> None:
    rev = _DispositionRetireReverse()
    with pytest.raises(ConfigError) as excinfo:
        rev.apply(roots=_roots(tmp_path))
    assert "migrate --revert" not in str(excinfo.value)


def test_reverse_manifest_is_note_only_and_touches_nothing(tmp_path) -> None:
    roots = _roots(tmp_path)
    rev = _DispositionRetireReverse()
    entries = rev.manifest(roots=roots)
    assert len(entries) == 1
    assert entries[0].affected_path == roots.cfg_path
    assert rev.affected_paths(roots=roots) == ()
