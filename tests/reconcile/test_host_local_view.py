"""Tests for reading host-local section headings back out of the reconcile store.

:func:`host_local_headings_from_store` reports the headings of the host-local
markdown sections a tracked file holds in the reconcile per-unit store (LOCAL line
units carrying a ``reloc_anchor`` heading identity). :func:`seeded_headings_from_rows`
reports the same headings from the index rows alone.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from setforge.reconcile import store
from setforge.reconcile.host_local_view import (
    host_local_headings_from_store,
    seeded_headings_from_rows,
)
from setforge.reconcile.hunks import Hunk, extract_hunks, serialize
from setforge.reconcile.index_model import FileEntry, Index
from setforge.reconcile.types import ABSENT, HunkClass, file_id

BASE = b"## Alpha\naaa\n## Beta\nbbb\n"
LOCAL = b"## Alpha\naaa\n## My Tweaks\nmy custom line\n## Beta\nbbb\n"


def _hunk(base: bytes, local: bytes, label: str) -> Hunk:
    """The single extracted base->local hunk whose label matches ``label``."""
    return next(h for h in extract_hunks(base, local) if h.label == label)


def test_reports_local_reloc_section(tmp_state: Path) -> None:
    fid = file_id("claude/CLAUDE.md")
    rows = serialize([replace(_hunk(BASE, LOCAL, "## My Tweaks"), cls=HunkClass.LOCAL)])
    assert rows[0]["reloc_anchor"] == "## My Tweaks"
    store.record("p", fid, base=BASE, local=LOCAL, hunks=rows)

    assert host_local_headings_from_store("p", fid) == {"## My Tweaks"}


def test_local_edit_without_reloc_anchor_not_reported(tmp_state: Path) -> None:
    fid = file_id("notes.md")
    base = b"alpha\nbeta\ngamma\n"
    local = b"alpha\nBETA-EDITED\ngamma\n"
    (hunk,) = extract_hunks(base, local)
    rows = serialize([replace(hunk, cls=HunkClass.LOCAL)])
    assert "reloc_anchor" not in rows[0]
    store.record("p", fid, base=base, local=local, hunks=rows)

    assert host_local_headings_from_store("p", fid) == set()


def test_shared_unit_not_reported(tmp_state: Path) -> None:
    fid = file_id("shared.md")
    base = b"## Alpha\naaa\n## Beta\nbbb\n"
    local = b"## Alpha\naaa\n## Shared Bit\nshared line\n## Beta\nbbb\n"
    (hunk,) = extract_hunks(base, local)
    shared = replace(hunk, cls=HunkClass.SHARED, reloc_anchor="## Shared Bit")
    rows = serialize([shared])
    assert rows[0]["reloc_anchor"] == "## Shared Bit"
    store.record("p", fid, base=base, local=local, hunks=rows)

    assert host_local_headings_from_store("p", fid) == set()


def test_unrecorded_file_has_no_headings(tmp_state: Path) -> None:
    assert host_local_headings_from_store("p", file_id("missing.md")) == set()


def test_headings_are_scoped_to_the_requested_file(tmp_state: Path) -> None:
    fid_a = file_id("a.md")
    fid_b = file_id("b.md")
    rows = serialize([replace(_hunk(BASE, LOCAL, "## My Tweaks"), cls=HunkClass.LOCAL)])
    store.record("p", fid_a, base=BASE, local=LOCAL, hunks=rows)
    store.record("p", fid_b, base=BASE, local=BASE, hunks=[])

    assert host_local_headings_from_store("p", fid_a) == {"## My Tweaks"}
    assert host_local_headings_from_store("p", fid_b) == set()


MULTI_BASE = b"## Alpha\naaa\n## Beta\nbbb\n"
MULTI_LOCAL = (
    b"## Alpha\naaa\n## Tweak One\nfirst body\n"
    b"## Beta\nbbb\n## Tweak Two\nsecond body\n"
)


def test_reports_two_local_reloc_sections_in_one_file(tmp_state: Path) -> None:
    fid = file_id("claude/CLAUDE.md")
    rows = serialize(
        [
            replace(
                _hunk(MULTI_BASE, MULTI_LOCAL, "## Tweak One"), cls=HunkClass.LOCAL
            ),
            replace(
                _hunk(MULTI_BASE, MULTI_LOCAL, "## Tweak Two"), cls=HunkClass.LOCAL
            ),
        ]
    )
    assert {r["reloc_anchor"] for r in rows} == {"## Tweak One", "## Tweak Two"}
    store.record("p", fid, base=MULTI_BASE, local=MULTI_LOCAL, hunks=rows)

    assert host_local_headings_from_store("p", fid) == {"## Tweak One", "## Tweak Two"}


def test_fail_soft_when_base_missing_for_reloc_row(tmp_state: Path) -> None:
    fid = file_id("orphan.md")
    rows = serialize([replace(_hunk(BASE, LOCAL, "## My Tweaks"), cls=HunkClass.LOCAL)])
    assert rows[0]["reloc_anchor"] == "## My Tweaks"
    store.write_index(
        "p",
        Index(
            files={
                str(fid): FileEntry(
                    present=True, local_hash="sha256:x", staged=True, hunks=rows
                )
            }
        ),
    )
    assert store.read_base("p", fid) is None

    assert host_local_headings_from_store("p", fid) == set()


def test_fail_soft_when_local_absent_for_reloc_row(tmp_state: Path) -> None:
    fid = file_id("absent.md")
    rows = serialize([replace(_hunk(BASE, LOCAL, "## My Tweaks"), cls=HunkClass.LOCAL)])
    store.record("p", fid, base=BASE, local=ABSENT, hunks=rows)
    assert store.read_local("p", fid) is ABSENT

    assert host_local_headings_from_store("p", fid) == set()


def test_rows_only_counts_a_row_whose_section_left_the_recorded_bytes(
    tmp_state: Path,
) -> None:
    fid = file_id("claude/CLAUDE.md")
    rows = serialize([replace(_hunk(BASE, LOCAL, "## My Tweaks"), cls=HunkClass.LOCAL)])
    store.record("p", fid, base=BASE, local=BASE, hunks=rows)

    assert host_local_headings_from_store("p", fid) == set()
    assert seeded_headings_from_rows("p", fid) == {"## My Tweaks"}


def test_rows_only_ignores_shared_rows_and_local_rows_without_an_anchor(
    tmp_state: Path,
) -> None:
    shared_fid = file_id("shared.md")
    shared = replace(
        _hunk(BASE, LOCAL, "## My Tweaks"),
        cls=HunkClass.SHARED,
        reloc_anchor="## My Tweaks",
    )
    store.record("p", shared_fid, base=BASE, local=LOCAL, hunks=serialize([shared]))

    plain_fid = file_id("notes.md")
    plain_base = b"alpha\nbeta\ngamma\n"
    plain_local = b"alpha\nBETA-EDITED\ngamma\n"
    (plain,) = extract_hunks(plain_base, plain_local)
    plain_rows = serialize([replace(plain, cls=HunkClass.LOCAL)])
    assert "reloc_anchor" not in plain_rows[0]
    store.record("p", plain_fid, base=plain_base, local=plain_local, hunks=plain_rows)

    assert seeded_headings_from_rows("p", shared_fid) == set()
    assert seeded_headings_from_rows("p", plain_fid) == set()


def test_rows_only_unrecorded_file_has_no_headings(tmp_state: Path) -> None:
    assert seeded_headings_from_rows("p", file_id("missing.md")) == set()
