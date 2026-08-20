"""Keep the safe-adoption decision and lane inventory closed and reviewable."""

from __future__ import annotations

from pathlib import Path

_RFC = Path(__file__).parents[1] / "docs" / "rfcs" / "0002-safe-adoption.md"


def _table_rows(heading: str) -> list[list[str]]:
    text = _RFC.read_text(encoding="utf-8")
    section = text.split(f"## {heading}\n", 1)[1].split("\n## ", 1)[0]
    rows = []
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells[0] in {"Dimension", "Lane"} or set(cells[0]) == {"-"}:
            continue
        rows.append(cells)
    return rows


def test_adoption_dimensions_are_independent_and_closed() -> None:
    rows = _table_rows("Independent dimensions")

    assert [row[0] for row in rows] == [
        "Current authority",
        "Claim lifecycle",
        "Declaration residence",
        "Resolution binding",
        "Provenance evidence",
    ]
    assert rows[0][1] == "`none`, `manage`"
    assert rows[1][1] == "no claim, `claimed`, `released`"
    assert rows[2][1] == "`shared`, `host-local`"
    assert rows[3][1] == "`portable`, `host-bound`, `instance-bound`"
    assert rows[4][1] == "structured evidence record"


def test_every_adoption_lane_has_a_verification_boundary() -> None:
    rows = _table_rows("Implementation and verification matrix")

    assert [row[0] for row in rows] == [
        "Existing files and owned regions",
        "Generated resources and host-resolved inputs",
        "Multi-target application capability graphs",
        "Durable identity and collision management",
        "Directory trees",
        "Platform release assets",
        "Package provenance lifecycle",
    ]
    assert all(len(row) == 5 for row in rows)
    assert all(all(cell for cell in row[1:]) for row in rows)


def test_adoption_never_implies_immediate_resource_mutation() -> None:
    text = _RFC.read_text(encoding="utf-8")

    assert "Adoption is a metadata-only compare-and-swap operation" in text
    assert "Release sets current authority to `none`" in text
    assert "leaves the resource intact" in text
    assert "legacy-unverified" in text
    assert "current `manage` grant" in text
    assert "Existing tracked-file reconcile stores are also dual-read" in text
    assert "ownership adoption is metadata-only" in text
    assert "not one exclusive enum" in text
    assert "whether SetForge installed the" in text
    assert "installation operation's separately journaled" in text


def test_mixed_files_and_reverse_authority_fail_closed() -> None:
    text = _RFC.read_text(encoding="utf-8")

    assert "The container claim never turns LOCAL bytes" in text
    assert "classification alone never creates or transfers that claim" in text
    assert "Whole-file replacement or deletion refuses" in text
    assert "Restoring a ledger snapshot alone never" in text
    assert "removal are blocked until" in text
    assert "does not change when a missing root is created" in text
    assert "realpath is locator and guard evidence" in text
    assert "Simultaneous first use from multiple" in text
