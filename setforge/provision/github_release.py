"""The github_release :class:`Provisioner`."""

from __future__ import annotations

import logging
import time
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
    ObservationOrigin,
    Outcome,
    PackageObservation,
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
# The socket timeout above applies per connect + per blocking read, NOT to the
# whole transfer, so a slow-drip server emitting a chunk just under it can hold
# the read loop open indefinitely. This is a wall-clock cap on the entire
# download; a few multiples of the per-read timeout leaves ample slack for a
# legitimately large, honestly-paced asset while bounding a hostile drip.
_DOWNLOAD_DEADLINE_S = 4 * _DOWNLOAD_TIMEOUT_S
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
        return self._receipts.installed_for(self.type)

    def plan(
        self, items: Sequence[ProvisionItem], installed: set[Identity]
    ) -> ProvisionDelta:
        return ProvisionDelta(
            installed=tuple(
                item.identity for item in items if item.identity not in installed
            )
        )

    def observations(self, installed: set[Identity]) -> tuple[PackageObservation, ...]:
        return tuple(
            PackageObservation(
                identity,
                ObservationOrigin.CURRENT_RECEIPT
                if (entry := self._receipts.entry_for(identity, self.type)) is not None
                and entry.provider is not None
                else ObservationOrigin.LEGACY_RECEIPT,
                version=entry.version if entry is not None else None,
                locator=str(entry.path) if entry is not None and entry.path else None,
                checksum=entry.checksum if entry is not None else None,
            )
            for identity in sorted(installed, key=lambda value: value.key)
        )

    def apply_one(self, item: ProvisionItem) -> ProvisionOutcome:
        entry = self._receipts.entry_for(item.identity, self.type)
        if (
            item.identity in self.probe()
            and entry is not None
            and (item.version is None or entry.version == item.version)
            and (item.checksum is None or entry.checksum == item.checksum)
        ):
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
            provider=self.type,
        )
        return ProvisionOutcome(
            item=item, outcome=Outcome.OK, detail=f"installed {dest}"
        )

    def uninstall_one(self, identity: Identity) -> None:
        recorded = self._receipts.path_for(identity, provider=self.type)
        if recorded is not None:
            recorded.unlink(missing_ok=True)
        self._receipts.remove(identity, provider=self.type)

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
                start = time.monotonic()
                while True:
                    chunk = response.read(_CHUNK)
                    if not chunk:
                        break
                    if time.monotonic() - start > _DOWNLOAD_DEADLINE_S:
                        raise DownloadError(
                            f"asset download exceeded the {_DOWNLOAD_DEADLINE_S}s "
                            "deadline"
                        )
                    total += len(chunk)
                    if total > _MAX_WIRE_BYTES:
                        raise DownloadError(
                            f"asset exceeds the {_MAX_WIRE_BYTES}-byte wire cap"
                        )
                    chunks.append(chunk)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DownloadError(f"network error fetching {url}: {exc}") from exc
        return b"".join(chunks)
