"""The shared archive→verify→extract→pick-binary→install core.

The security-critical pipeline every asset-install provisioner reuses. It
takes raw archive BYTES plus an :class:`InstallSpec` and never sources those
bytes itself: github_release downloads them over HTTPS, ``local`` reads them
from a tracked file. Keeping the source out of the core is what lets both
callers share one hardened pipeline.

The pipeline, in order:

1. Verify the checksum (``hmac.compare_digest``) BEFORE any extract or read.
   Whether a MISSING checksum is fatal is the caller's policy, passed as
   ``checksum_required`` — github_release requires one; ``local`` does not.
   The core never bakes a single null-policy in.
2. Extract into a private :class:`~tempfile.TemporaryDirectory` that encloses
   every subsequent step, so any error path leaves nothing behind.
3. Independently re-validate every archive member (this is NOT delegated to
   ``tarfile``'s ``filter="data"`` alone — see the loop for why): reject
   absolute paths, ``..`` escapes, symlink and hardlink members, and cap the
   summed DECOMPRESSED size (decompression-bomb guard).
4. Pick the requested binary, re-confirm the composed destination resolves
   inside the install dir (two-layer path confinement), and atomically
   install it with the exec bit via :func:`~setforge.atomicio.atomic_write_bytes`.

Every failure raises :class:`InstallError` carrying an
:class:`~setforge.provision.protocol.Outcome` ``kind`` (always ``HARD`` here —
these are safety-gate failures; a transient download failure is the caller's
concern, mapped to ``SOFT`` there).
"""

from __future__ import annotations

import hashlib
import hmac
import io
import os
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

# Cap on the summed DECOMPRESSED payload (not wire bytes) — a
# decompression-bomb guard. A release binary is comfortably under this; a
# member set that sums past it is rejected before a single byte is written.
DEFAULT_MAX_UNCOMPRESSED: int = 512 * 1024 * 1024  # 512 MiB

_ALLOWED_CHECKSUM_ALGOS: frozenset[str] = frozenset({"sha256"})
_SHA256_HEX_LEN = 64


@dataclass(frozen=True, slots=True)
class InstallSpec:
    """The install request the core acts on (source-agnostic).

    ``asset`` is the asset FILENAME (its suffix selects tar/zip/no-extract).
    ``binary`` is the bare name to pick out of the archive (or the asset
    itself when ``extract`` is false). ``install_dir`` is the resolved target
    directory; ``rename`` optionally renames the installed file. ``checksum``
    is ``sha256:<hex>`` or ``None``.
    """

    asset: str
    binary: str
    install_dir: Path
    rename: str | None
    extract: bool
    chmod: str
    checksum: str | None


class InstallError(Exception):
    """A safety-gate or install failure. Carries an ``Outcome`` ``kind``.

    ``kind`` is ``Outcome.HARD`` for every failure this core raises — a bad
    checksum, an unsafe archive member, or a path-confinement breach are all
    attempted-and-failed safety gates that must gate the run's exit. Transient
    causes (network) never reach here; the caller maps those to ``SOFT``.
    """

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
    """Verify ``data``, extract safely, and atomically install the binary.

    Returns the installed :class:`Path` on success. Raises
    :class:`InstallError` (``kind=HARD``) on any checksum, archive-safety, or
    path-confinement failure. Nothing is written on any failure path — the
    only write is the final ``atomic_write_bytes`` after every gate passes.
    """
    _verify_checksum(data, spec.checksum, required=checksum_required)

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
        _atomic_install(binary_bytes, dest)
    return dest


# --- (a) checksum -------------------------------------------------------


def _verify_checksum(data: bytes, checksum: str | None, *, required: bool) -> None:
    """Verify ``data`` against ``checksum`` BEFORE any extract/read.

    ``checksum`` is ``<algo>:<hex>``. The algo must be in the allowlist and
    the hex the exact expected length; comparison is constant-time via
    :func:`hmac.compare_digest`. A ``None`` checksum is fatal only when
    ``required`` — that policy split is the caller's, never baked in here.
    """
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


