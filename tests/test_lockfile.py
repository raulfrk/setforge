"""Tests for the setforge.lock model + (de)serialization: determinism, round-trips."""

import random
from pathlib import Path

import pytest
from pydantic import ValidationError

from setforge.errors import LockVersionError, MalformedLockError, SetforgeError
from setforge.lockfile import (
    LOCK_FILENAME,
    LOCK_VERSION,
    LockFile,
    dump_lock,
    lock_path,
    parse_lock,
    write_lock,
)
from setforge.provision.resolve.protocol import (
    IntegrityKind,
    PackageType,
    ResolvedArtifact,
    ResolvedPin,
    artifact_set_integrity,
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


def _portable_release_pin() -> ResolvedPin:
    artifacts = (
        ResolvedArtifact(
            os="linux",
            arch="x86_64",
            asset="tool-linux-amd64.tar.gz",
            checksum=f"sha256:{'b' * 64}",
        ),
        ResolvedArtifact(
            os="macos",
            arch="aarch64",
            asset="tool-darwin-arm64.tar.gz",
            checksum=f"sha256:{'a' * 64}",
        ),
    )
    return ResolvedPin(
        type=PackageType.GITHUB_RELEASE,
        key="owner/tool",
        version="v1.2.3",
        integrity=artifact_set_integrity(artifacts),
        integrity_kind=IntegrityKind.CHECKSUM,
        profiles=("portable",),
        artifacts=artifacts,
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
    assert "\nsum = " not in text
    assert "\nsha = " not in text


def test_portable_release_pin_requires_checksum_integrity_kind() -> None:
    pin = _portable_release_pin()
    with pytest.raises(ValidationError, match="require checksum integrity"):
        ResolvedPin.model_validate(
            {**pin.model_dump(), "integrity_kind": IntegrityKind.SHA}
        )


def test_sum_pin_serializes_under_sum_column() -> None:
    text = dump_lock(LockFile(packages=(_sum_pin(),)))
    assert "\nsum = " in text
    assert "checksum = " not in text


def test_sha_pin_serializes_under_sha_column() -> None:
    text = dump_lock(LockFile(packages=(_sha_pin(),)))
    assert "\nsha = " in text
    assert "checksum = " not in text


def test_parse_dump_round_trip_equal() -> None:
    lf = LockFile(version=1, packages=(_checksum_pin(), _sum_pin(), _sha_pin()))
    assert parse_lock(dump_lock(lf)) == LockFile(
        version=1,
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


def test_v2_portable_release_artifacts_round_trip_without_host_pin() -> None:
    text = dump_lock(LockFile(packages=(_portable_release_pin(),)))

    assert "version = 2" in text
    assert text.count("[[package.artifact]]") == 2
    assert 'os = "macos"' in text
    assert 'arch = "x86_64"' in text
    reparsed = parse_lock(text)
    assert reparsed.packages[0].artifacts == _portable_release_pin().artifacts


def test_v1_rejects_v2_artifact_tables() -> None:
    text = """\
version = 1
[[package]]
type = "github_release"
key = "owner/tool"
version = "v1"
[[package.artifact]]
os = "linux"
arch = "x86_64"
asset = "tool.tar.gz"
checksum = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
"""
    with pytest.raises(MalformedLockError, match="v1 does not support"):
        parse_lock(text)


def test_v2_rejects_duplicate_platform_artifacts() -> None:
    text = """\
version = 2
[[package]]
type = "github_release"
key = "owner/tool"
version = "v1"
[[package.artifact]]
os = "linux"
arch = "x86_64"
asset = "one.tar.gz"
checksum = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
[[package.artifact]]
os = "linux"
arch = "x86_64"
asset = "two.tar.gz"
checksum = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
"""
    with pytest.raises(MalformedLockError, match="sorted and unique"):
        parse_lock(text)


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


@pytest.mark.parametrize(
    "integrity_literal",
    ["12345", '["a"]'],
    ids=["scalar-int", "array"],
)
def test_reject_non_string_integrity(integrity_literal: str) -> None:
    # Valid TOML, passes presence guards, but must not leak a raw pydantic error.
    text = (
        'version = 1\n[[package]]\ntype = "cargo"\nkey = "x"\n'
        f'version = "1"\nchecksum = {integrity_literal}\n'
    )
    with pytest.raises(MalformedLockError):
        parse_lock(text)


def test_reject_missing_key() -> None:
    text = 'version = 1\n[[package]]\ntype = "go"\nversion = "1"\nsum = "h1:a"\n'
    with pytest.raises(MalformedLockError, match="'key'"):
        parse_lock(text)


def test_reject_newer_lock_version() -> None:
    text = "version = 3\n"
    with pytest.raises(LockVersionError, match="Upgrade setforge"):
        parse_lock(text)


def test_accept_older_lock_version() -> None:
    text = "version = 0\n"
    assert parse_lock(text) == LockFile(version=0, packages=())


def test_reject_missing_version_unchanged() -> None:
    with pytest.raises(MalformedLockError, match="version"):
        parse_lock('[[package]]\ntype = "go"\nkey = "x"\nversion = "1"\nsum = "h1:a"\n')


def test_newer_lock_version_refused_before_pin_parsing() -> None:
    text = (
        f"version = {LOCK_VERSION + 1}\n"
        '[[package]]\ntype = "go"\nkey = "x"\nversion = "1"\n'
    )
    with pytest.raises(LockVersionError, match="Upgrade setforge"):
        parse_lock(text)


def test_newer_lock_version_is_a_setforge_error() -> None:
    with pytest.raises(SetforgeError):
        parse_lock("version = 999\n")
