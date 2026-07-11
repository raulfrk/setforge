"""The github_release :class:`Provisioner`."""

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
_MAX_WIRE_BYTES = 512 * 1024 * 1024  # 512 MiB
_CHUNK = 64 * 1024


class DownloadError(Exception):
    pass


def _asset_url(pkg: GitHubReleasePackage) -> str:
    return f"https://github.com/{pkg.repo}/releases/download/{pkg.tag}/{pkg.asset}"


@register("github_release")
class GitHubReleaseProvisioner(Provisioner):
    type = "github_release"

    def __init__(self, *, receipts: ReceiptStore | None = None) -> None:
        self._receipts = receipts or ReceiptStore(default_receipt_root())

    def probe(self) -> set[Identity]:
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
        recorded = self._receipts.path_for(identity)
        if recorded is not None:
            recorded.unlink(missing_ok=True)
        self._receipts.remove(identity)

    def _download(self, url: str) -> bytes:
        if not url.startswith("https://"):
            raise DownloadError(f"refusing non-HTTPS asset URL: {url!r}")
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(
                request, timeout=_DOWNLOAD_TIMEOUT_S
            ) as response:
                final_url = response.geturl()
                # Re-checked on the final URL: the opener follows a https->http
                # redirect silently, so this is the only downgrade catch.
                if not final_url.startswith("https://"):
                    raise DownloadError(
                        f"refusing non-HTTPS redirect target: {final_url!r}"
                    )
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