# --- (e) install-path confinement (compose layer) -----------------------


def _confine(install_dir: Path, name: str) -> Path:
    """Compose ``install_dir / name`` and confirm it stays inside ``install_dir``.

    Re-rejects ``/`` and ``..`` in the bare name (config validation is the
    first layer; this is the independent second) AND realpaths the composed
    destination to assert containment.
    """
    if not name or "/" in name or ".." in name:
        raise InstallError(
            f"install target {name!r} must be a bare filename "
            "('/' and '..' are rejected)"
        )
    dest = (install_dir / name).resolve()
    if not _is_relative_to(dest, install_dir):
        raise InstallError(
            f"install target {dest} escapes the install directory {install_dir}"
        )
    return dest


# --- (b)(c) safe extraction + pick --------------------------------------


def _extract_and_pick(
    data: bytes,
    spec: InstallSpec,
    staging: Path,
    *,
    max_uncompressed: int,
) -> bytes:
    """Safely extract ``data`` into ``staging`` and return the picked binary's bytes.

    Dispatches on the asset suffix. Every member is independently validated
    (see :func:`_check_tar_member` / :func:`_check_zip_member`) before any
    write, and the summed decompressed size is capped.
    """
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
        # filter="data" is defense in depth ON TOP of the per-member loop
        # above; the loop is the primary defense because the stdlib filter has
        # had realpath/PATH_MAX bypasses (CVE-2025-4517) and zip has no filter
        # at all, so we never rely on it alone.
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
            _check_zip_member(info.filename, staging)
        # No zipfile equivalent of tar's data filter — the per-member check is
        # the ONLY defense, so extract members one at a time after validation.
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
    """Reject a tar member that could escape ``dest`` or plant a link."""
    if member.issym() or member.islnk():
        raise InstallError(
            f"archive member {member.name!r} is a "
            f"{'sym' if member.issym() else 'hard'}link — rejected"
        )
    _reject_escape(member.name, dest)


def _check_zip_member(name: str, dest: Path) -> None:
    """Reject a zip entry whose resolved path escapes ``dest``."""
    _reject_escape(name, dest)


def _reject_escape(name: str, dest: Path) -> None:
    if name.startswith("/") or os.path.isabs(name):
        raise InstallError(f"archive member {name!r} has an absolute path — rejected")
    resolved = (dest / name).resolve()
    if not _is_relative_to(resolved, dest.resolve()):
        raise InstallError(
            f"archive member {name!r} escapes the extraction directory — rejected"
        )


def _read_picked(staging: Path, binary: str) -> bytes:
    """Return the bytes of ``binary`` under ``staging``, confined to it.

    ``binary`` may name a nested path inside the archive; the resolved path is
    re-confined to ``staging`` and must be a regular file.
    """
    if binary.startswith("/") or ".." in Path(binary).parts:
        raise InstallError(f"binary path {binary!r} must stay inside the archive")
    picked = (staging / binary).resolve()
    if not _is_relative_to(picked, staging.resolve()):
        raise InstallError(f"binary path {binary!r} escapes the archive — rejected")
    if not picked.is_file():
        raise InstallError(f"binary {binary!r} not found in the archive")
    return picked.read_bytes()


# --- (f) atomic install -------------------------------------------------


def _atomic_install(binary_bytes: bytes, dest: Path) -> None:
    """Atomically write ``binary_bytes`` to ``dest`` with the exec bit set.

    The exec bit is applied to the temp fd BEFORE the rename (the deploy
    fchmod-before-replace idiom, via :func:`atomic_write_bytes`'s ``mode=``),
    so the published file is never briefly non-executable and the write is
    never a partial in-place mutation.
    """
    atomic_write_bytes(dest, binary_bytes, mode=_exec_mode())


def _exec_mode() -> int:
    """0755 — owner rwx, group/other rx (the ``+x`` binary convention)."""
    return stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH


def _is_relative_to(path: Path, base: Path) -> bool:
    """``Path.is_relative_to`` — spelled out for an explicit boolean return."""
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True
