"""Tests for the install-receipt store (spec §4).

The receipt is a per-package marker (identity + version + checksum) for
list-less ecosystems, written atomically after each install success. It is
NOT the future resolved-graph lockfile (setforge.lock) — a distinct directory
under the state dir.
"""

import json
from pathlib import Path

import pytest

from setforge.errors import CorruptReceiptError
from setforge.provision.protocol import Identity
from setforge.provision.receipt import (
    ReceiptStore,
    _receipt_name,
    default_receipt_root,
)


def _ident(key: str = "pkg", display: str = "Pkg") -> Identity:
    return Identity(key=key, display=display)


def test_record_then_installed_round_trips(tmp_path: Path) -> None:
    store = ReceiptStore(tmp_path)
    ident = _ident()
    store.record(ident, version="1.2.3", checksum="abc")
    assert store.installed() == {ident}


def test_installed_empty_when_no_receipts(tmp_path: Path) -> None:
    assert ReceiptStore(tmp_path).installed() == set()


def test_re_record_replaces(tmp_path: Path) -> None:
    store = ReceiptStore(tmp_path)
    ident = _ident()
    store.record(ident, version="1.0.0", checksum=None)
    store.record(ident, version="2.0.0", checksum="deadbeef")
    assert store.installed() == {ident}
    files = list(tmp_path.iterdir())
    assert len(files) == 1


def test_per_item_durability_survives_fresh_store(tmp_path: Path) -> None:
    ReceiptStore(tmp_path).record(_ident("a", "A"), version="1", checksum=None)
    assert ReceiptStore(tmp_path).installed() == {_ident("a", "A")}


def test_write_is_atomic_no_partial_file(tmp_path: Path) -> None:
    # atomic_write leaves no .tmp debris and the published file is whole JSON.
    store = ReceiptStore(tmp_path)
    store.record(_ident(), version="1.2.3", checksum="abc")
    debris = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert debris == []
    assert store.installed() == {_ident()}


def test_installed_raises_on_invalid_json(tmp_path: Path) -> None:
    bad = tmp_path / "deadbeef.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(CorruptReceiptError) as excinfo:
        ReceiptStore(tmp_path).installed()
    assert str(bad) in str(excinfo.value)


def test_installed_raises_on_missing_key(tmp_path: Path) -> None:
    bad = tmp_path / "deadbeef.json"
    bad.write_text('{"display": "X"}', encoding="utf-8")
    with pytest.raises(CorruptReceiptError) as excinfo:
        ReceiptStore(tmp_path).installed()
    assert str(bad) in str(excinfo.value)


def test_installed_ignores_stray_tmp_file(tmp_path: Path) -> None:
    store = ReceiptStore(tmp_path)
    store.record(_ident(), version="1", checksum=None)
    (tmp_path / "orphan.json.tmp").write_text("garbage-not-json", encoding="utf-8")
    assert store.installed() == {_ident()}


def test_record_with_path_round_trips(tmp_path: Path) -> None:
    store = ReceiptStore(tmp_path)
    ident = _ident()
    store.record(ident, version="1.2.3", checksum="abc", path="/opt/bin/pkg")
    assert store.path_for(ident) == Path("/opt/bin/pkg")


def test_record_accepts_path_object(tmp_path: Path) -> None:
    store = ReceiptStore(tmp_path)
    ident = _ident()
    store.record(ident, version="1", checksum=None, path=Path("/opt/bin/pkg"))
    assert store.path_for(ident) == Path("/opt/bin/pkg")


def test_path_for_none_when_unrecorded(tmp_path: Path) -> None:
    store = ReceiptStore(tmp_path)
    ident = _ident()
    store.record(ident, version="1", checksum=None)
    assert store.path_for(ident) is None


def test_path_for_none_when_no_receipt(tmp_path: Path) -> None:
    assert ReceiptStore(tmp_path).path_for(_ident()) is None


def test_old_payload_without_path_reads_cleanly(tmp_path: Path) -> None:
    # Simulate a receipt written before the path field existed: no "path" key.
    ident = _ident()
    legacy = {
        "key": ident.key,
        "display": ident.display,
        "version": "1",
        "checksum": None,
    }
    (tmp_path / _receipt_name(ident)).write_text(
        json.dumps(legacy) + "\n", encoding="utf-8"
    )
    store = ReceiptStore(tmp_path)
    assert store.installed() == {ident}  # does NOT raise
    assert store.path_for(ident) is None


def test_installed_unchanged_ignores_path(tmp_path: Path) -> None:
    store = ReceiptStore(tmp_path)
    ident = _ident()
    store.record(ident, version="1", checksum="c", path="/opt/bin/pkg")
    assert store.installed() == {ident}


def test_path_for_raises_on_missing_key(tmp_path: Path) -> None:
    ident = _ident()
    (tmp_path / _receipt_name(ident)).write_text('{"display": "X"}', encoding="utf-8")
    with pytest.raises(CorruptReceiptError):
        ReceiptStore(tmp_path).path_for(ident)


def test_path_for_raises_on_wrong_typed_path(tmp_path: Path) -> None:
    # A syntactically-valid receipt whose path value is the wrong type must
    # surface as CorruptReceiptError, not a raw TypeError from Path().
    ident = _ident()
    payload = {"key": ident.key, "display": ident.display, "path": 123}
    (tmp_path / _receipt_name(ident)).write_text(
        json.dumps(payload) + "\n", encoding="utf-8"
    )
    with pytest.raises(CorruptReceiptError):
        ReceiptStore(tmp_path).path_for(ident)


def test_remove_deletes_receipt(tmp_path: Path) -> None:
    store = ReceiptStore(tmp_path)
    ident = _ident()
    store.record(ident, version="1", checksum=None, path="/opt/bin/pkg")
    assert store.installed() == {ident}
    store.remove(ident)
    assert store.installed() == set()
    assert store.path_for(ident) is None
    assert list(tmp_path.iterdir()) == []


def test_remove_is_idempotent(tmp_path: Path) -> None:
    """Removing an absent receipt is a no-op, never a raise (crash-safe reap)."""
    store = ReceiptStore(tmp_path)
    store.remove(_ident("never-recorded"))  # must not raise


def test_remove_only_targets_named_receipt(tmp_path: Path) -> None:
    store = ReceiptStore(tmp_path)
    store.record(_ident("a", "A"), version="1", checksum=None)
    store.record(_ident("b", "B"), version="1", checksum=None)
    store.remove(_ident("a", "A"))
    assert store.installed() == {_ident("b", "B")}


def test_receipt_root_distinct_from_lockfile(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    from setforge.locking import state_root

    root = default_receipt_root()
    lock_path = state_root() / "locks" / "debian-vm.lock"
    assert root != lock_path.parent
    assert root.name == "receipts"
    assert not str(root).endswith(".lock")
