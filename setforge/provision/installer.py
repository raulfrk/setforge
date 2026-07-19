"""The shared verify→resolve-mode→(extract+pick-binary | use-raw)→install core."""

from __future__ import annotations

import hashlib
import hmac
import io
import stat
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from setforge.atomicio import atomic_write_bytes
from setforge.provision.protocol import Outcome

__all__ = [
    "DEFAULT_MAX_UNCOMPRESSED",
    "InstallError",
    "InstallSpec",
    "install_from_bytes",
]

# Cap on summed DECOMPRESSED size (not wire bytes) — decompression-bomb guard.
DEFAULT_MAX_UNCOMPRESSED: int = 512 * 1024 * 1024  # 512 MiB

_ALLOWED_CHECKSUM_ALGOS: frozenset[str] = frozenset({"sha256"})
_SHA256_HEX_LEN = 64


@dataclass(frozen=True, slots=True)
class InstallSpec:
    asset: str
    binary: str
    install_dir: Path
    rename: str | None
    extract: bool
    chmod: str
    checksum: str | None


class InstallError(Exception):
    def __init__(self, message: str, *, kind: Outcome = Outcome.HARD) -> None:
        super().__init__(message)
        self.kind = kind


def install_from_bytes(
    data: bytes,
    spec: InstallSpec,
    *,
    checksum_required: bool,
    max_uncompressed: int = DEFAULT_MAX_UNCOMPRESSED,
) -> Path:
    _verify_checksum(data, spec.checksum, required=checksum_required)

    mode = _resolve_mode(spec.chmod)
    install_dir = spec.install_dir.expanduser().resolve()
    target_name = spec.rename if spec.rename is not None else spec.binary
    dest = _confine(install_dir, target_name)

    with tempfile.TemporaryDirectory(prefix="setforge-install-") as tmp:
        staging = Path(tmp)
        if spec.extract:
            binary_bytes = _extract_and_pick(
                data, spec, staging, max_uncompressed=max_uncompressed
            )
        else:
            binary_bytes = data
        _atomic_install(binary_bytes, dest, mode)
    return dest


def _verify_checksum(data: bytes, checksum: str | None, *, required: bool) -> None:
    # required is the caller's policy (github_release=True, local=False).
    if checksum is None:
        if required:
            raise InstallError("a checksum is required but none was provided")
        return
    algo, _, expected_hex = checksum.partition(":")
    if not _ or algo not in _ALLOWED_CHECKSUM_ALGOS:
        raise InstallError(
            f"unsupported checksum algorithm {algo!r}; allowed: "
            f"{', '.join(sorted(_ALLOWED_CHECKSUM_ALGOS))}"
        )
    expected_hex = expected_hex.strip().lower()
    if len(expected_hex) != _SHA256_HEX_LEN or not _is_hex(expected_hex):
        raise InstallError(
            f"malformed sha256 checksum {expected_hex!r}: "
            f"expected {_SHA256_HEX_LEN} hex characters"
        )
    actual_hex = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(actual_hex, expected_hex):
        raise InstallError(
            f"checksum mismatch: expected sha256 {expected_hex}, got {actual_hex}"
        )


def _is_hex(s: str) -> bool:
    try:
        int(s, 16)
    except ValueError:
        return False
    return True


def _confine(install_dir: Path, name: str) -> Path:
    if not name or "/" in name or ".." in name:
        raise InstallError(
            f"install target {name!r} must be a bare filename "
            "('/' and '..' are rejected)"
        )
    dest = (install_dir / name).resolve()
    if not dest.is_relative_to(install_dir):
        raise InstallError(
            f"install target {dest} escapes the install directory {install_dir}"
        )
    return dest


def _extract_and_pick(
    data: bytes,
    spec: InstallSpec,
    staging: Path,
    *,
    max_uncompressed: int,
) -> bytes:
    if _is_tar(spec.asset):
        return _extract_tar(data, spec, staging, max_uncompressed=max_uncompressed)
    if spec.asset.endswith(".zip"):
        return _extract_zip(data, spec, staging, max_uncompressed=max_uncompressed)
    raise InstallError(
        f"cannot extract asset {spec.asset!r}: unknown archive type "
        "(expected .tar.gz/.tgz/.tar or .zip)"
    )


def _is_tar(asset: str) -> bool:
    return asset.endswith((".tar.gz", ".tgz", ".tar", ".tar.bz2", ".tar.xz"))


def _extract_tar(
    data: bytes,
    spec: InstallSpec,
    staging: Path,
    *,
    max_uncompressed: int,
) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
        members = tar.getmembers()
        _guard_total_size(sum(m.size for m in members if m.isreg()), max_uncompressed)
        for member in members:
            _check_tar_member(member, staging)
        # defense in depth only: filter="data" has had bypasses (CVE-2025-4517)
        tar.extractall(path=staging, filter="data")
    return _read_picked(staging, spec.binary)


def _extract_zip(
    data: bytes,
    spec: InstallSpec,
    staging: Path,
    *,
    max_uncompressed: int,
) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = zf.infolist()
        _guard_total_size(sum(i.file_size for i in infos), max_uncompressed)
        for info in infos:
            _check_zip_member(info, staging)
        for info in infos:
            zf.extract(info, path=staging)
    return _read_picked(staging, spec.binary)


def _guard_total_size(total: int, cap: int) -> None:
    if total > cap:
        raise InstallError(
            f"archive decompresses to {total} bytes, over the {cap}-byte cap "
            "(possible decompression bomb)"
        )


def _check_tar_member(member: tarfile.TarInfo, dest: Path) -> None:
    if member.issym() or member.islnk():
        raise InstallError(
            f"archive member {member.name!r} is a "
            f"{'sym' if member.issym() else 'hard'}link — rejected"
        )
    _reject_escape(member.name, dest)


def _check_zip_member(info: zipfile.ZipInfo, dest: Path) -> None:
    if (info.external_attr >> 16) & 0o170000 == 0o120000:
        raise InstallError(f"archive member {info.filename!r} is a symlink — rejected")
    _reject_escape(info.filename, dest)


def _reject_escape(name: str, dest: Path) -> None:
    if Path(name).is_absolute():
        raise InstallError(f"archive member {name!r} has an absolute path — rejected")
    resolved = (dest / name).resolve()
    if not resolved.is_relative_to(dest.resolve()):
        raise InstallError(
            f"archive member {name!r} escapes the extraction directory — rejected"
        )


def _read_picked(staging: Path, binary: str) -> bytes:
    if binary.startswith("/") or ".." in Path(binary).parts:
        raise InstallError(f"binary path {binary!r} must stay inside the archive")
    picked = (staging / binary).resolve()
    if not picked.is_relative_to(staging.resolve()):
        raise InstallError(f"binary path {binary!r} escapes the archive — rejected")
    if not picked.is_file():
        raise InstallError(f"binary {binary!r} not found in the archive")
    return picked.read_bytes()


def _atomic_install(binary_bytes: bytes, dest: Path, mode: int) -> None:
    # fchmod-before-replace: mode set on the temp fd before the rename.
    atomic_write_bytes(dest, binary_bytes, mode=mode)


def _resolve_mode(chmod: str) -> int:
    if chmod == "+x":
        return _exec_mode()
    try:
        return int(chmod, 8)
    except ValueError:
        raise InstallError(
            f"unsupported chmod {chmod!r}: expected '+x' or a bare octal mode "
            "(e.g. '755' or '0755')"
        ) from None


def _exec_mode() -> int:
    return stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
