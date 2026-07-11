"""Tests for the shared archive→verify→extract→install core.

The core takes raw archive BYTES plus an :class:`InstallSpec` and performs a
security-hardened install (checksum verify → safe extract → pick binary →
atomic install with the exec bit). The download/read SOURCE lives in the
caller (github_release downloads; local reads a tracked file), so this module
never touches the network or the config.

Every security gate in the checklist is exercised here against BAKED in-memory
fixture archives - no network, no real assets.
"""

from __future__ import annotations

import hashlib
import io
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest

from setforge.provision.installer import (
    InstallError,
    InstallSpec,
    install_from_bytes,
)
from setforge.provision.protocol import Outcome


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _tar_gz(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, payload in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def _tar_gz_with(info: tarfile.TarInfo, payload: bytes = b"") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.addfile(info, io.BytesIO(payload) if payload else None)
    return buf.getvalue()


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, payload in members.items():
            zf.writestr(name, payload)
    return buf.getvalue()


def _spec(
    install_dir: Path,
    *,
    asset: str = "tool.tar.gz",
    binary: str = "tool",
    rename: str | None = None,
    extract: bool = True,
    checksum: str | None = None,
) -> InstallSpec:
    return InstallSpec(
        asset=asset,
        binary=binary,
        install_dir=install_dir,
        rename=rename,
        extract=extract,
        chmod="+x",
        checksum=checksum,
    )


# --- happy path ---------------------------------------------------------


def test_happy_path_installs_binary_with_exec_bit(tmp_path: Path) -> None:
    install_dir = tmp_path / "bin"
    payload = b"#!/bin/sh\necho hi\n"
    data = _tar_gz({"tool": payload})
    spec = _spec(install_dir, checksum=_sha256(data))

    dest = install_from_bytes(data, spec, checksum_required=True)

    assert dest == install_dir / "tool"
    assert dest.read_bytes() == payload
    assert dest.stat().st_mode & stat.S_IXUSR


def test_rename_targets_the_renamed_filename(tmp_path: Path) -> None:
    install_dir = tmp_path / "bin"
    data = _tar_gz({"tool": b"payload"})
    spec = _spec(install_dir, rename="renamed", checksum=_sha256(data))

    dest = install_from_bytes(data, spec, checksum_required=True)

    assert dest == install_dir / "renamed"
    assert dest.read_bytes() == b"payload"


def test_zip_happy_path(tmp_path: Path) -> None:
    install_dir = tmp_path / "bin"
    data = _zip({"tool": b"zip-payload"})
    spec = _spec(install_dir, asset="tool.zip", checksum=_sha256(data))

    dest = install_from_bytes(data, spec, checksum_required=True)

    assert dest.read_bytes() == b"zip-payload"


def test_no_extract_installs_the_asset_bytes_directly(tmp_path: Path) -> None:
    install_dir = tmp_path / "bin"
    data = b"raw-binary-bytes"
    spec = _spec(
        install_dir, asset="tool", binary="tool", extract=False, checksum=_sha256(data)
    )

    dest = install_from_bytes(data, spec, checksum_required=True)

    assert dest.read_bytes() == data
    assert dest.stat().st_mode & stat.S_IXUSR


# --- (a) checksum verified BEFORE extract -------------------------------


def test_bad_checksum_is_hard_and_installs_nothing(tmp_path: Path) -> None:
    install_dir = tmp_path / "bin"
    data = _tar_gz({"tool": b"payload"})
    spec = _spec(install_dir, checksum="sha256:" + "0" * 64)

    with pytest.raises(InstallError) as exc:
        install_from_bytes(data, spec, checksum_required=True)

    assert exc.value.kind is Outcome.HARD
    assert not install_dir.exists() or not any(install_dir.iterdir())


def test_missing_checksum_when_required_is_hard(tmp_path: Path) -> None:
    install_dir = tmp_path / "bin"
    data = _tar_gz({"tool": b"payload"})
    spec = _spec(install_dir, checksum=None)

    with pytest.raises(InstallError) as exc:
        install_from_bytes(data, spec, checksum_required=True)

    assert exc.value.kind is Outcome.HARD
    assert not install_dir.exists() or not any(install_dir.iterdir())


def test_missing_checksum_when_optional_installs(tmp_path: Path) -> None:
    install_dir = tmp_path / "bin"
    data = _tar_gz({"tool": b"payload"})
    spec = _spec(install_dir, checksum=None)

    dest = install_from_bytes(data, spec, checksum_required=False)

    assert dest.read_bytes() == b"payload"


def test_unknown_checksum_algo_is_hard(tmp_path: Path) -> None:
    install_dir = tmp_path / "bin"
    data = _tar_gz({"tool": b"payload"})
    spec = _spec(install_dir, checksum="md5:" + "a" * 32)

    with pytest.raises(InstallError) as exc:
        install_from_bytes(data, spec, checksum_required=True)

    assert exc.value.kind is Outcome.HARD


def test_malformed_checksum_length_is_hard(tmp_path: Path) -> None:
    install_dir = tmp_path / "bin"
    data = _tar_gz({"tool": b"payload"})
    spec = _spec(install_dir, checksum="sha256:deadbeef")  # too short

    with pytest.raises(InstallError) as exc:
        install_from_bytes(data, spec, checksum_required=True)

    assert exc.value.kind is Outcome.HARD


# --- (b) path escape / symlink / hardlink rejects -----------------------


def test_tar_member_with_dotdot_escape_is_rejected(tmp_path: Path) -> None:
    install_dir = tmp_path / "bin"
    info = tarfile.TarInfo(name="../escape")
    info.size = 3
    data = _tar_gz_with(info, b"bad")
    spec = _spec(install_dir, binary="escape", checksum=_sha256(data))

    with pytest.raises(InstallError) as exc:
        install_from_bytes(data, spec, checksum_required=True)

    assert exc.value.kind is Outcome.HARD
    assert not (tmp_path / "escape").exists()


def test_tar_member_with_absolute_path_is_rejected(tmp_path: Path) -> None:
    install_dir = tmp_path / "bin"
    info = tarfile.TarInfo(name="/etc/evil")
    info.size = 3
    data = _tar_gz_with(info, b"bad")
    spec = _spec(install_dir, binary="evil", checksum=_sha256(data))

    with pytest.raises(InstallError) as exc:
        install_from_bytes(data, spec, checksum_required=True)

    assert exc.value.kind is Outcome.HARD


def test_tar_symlink_member_is_rejected(tmp_path: Path) -> None:
    install_dir = tmp_path / "bin"
    info = tarfile.TarInfo(name="tool")
    info.type = tarfile.SYMTYPE
    info.linkname = "/etc/passwd"
    data = _tar_gz_with(info)
    spec = _spec(install_dir, checksum=_sha256(data))

    with pytest.raises(InstallError) as exc:
        install_from_bytes(data, spec, checksum_required=True)

    assert exc.value.kind is Outcome.HARD


def test_tar_hardlink_member_is_rejected(tmp_path: Path) -> None:
    install_dir = tmp_path / "bin"
    real = tarfile.TarInfo(name="real")
    real.size = 3
    link = tarfile.TarInfo(name="tool")
    link.type = tarfile.LNKTYPE
    link.linkname = "real"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.addfile(real, io.BytesIO(b"abc"))
        tar.addfile(link, None)
    data = buf.getvalue()
    spec = _spec(install_dir, checksum=_sha256(data))

    with pytest.raises(InstallError) as exc:
        install_from_bytes(data, spec, checksum_required=True)

    assert exc.value.kind is Outcome.HARD


def test_zip_member_with_dotdot_escape_is_rejected(tmp_path: Path) -> None:
    install_dir = tmp_path / "bin"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../escape", b"bad")
    data = buf.getvalue()
    spec = _spec(install_dir, asset="tool.zip", binary="escape", checksum=_sha256(data))

    with pytest.raises(InstallError) as exc:
        install_from_bytes(data, spec, checksum_required=True)

    assert exc.value.kind is Outcome.HARD
    assert not (tmp_path / "escape").exists()


def test_zip_symlink_member_is_rejected(tmp_path: Path) -> None:
    install_dir = tmp_path / "bin"
    # A zip has no link type; a symlink is encoded in the Unix mode bits held
    # in the high half of external_attr (S_IFLNK == 0o120000).
    info = zipfile.ZipInfo("tool")
    info.external_attr = (0o120777 & 0xFFFF) << 16
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(info, b"/etc/passwd")
    data = buf.getvalue()
    spec = _spec(install_dir, asset="tool.zip", checksum=_sha256(data))

    with pytest.raises(InstallError) as exc:
        install_from_bytes(data, spec, checksum_required=True)

    assert exc.value.kind is Outcome.HARD
    assert not install_dir.exists() or not any(install_dir.iterdir())


# --- (c) decompression-bomb guard ---------------------------------------


def test_decompression_bomb_over_cap_is_rejected(tmp_path: Path) -> None:
    install_dir = tmp_path / "bin"
    # A single member whose real (and declared) size exceeds the cap.
    big = b"\0" * (2 * 1024 * 1024)  # 2 MiB, compresses to ~nothing
    data = _tar_gz({"tool": big})
    spec = _spec(install_dir, checksum=_sha256(data))

    with pytest.raises(InstallError) as exc:
        install_from_bytes(data, spec, checksum_required=True, max_uncompressed=1024)

    assert exc.value.kind is Outcome.HARD
    assert not install_dir.exists() or not any(install_dir.iterdir())


# --- (e) install-path confinement ---------------------------------------


def test_binary_name_escaping_install_dir_is_rejected(tmp_path: Path) -> None:
    install_dir = tmp_path / "bin"
    data = _tar_gz({"tool": b"payload"})
    # A binary that resolves outside install_dir must be rejected at compose.
    spec = _spec(install_dir, binary="../evil", checksum=_sha256(data))

    with pytest.raises(InstallError) as exc:
        install_from_bytes(data, spec, checksum_required=True)

    assert exc.value.kind is Outcome.HARD
    assert not (tmp_path / "evil").exists()


def test_missing_binary_in_archive_is_hard(tmp_path: Path) -> None:
    install_dir = tmp_path / "bin"
    data = _tar_gz({"other": b"payload"})
    spec = _spec(install_dir, binary="tool", checksum=_sha256(data))

    with pytest.raises(InstallError) as exc:
        install_from_bytes(data, spec, checksum_required=True)

    assert exc.value.kind is Outcome.HARD
