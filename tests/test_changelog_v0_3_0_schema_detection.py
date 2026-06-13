"""The shipped ``CHANGELOG.md`` declares the v0.3.0 schema bump detectably.

Guards the release-cut contract: ``setforge upgrade``'s schema-impact
assessment must classify a 0.3.0 upgrade as a DETECTED ``1.0 → 2.0`` bump
by reading the REAL ``CHANGELOG.md`` (not a fixture), so the canonical
``schema_version bumped 1.0 → 2.0`` line cannot silently drift out of the
released notes.
"""

from __future__ import annotations

from pathlib import Path

from setforge._changelog_parser import parse_changelog
from setforge.cli.upgrade import SchemaChangeKind, _assess_schema_change

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CHANGELOG = _REPO_ROOT / "CHANGELOG.md"


def test_real_changelog_marks_v0_3_0_as_detected_schema_bump() -> None:
    notes = parse_changelog(_CHANGELOG.read_text(encoding="utf-8"), "0.3.0")
    assert notes is not None, "CHANGELOG.md has no ## [0.3.0] section"

    out = _assess_schema_change(notes, current_schema="1.0", is_major_bump=True)

    assert out.kind is SchemaChangeKind.DETECTED
    assert out.from_schema == "1.0"
    assert out.to_schema == "2.0"
