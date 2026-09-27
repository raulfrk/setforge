"""Malformed archives fail one item without discarding independent effects."""

import hashlib
import io
import random
import tarfile
import zipfile
from pathlib import Path

import pytest

from setforge.config import GitHubReleasePackage, LocalPackage
from setforge.provision.driver import exit_code, reconcile
from setforge.provision.github_release import GitHubReleaseProvisioner
from setforge.provision.identity import package_identity
from setforge.provision.local import LocalProvisioner
from setforge.provision.protocol import Outcome, ProvisionItem
from setforge.provision.receipt import ReceiptStore


def _truncated_tar_gz() -> bytes:
    buffer = io.BytesIO()
    payload = random.Random(17).randbytes(32768)
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        member = tarfile.TarInfo("broken")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    data = buffer.getvalue()
    return data[: len(data) // 2]


def _invalid_zip_payload(kind: str) -> bytes:
    buffer = io.BytesIO()
    name = "broken"
    compression = {
        "deflate": zipfile.ZIP_DEFLATED,
        "bzip2": zipfile.ZIP_BZIP2,
        "lzma": zipfile.ZIP_LZMA,
        "unsupported": zipfile.ZIP_DEFLATED,
        "encrypted": zipfile.ZIP_DEFLATED,
    }[kind]
    with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
        archive.writestr(name, b"executable payload")
    data = bytearray(buffer.getvalue())
    # This fixture has one ASCII filename and no extra fields. Retain the
    # ZIP structure while changing its compressed member or format flags.
    compressed_offset = 30 + len(name)
    central_offset = data.index(b"PK\x01\x02")
    if kind == "deflate":
        data[compressed_offset] = (data[compressed_offset] & 0xF8) | 7
    elif kind == "bzip2":
        data[compressed_offset : compressed_offset + 3] = b"bad"
    elif kind == "lzma":
        data[compressed_offset + 4] = 0xFF
    elif kind == "unsupported":
        data[8:10] = (99).to_bytes(2, "little")
        data[central_offset + 10 : central_offset + 12] = (99).to_bytes(2, "little")
    else:
        data[6] |= 1
        data[central_offset + 8] |= 1
    return bytes(data)


@pytest.mark.parametrize("provider", ["local", "github_release"])
@pytest.mark.parametrize(
    ("asset", "archive_data"),
    [
        ("broken.tar.gz", b"not an archive"),
        ("broken.zip", b"not an archive"),
        ("broken.tar.gz", _truncated_tar_gz()),
        ("broken.zip", _invalid_zip_payload("deflate")),
        ("broken.zip", _invalid_zip_payload("bzip2")),
        ("broken.zip", _invalid_zip_payload("lzma")),
        ("broken.zip", _invalid_zip_payload("unsupported")),
        ("broken.zip", _invalid_zip_payload("encrypted")),
    ],
    ids=[
        "invalid-tar",
        "invalid-zip",
        "truncated-gzip",
        "corrupt-deflate",
        "corrupt-bzip2",
        "corrupt-lzma",
        "unsupported-compression",
        "encrypted",
    ],
)
def test_malformed_archive_is_one_hard_failure_and_other_package_installs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    asset: str,
    archive_data: bytes,
) -> None:
    tracked = tmp_path / "tracked"
    tracked.mkdir()
    destination = tmp_path / "bin"
    destination.mkdir()
    original = b"keep the existing executable"
    (destination / "broken").write_bytes(original)
    payloads = {asset: archive_data, "good": b"#!/bin/sh\nexit 0\n"}
    receipts = ReceiptStore(tmp_path / "receipts")
    items = []
    for filename, payload in payloads.items():
        checksum = "sha256:" + hashlib.sha256(payload).hexdigest()
        binary = "good" if filename == "good" else "broken"
        package: LocalPackage | GitHubReleasePackage
        if provider == "local":
            (tracked / filename).write_bytes(payload)
            package = LocalPackage(
                path=filename,
                binary=binary,
                install=str(destination),
                extract=binary == "broken",
                checksum=checksum,
            )
        else:
            package = GitHubReleasePackage(
                repo=f"owner/{binary}",
                tag="v1.0.0",
                asset=filename,
                binary=binary,
                install=str(destination),
                extract=binary == "broken",
                checksum=checksum,
            )
        items.append(
            ProvisionItem(
                type=provider,
                identity=package_identity(package),
                config=package,
                checksum=checksum,
                version="v1.0.0" if provider == "github_release" else None,
            )
        )
    provisioner: LocalProvisioner | GitHubReleaseProvisioner
    if provider == "local":
        provisioner = LocalProvisioner(receipts=receipts, tracked_root=tracked)
    else:
        provisioner = GitHubReleaseProvisioner(receipts=receipts)
        monkeypatch.setattr(
            provisioner, "_download", lambda url: payloads[url.rsplit("/", 1)[1]]
        )

    result = reconcile(provisioner, items)

    assert [outcome.outcome for outcome in result.outcomes] == [
        Outcome.HARD,
        Outcome.OK,
    ]
    assert "archive" in result.outcomes[0].detail.lower()
    assert exit_code(result) == 1
    assert result.delta.installed == (items[1].identity,)
    assert receipts.installed_for(provider) == {items[1].identity}
    assert (destination / "broken").read_bytes() == original
    assert (destination / "good").read_bytes() == payloads["good"]
