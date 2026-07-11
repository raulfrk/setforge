"""The github_release :class:`Provisioner`.

Downloads a release asset over HTTPS and hands the bytes to the shared
installer core (:mod:`setforge.provision.installer`) — the security-hardened
verify→extract→install pipeline this module shares with ``local``. Marker
based: each success writes one :class:`ReceiptStore` receipt recording the
install path so probe/skip and uninstall rest on on-disk ground truth.

Outcome model:

* a MISSING checksum or a checksum MISMATCH → ``HARD`` (a safety gate failed;
  this is the one place this module emits ``HARD``, and nothing is installed);
* an unsafe archive member (path escape, symlink/hardlink) or a decompression
  bomb → ``HARD`` (attempted and failed a safety gate), surfaced by the core;
* a transient DOWNLOAD failure → ``SOFT`` (couldn't attempt — never gates exit);
* an already-present item → ``SKIP`` (probed before any download; writes nothing).

The download deliberately attaches NO auth: public release assets need none,
which sidesteps the redirect-auth-leak class entirely. Only HTTPS URLs are
accepted.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path

from setforge.config import GitHubReleasePackage
from setforge.provision.installer import (
    InstallError,
    InstallSpec,
    install_from_bytes,
)
from setforge.provision.protocol import (
    Identity,
    Outcome,
    ProvisionDelta,
    Provisioner,
    ProvisionItem,
    ProvisionOutcome,
)
from setforge.provision.receipt import ReceiptStore, default_receipt_root
from setforge.provision.registry import register

__all__ = ["DownloadError", "GitHubReleaseProvisioner"]

LOGGER: logging.Logger = logging.getLogger(__name__)

_DOWNLOAD_TIMEOUT_S = 120
# Wire-size cap: refuse to buffer more than this many downloaded bytes, so a
# hostile server cannot exhaust memory before the decompressed-size guard runs.
_MAX_WIRE_BYTES = 512 * 1024 * 1024  # 512 MiB
_CHUNK = 64 * 1024


class DownloadError(Exception):
    """A transient asset-download failure (network/HTTP/oversize).

    Distinct from :class:`~setforge.provision.installer.InstallError`: a
    download failure is SOFT (couldn't attempt), never a safety-gate breach.
    """


def _asset_url(pkg: GitHubReleasePackage) -> str:
    """Compose the release-asset download URL (always HTTPS)."""
    return f"https://github.com/{pkg.repo}/releases/download/{pkg.tag}/{pkg.asset}"


@register("github_release")
class GitHubReleaseProvisioner(Provisioner):
    """Provision a binary from a GitHub release asset (marker-based)."""

    type = "github_release"

    def __init__(self, *, receipts: ReceiptStore | None = None) -> None:
        """Bind to a :class:`ReceiptStore` (defaults to the per-host root).

        Tests inject a ``tmp_path``-backed store; production uses
        :func:`default_receipt_root`. ``build()`` calls ``cls()`` with no
        args, so the default must resolve the real root.
        """
        self._receipts = receipts or ReceiptStore(default_receipt_root())

    def probe(self) -> set[Identity]:
        """Return the identities the receipt store records (read-only)."""
        return self._receipts.installed()

    def plan(
        self, items: Sequence[ProvisionItem], installed: set[Identity]
    ) -> ProvisionDelta:
        return ProvisionDelta(
            installed=tuple(
                item.identity for item in items if item.identity not in installed
            )
        )

    def apply_one(self, item: ProvisionItem) -> ProvisionOutcome:
        if item.identity in self.probe():
            return ProvisionOutcome(item=item, outcome=Outcome.SKIP, detail="present")

        pkg = item.config
        if not isinstance(pkg, GitHubReleasePackage):  # pragma: no cover - typing guard
            raise AssertionError(
                f"github_release item carried {type(pkg).__name__}, not "
                "GitHubReleasePackage"
            )

        # Fail closed on a missing checksum BEFORE spending a download.
        if pkg.checksum is None:
            return ProvisionOutcome(
                item=item,
                outcome=Outcome.HARD,
                detail="a checksum is required for github_release assets",
            )

        url = _asset_url(pkg)
        try:
            data = self._download(url)
        except DownloadError as exc:
            LOGGER.warning("download failed for %s: %s", url, exc)
            return ProvisionOutcome(item=item, outcome=Outcome.SOFT, detail=str(exc))

        spec = InstallSpec(
            asset=pkg.asset,
            binary=pkg.binary,
            install_dir=Path(pkg.install).expanduser(),
            rename=pkg.rename,
            extract=pkg.extract,
            chmod=pkg.chmod,
            checksum=pkg.checksum,
        )
        try:
            dest = install_from_bytes(data, spec, checksum_required=True)
        except InstallError as exc:
            LOGGER.warning("install failed for %s: %s", pkg.repo, exc)
            return ProvisionOutcome(item=item, outcome=exc.kind, detail=str(exc))

        self._receipts.record(
            item.identity,
            version=pkg.tag,
            checksum=pkg.checksum,
            path=dest,
        )
        return ProvisionOutcome(
            item=item, outcome=Outcome.OK, detail=f"installed {dest}"
        )

    def uninstall_one(self, identity: Identity) -> None:
        """Remove the receipt and best-effort unlink the recorded binary."""
        recorded = self._receipts.path_for(identity)
        if recorded is not None:
            recorded.unlink(missing_ok=True)
        self._receipts.remove(identity)

    def _download(self, url: str) -> bytes:
        """Fetch ``url`` over HTTPS with NO auth, streamed under a wire cap.

        HTTPS-only (rejects any other scheme). Attaching no credentials
        sidesteps the redirect-auth-leak class outright. Reads in chunks and
        aborts once the running total exceeds :data:`_MAX_WIRE_BYTES`. Any
        network/HTTP/oversize condition raises :class:`DownloadError` (SOFT).
        """
        if not url.startswith("https://"):
            raise DownloadError(f"refusing non-HTTPS asset URL: {url!r}")
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(
                request, timeout=_DOWNLOAD_TIMEOUT_S
            ) as response:
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = response.read(_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _MAX_WIRE_BYTES:
                        raise DownloadError(
                            f"asset exceeds the {_MAX_WIRE_BYTES}-byte wire cap"
                        )
                    chunks.append(chunk)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DownloadError(f"network error fetching {url}: {exc}") from exc
        return b"".join(chunks)
