"""Fingerprint-preservation tests for host-local section recording."""

from dataclasses import replace
from pathlib import Path

import pytest

from setforge import locking
from setforge.reconcile import hunks, store
from setforge.reconcile.host_local_record import record_local_reloc_sections
from setforge.reconcile.types import HunkClass, file_id


def test_host_local_record_does_not_reconfirm_changed_shared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    base = b"## Shared\nbase\n\n## Stable\nsame\n"
    confirmed = b"## Shared\nconfirmed\n\n## Stable\nsame\n"
    drifted_with_local = (
        b"## Shared\ndrifted\n\n## Stable\nsame\n\n## Host Only\nsecret\n"
    )
    shared = hunks.extract_hunks(base, confirmed)[0]
    rows = hunks.serialize([replace(shared, cls=HunkClass.SHARED)])

    with locking.profile_lock("p"):
        record_local_reloc_sections(
            "p",
            file_id("notes"),
            base=base,
            new_local=drifted_with_local,
            existing_hunks=rows,
            residual_headings={"## Host Only"},
        )

    persisted = store.read_index("p").files["notes"]
    by_label = {str(row["label"]): row for row in persisted.hunks}
    assert persisted.staged is True
    assert by_label["## Shared"]["cls"] == HunkClass.SHARED.value
    assert by_label["## Shared"]["live_hash"] == shared.live_hash
    assert by_label["## Host Only"]["cls"] == HunkClass.LOCAL.value
