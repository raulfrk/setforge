"""Fast parity check between tracked/claude/ and tests/fixtures/e2e/tracked/claude/.

The Docker e2e suite assumes the fixture mirror is identical to the
source-of-truth tracked/claude/ tree. When tracked/claude/ gains a file,
the mirror must follow. This test fails fast in plain `pytest` so drift
surfaces before the slow e2e_docker run.
"""

from __future__ import annotations

import filecmp
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "tracked" / "claude"
MIRROR = REPO_ROOT / "tests" / "fixtures" / "e2e" / "tracked" / "claude"


def _gather_files(root: Path) -> set[Path]:
    return {p.relative_to(root) for p in root.rglob("*") if p.is_file()}


def test_fixture_mirror_has_same_file_set() -> None:
    """Every file under tracked/claude/ has a counterpart in the mirror."""
    src_files = _gather_files(SRC)
    mirror_files = _gather_files(MIRROR)
    missing_in_mirror = src_files - mirror_files
    extra_in_mirror = mirror_files - src_files
    assert not missing_in_mirror, f"missing in mirror: {sorted(missing_in_mirror)}"
    assert not extra_in_mirror, f"extra in mirror: {sorted(extra_in_mirror)}"


def test_fixture_mirror_byte_identical() -> None:
    """Every mirrored file matches its source byte-for-byte."""
    src_files = _gather_files(SRC)
    differences = [
        rel
        for rel in src_files
        if (MIRROR / rel).exists()
        and not filecmp.cmp(SRC / rel, MIRROR / rel, shallow=False)
    ]
    assert not differences, f"content drift in mirror: {sorted(differences)}"
