"""The local :class:`Provisioner`: install from a config-repo tracked file."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from pathlib import Path

from setforge.config import LocalPackage
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
from setforge.source import get_resolved_source, resolve_source_dir

__all__ = ["LocalProvisioner", "LocalSourceError"]

LOGGER: logging.Logger = logging.getLogger(__name__)


class LocalSourceError(Exception):
    pass


def _resolve_tracked_source(tracked_root: Path, rel: str) -> Path:
    # realpath both sides: catches a symlink under tracked/ aimed outside it too.
    real_root = Path(tracked_root).resolve()
    candidate = (real_root / rel).resolve()
    if not candidate.is_relative_to(real_root):
        raise LocalSourceError(
            f"tracked source {rel!r} resolves to {candidate}, which escapes "
            f"the tracked root {real_root} — rejected"
        )
    if not candidate.is_file():
        raise LocalSourceError(
            f"tracked source {rel!r} not found at {candidate} "
            "(expected a regular file under the tracked root)"
        )
    return candidate


@register("local")
class LocalProvisioner(Provisioner):
    type = "local"

    def __init__(
        self,
        *,
        receipts: ReceiptStore | None = None,
        tracked_root: Path | None = None,
    ) -> None:
        self._receipts = receipts or ReceiptStore(default_receipt_root())
        self._tracked_root = tracked_root

    def probe(self) -> set[Identity]:
        return self._receipts.installed_for(self.type)

    def plan(
        self, items: Sequence[ProvisionItem], installed: set[Identity]
    ) -> ProvisionDelta:
        return ProvisionDelta(
            installed=tuple(
                item.identity
                for item in items
                if item.identity not in installed or self._is_stale(item)
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
                locator=str(entry.path) if entry is not None and entry.path else None,
                fingerprint=entry.source_digest if entry is not None else None,
                checksum=entry.checksum if entry is not None else None,
            )
            for identity in sorted(installed, key=lambda value: value.key)
        )

    def _source_digest(self, item: ProvisionItem) -> str | None:
        """sha256 of the tracked bytes this item installs from, or None.

        None means "cannot read the source" — a missing file, a path that
        escapes the tracked root, an unreadable one. Callers treat that as
        NOT stale, so a declared package whose tracked file has since gone
        missing is left alone rather than escalated into a failure.
        """
        pkg = item.config
        if not isinstance(pkg, LocalPackage):  # pragma: no cover
            return None
        try:
            source = _resolve_tracked_source(self._resolve_tracked_root(), pkg.path)
            return hashlib.sha256(source.read_bytes()).hexdigest()
        except (LocalSourceError, OSError):
            return None

    def _is_stale(self, item: ProvisionItem) -> bool:
        """True when an ALREADY-INSTALLED item's source bytes no longer match.

        This is the whole reason ``local`` needs a content check at all. Its
        identity is the bare binary name (:func:`package_identity`) and it
        carries no version, so nothing else can distinguish a rebuilt tracked
        binary from the one already deployed: ``plan`` dropped the item from
        the delta forever, ``driver.reconcile`` therefore never called
        ``apply_one`` for it, and the install reported success while the live
        binary kept the old bytes.

        A receipt with no recorded digest — every receipt written before
        ``source_digest`` existed — reads as stale exactly once. The reinstall
        records the digest, so the next run settles. That is what makes the fix
        self-healing for receipts already on disk.
        """
        current = self._source_digest(item)
        if current is None:
            return False
        return current != self._receipts.digest_for(item.identity, provider=self.type)

    def apply_one(self, item: ProvisionItem) -> ProvisionOutcome:
        # Same predicate `plan` used, not a second copy of the rule — the two
        # disagreeing is how a reinstall gets planned and then skipped.
        if item.identity in self.probe() and not self._is_stale(item):
            return ProvisionOutcome(item=item, outcome=Outcome.SKIP, detail="present")

        pkg = item.config
        if not isinstance(pkg, LocalPackage):  # pragma: no cover
            raise AssertionError(
                f"local item carried {type(pkg).__name__}, not LocalPackage"
            )

        try:
            source = _resolve_tracked_source(self._resolve_tracked_root(), pkg.path)
            data = source.read_bytes()
        except LocalSourceError as exc:
            # Escape or missing file: both HARD, never silently skipped (repro gate).
            LOGGER.warning("local source rejected for %s: %s", pkg.path, exc)
            return ProvisionOutcome(item=item, outcome=Outcome.HARD, detail=str(exc))
        except OSError as exc:  # pragma: no cover
            LOGGER.warning("reading local source %s failed: %s", pkg.path, exc)
            return ProvisionOutcome(item=item, outcome=Outcome.HARD, detail=str(exc))

        spec = InstallSpec(
            asset=pkg.path,
            binary=pkg.binary,
            install_dir=Path(pkg.install).expanduser(),
            rename=pkg.rename,
            extract=pkg.extract,
            chmod=pkg.chmod,
            checksum=pkg.checksum,
        )
        try:
            # Checksum optional here (bit-rot guard, not required like github_release).
            dest = install_from_bytes(data, spec, checksum_required=False)
        except InstallError as exc:
            LOGGER.warning("install failed for local %s: %s", pkg.path, exc)
            return ProvisionOutcome(item=item, outcome=exc.kind, detail=str(exc))

        self._receipts.record(
            item.identity,
            version=None,
            checksum=pkg.checksum,
            path=dest,
            # The bytes actually installed from, so the next run can tell a
            # rebuilt source from an unchanged one. Hashed here rather than
            # reusing `_source_digest` so it describes exactly what was
            # written, not a possibly re-read file.
            source_digest=hashlib.sha256(data).hexdigest(),
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

    def _resolve_tracked_root(self) -> Path:
        # Lazy: mirrors install.py's tracked_root derivation via the source layer.
        if self._tracked_root is not None:
            return self._tracked_root
        source_dir = resolve_source_dir(get_resolved_source())
        return source_dir / "tracked"
