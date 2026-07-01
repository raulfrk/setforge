"""Wave-5 tests: the cutover's own-transition wiring (D6).

The migrate driver skips its text-only transition for any chain containing a
migration that records its own (the cutover); the cutover's transition helper
writes ONE MIGRATE record carrying binary state_snapshots that round-trip
byte-exact via load_state_snapshots.
"""

from __future__ import annotations

from setforge import transitions
from setforge.cli.migrate import _chain_owns_transition
from setforge.migrations import Migration, VersionStampMigration
from setforge.migrations._contract_2_0 import Contract20Migration
from setforge.migrations._disposition_retire import (
    DispositionRetireMigration,
    _write_cutover_transition,
)
from setforge.transitions import SnapshotStore, StateSnapshotEntry


def test_plain_chain_does_not_own_transition() -> None:
    assert _chain_owns_transition([VersionStampMigration()]) is False


def test_cutover_chain_owns_transition() -> None:
    assert _chain_owns_transition([DispositionRetireMigration()]) is True


def test_multistep_chain_with_cutover_owns_transition() -> None:
    # A 1.2->3.0 upgrade chains earlier steps + the cutover; the presence of the
    # cutover still makes the driver skip its transition (no sole-step guard).
    chain: list[Migration] = [Contract20Migration(), DispositionRetireMigration()]
    assert _chain_owns_transition(chain) is True


def test_cutover_transition_round_trips_state_snapshots(tmp_path) -> None:
    cfg = tmp_path / "setforge.yaml"
    entries = (
        StateSnapshotEntry(
            store=SnapshotStore.SCALAR_BASE,
            profile="default",
            key="conf",
            payload=b'{"a": 1}',
        ),
        StateSnapshotEntry(
            store=SnapshotStore.SPANS, profile="default", key="conf", payload=None
        ),
    )
    td = _write_cutover_transition(
        file_pre={cfg: 'schema_version: "2.1"\n'},
        file_post={cfg: 'schema_version: "3.0"\n'},
        state_snapshots=entries,
    )
    loaded = transitions.load_state_snapshots(td)
    assert loaded is not None
    by_store = {(e.store, e.key): e for e in loaded}
    assert by_store[(SnapshotStore.SCALAR_BASE, "conf")].payload == b'{"a": 1}'
    # A None payload (the entry was absent pre-cutover) round-trips as None,
    # never collapsing to b"" — so revert deletes rather than writes an empty file.
    assert by_store[(SnapshotStore.SPANS, "conf")].payload is None
