"""Tests for the install-receipt store (spec §4).

The receipt is a per-package marker (identity + version + checksum) for
list-less ecosystems, written atomically after each install success. It is
NOT the B10 lockfile — a distinct directory under the state dir.
"""

from pathlib import Path

import pytest

from setforge.errors import CorruptReceiptError
from setforge.provision.protocol import Identity
from setforge.provision.receipt import ReceiptStore, default_receipt_root


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
    # B is never recorded (a mid-batch crash). A must still be readable
    # by a fresh store instance — installed() reads on-disk ground truth.
    assert ReceiptStore(tmp_path).installed() == {_ident("a", "A")}


def test_write_is_atomic_no_partial_file(tmp_path: Path) -> None:
    # atomic_write leaves no .tmp debris and the published file is whole JSON.
    store = ReceiptStore(tmp_path)
    store.record(_ident(), version="1.2.3", checksum="abc")
    debris = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert debris == []
    assert store.installed() == {_ident()}


def test_installed_raises_on_invalid_json(tmp_path: Path) -> None:
    # A hand-corrupted receipt (not valid JSON) surfaces as CorruptReceiptError
    # naming the path, never a raw JSONDecodeError aborting reconcile.
    bad = tmp_path / "deadbeef.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(CorruptReceiptError) as excinfo:
        ReceiptStore(tmp_path).installed()
    assert str(bad) in str(excinfo.value)


def test_installed_raises_on_missing_key(tmp_path: Path) -> None:
    # Valid JSON but wrong shape (no "key") is corruption too.
    bad = tmp_path / "deadbeef.json"
    bad.write_text('{"display": "X"}', encoding="utf-8")
    with pytest.raises(CorruptReceiptError) as excinfo:
        ReceiptStore(tmp_path).installed()
    assert str(bad) in str(excinfo.value)


def test_installed_ignores_stray_tmp_file(tmp_path: Path) -> None:
    # A stray .tmp from a crashed atomic write is IGNORED, not parsed — only
    # final *.json receipts are read.
    store = ReceiptStore(tmp_path)
    store.record(_ident(), version="1", checksum=None)
    (tmp_path / "orphan.json.tmp").write_text("garbage-not-json", encoding="utf-8")
    assert store.installed() == {_ident()}


def test_receipt_root_distinct_from_lockfile(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    from setforge.locking import state_root

    root = default_receipt_root()
    lock_path = state_root() / "locks" / "debian-vm.lock"
    assert root != lock_path.parent
    assert root.name == "receipts"
    assert not str(root).endswith(".lock")
