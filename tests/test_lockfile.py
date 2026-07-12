"""Tests for the setforge.lock model + (de)serialization (spec §B1, §C).

Covers the three integrity kinds' round-trips, byte-for-byte determinism
(including input-order independence), the parse/dump round-trip, atomic write,
and clean rejection of malformed locks.
"""

import random
from pathlib import Path

import pytest

from setforge.errors import MalformedLockError
from setforge.lockfile import (
    LOCK_FILENAME,
    LockFile,
    dump_lock,
    lock_path,
    parse_lock,
    write_lock,
)
from setforge.provision.resolve.protocol import (
    IntegrityKind,
    PackageType,
    ResolvedPin,
)


def _checksum_pin() -> ResolvedPin:
    return ResolvedPin(
        type=PackageType.GITHUB_RELEASE,
        key="revdiff-bin",
        version="v0.8.9",
        integrity="sha256:9f3c",
        integrity_kind=IntegrityKind.CHECKSUM,
        profiles=("debian-vm",),
    )


def _sum_pin() -> ResolvedPin:
    return ResolvedPin(
        type=PackageType.GO,
        key="github.com/x/y",
        version="v1.4.2",
        integrity="h1:abc",
        integrity_kind=IntegrityKind.SUM,
        profiles=("debian-vm",),
    )


def _sha_pin() -> ResolvedPin:
    return ResolvedPin(
        type=PackageType.PLUGIN,
        key="revdiff@revdiff",
        version="4e5f6a",
        integrity="4e5f6a" * 6 + "abcd",
        integrity_kind=IntegrityKind.SHA,
        profiles=("debian-vm",),
    )


@pytest.mark.parametrize(
    "pin", [_checksum_pin(), _sum_pin(), _sha_pin()], ids=["checksum", "sum", "sha"]
)
def test_pin_round_trips_through_lock_entry(pin: ResolvedPin) -> None:
    lf = LockFile(packages=(pin,))
    reparsed = parse_lock(dump_lock(lf))
    assert reparsed.packages == (pin,)


def test_checksum_pin_serializes_under_checksum_column() -> None:
    text = dump_lock(LockFile(packages=(_checksum_pin(),)))
    assert "checksum = " in text
    # `sum`/`sha` are their own columns; anchor on line-start to avoid matching
    # the `sum` substring inside `checksum`.
    assert "\nsum = " not in text
    assert "\nsha = " not in text


def test_sum_pin_serializes_under_sum_column() -> None:
    text = dump_lock(LockFile(packages=(_sum_pin(),)))
    assert "sum = " in text
    assert "checksum = " not in text


def test_sha_pin_serializes_under_sha_column() -> None:
    text = dump_lock(LockFile(packages=(_sha_pin(),)))
    assert "sha = " in text
    assert "checksum = " not in text


def test_parse_dump_round_trip_equal() -> None:
    lf = LockFile(version=1, packages=(_checksum_pin(), _sum_pin(), _sha_pin()))
    assert parse_lock(dump_lock(lf)) == LockFile(
        version=1,
        # sorted by (type, key): github_release, go, plugin
        packages=(_checksum_pin(), _sum_pin(), _sha_pin()),
    )


def test_dump_is_byte_stable() -> None:
    lf = LockFile(packages=(_checksum_pin(), _sum_pin(), _sha_pin()))
    assert dump_lock(lf) == dump_lock(lf)


def test_dump_is_input_order_independent() -> None:
    pins = [_checksum_pin(), _sum_pin(), _sha_pin()]
    baseline = dump_lock(LockFile(packages=tuple(pins)))
    for _ in range(20):
        shuffled = pins[:]
        random.shuffle(shuffled)
        assert dump_lock(LockFile(packages=tuple(shuffled))) == baseline


def test_dump_has_no_timestamp_or_host_field() -> None:
    text = dump_lock(LockFile(packages=(_checksum_pin(),)))
    for banned in ("timestamp", "generated", "host", "duration", "date"):
        assert banned not in text


def test_lock_path_is_config_repo_root(tmp_path: Path) -> None:
    config = tmp_path / "sub" / "setforge.yaml"
    assert lock_path(config) == tmp_path / "sub" / LOCK_FILENAME


def test_write_lock_is_atomic_and_parses_back(tmp_path: Path) -> None:
    lf = LockFile(packages=(_checksum_pin(), _sum_pin()))
    dest = tmp_path / LOCK_FILENAME
    write_lock(lf, dest)
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())
    assert parse_lock(dest.read_text()) == lf


def test_reject_missing_version() -> None:
    with pytest.raises(MalformedLockError, match="version"):
        parse_lock('[[package]]\ntype = "go"\nkey = "x"\nversion = "1"\nsum = "h1:a"\n')


def test_reject_non_toml() -> None:
    with pytest.raises(MalformedLockError, match="not valid TOML"):
        parse_lock("this is = = not toml [[[")


def test_reject_unknown_type() -> None:
    text = (
        'version = 1\n[[package]]\ntype = "npm"\nkey = "x"\n'
        'version = "1"\nchecksum = "sha256:a"\n'
    )
    with pytest.raises(MalformedLockError, match="unknown package type"):
        parse_lock(text)


def test_reject_missing_integrity() -> None:
    text = 'version = 1\n[[package]]\ntype = "go"\nkey = "x"\nversion = "1"\n'
    with pytest.raises(MalformedLockError, match="no integrity field"):
        parse_lock(text)


def test_reject_wrong_integrity_column_for_ecosystem() -> None:
    # go must use `sum`, not `checksum`.
    text = (
        'version = 1\n[[package]]\ntype = "go"\nkey = "x"\n'
        'version = "1"\nchecksum = "sha256:a"\n'
    )
    with pytest.raises(MalformedLockError, match="uses integrity field"):
        parse_lock(text)


def test_reject_multiple_integrity_fields() -> None:
    text = (
        'version = 1\n[[package]]\ntype = "go"\nkey = "x"\nversion = "1"\n'
        'sum = "h1:a"\nchecksum = "sha256:b"\n'
    )
    with pytest.raises(MalformedLockError, match="multiple integrity"):
        parse_lock(text)


def test_reject_missing_key() -> None:
    text = 'version = 1\n[[package]]\ntype = "go"\nversion = "1"\nsum = "h1:a"\n'
    with pytest.raises(MalformedLockError, match="'key'"):
        parse_lock(text)
