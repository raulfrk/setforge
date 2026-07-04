"""Tests for the reconcile-store host-local section projection (task B1).

:func:`host_local_sections_from_store` reads the host-local markdown sections
back OUT of the reconcile per-unit store (LOCAL line units carrying a
``reloc_anchor`` heading identity) in the same shape
:func:`setforge.source.load_local_host_local_sections` produced, so the legacy
consumers can be repointed at the store (task D2).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from setforge.anchors import AnchorAfterHeading
from setforge.reconcile import store
from setforge.reconcile.host_local_view import host_local_sections_from_store
from setforge.reconcile.hunks import Hunk, extract_hunks, serialize
from setforge.reconcile.types import HunkClass, file_id
from setforge.source import HostLocalSection, HostLocalSectionName

# A host-local ADDITIVE "## My Tweaks" section spliced between two base headings.
BASE = b"## Alpha\naaa\n## Beta\nbbb\n"
LOCAL = b"## Alpha\naaa\n## My Tweaks\nmy custom line\n## Beta\nbbb\n"


def _hunk(base: bytes, local: bytes, label: str) -> Hunk:
    """The single extracted base->local hunk whose label matches ``label``."""
    return next(h for h in extract_hunks(base, local) if h.label == label)


def test_projects_local_reloc_section(tmp_state: Path) -> None:
    # A LOCAL unit carrying a minted reloc_anchor projects to a HostLocalSection
    # with the heading name, an after-heading anchor, and the region body bytes.
    fid = file_id("claude/CLAUDE.md")
    rows = serialize([replace(_hunk(BASE, LOCAL, "## My Tweaks"), cls=HunkClass.LOCAL)])
    assert rows[0]["reloc_anchor"] == "## My Tweaks"  # precondition: minted
    store.record("p", fid, base=BASE, local=LOCAL, hunks=rows)

    result = host_local_sections_from_store("p")

    assert set(result) == {"claude/CLAUDE.md"}
    sections = result["claude/CLAUDE.md"]
    assert set(sections) == {"## My Tweaks"}
    section = sections[HostLocalSectionName("## My Tweaks")]
    assert isinstance(section, HostLocalSection)
    assert section.anchor == AnchorAfterHeading(value="## My Tweaks")
    assert section.body == "## My Tweaks\nmy custom line\n"
    assert section.body_file is None


def test_local_edit_without_reloc_anchor_not_projected(tmp_state: Path) -> None:
    # An ordinary headingless local edit mints no reloc_anchor -> not a section.
    fid = file_id("notes.md")
    base = b"alpha\nbeta\ngamma\n"
    local = b"alpha\nBETA-EDITED\ngamma\n"
    (hunk,) = extract_hunks(base, local)
    rows = serialize([replace(hunk, cls=HunkClass.LOCAL)])
    assert "reloc_anchor" not in rows[0]  # precondition: no heading -> no mint
    store.record("p", fid, base=base, local=local, hunks=rows)

    assert host_local_sections_from_store("p") == {}


def test_shared_unit_not_projected(tmp_state: Path) -> None:
    # A SHARED unit is not host-local even if a reloc_anchor is present on its row.
    fid = file_id("shared.md")
    base = b"## Alpha\naaa\n## Beta\nbbb\n"
    local = b"## Alpha\naaa\n## Shared Bit\nshared line\n## Beta\nbbb\n"
    (hunk,) = extract_hunks(base, local)
    shared = replace(hunk, cls=HunkClass.SHARED, reloc_anchor="## Shared Bit")
    rows = serialize([shared])
    assert rows[0]["reloc_anchor"] == "## Shared Bit"  # reloc present but SHARED
    store.record("p", fid, base=base, local=local, hunks=rows)

    assert host_local_sections_from_store("p") == {}


def test_empty_store_empty_result(tmp_state: Path) -> None:
    assert host_local_sections_from_store("p") == {}


def test_fid_filter_scopes_projection(tmp_state: Path) -> None:
    # Two files each carry a host-local section; passing fid scopes to one.
    fid_a = file_id("a.md")
    fid_b = file_id("b.md")
    rows = serialize([replace(_hunk(BASE, LOCAL, "## My Tweaks"), cls=HunkClass.LOCAL)])
    store.record("p", fid_a, base=BASE, local=LOCAL, hunks=rows)
    store.record("p", fid_b, base=BASE, local=LOCAL, hunks=rows)

    assert set(host_local_sections_from_store("p")) == {"a.md", "b.md"}
    assert set(host_local_sections_from_store("p", fid_a)) == {"a.md"}
