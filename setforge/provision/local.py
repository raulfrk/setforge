"""The local :class:`Provisioner` — install a binary/archive from a tracked file.

The sibling of :mod:`setforge.provision.github_release`: same marker-based
receipt shape and shared install core, but the SOURCE is a file committed
under the config repo's ``tracked/`` tree (read locally) rather than a
downloaded release asset, and a checksum is OPTIONAL rather than required
(the source is version-controlled, so a checksum is a bit-rot guard, not the
sole integrity boundary).
"""

from __future__ import annotations

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
    Outcome,
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
    """A tracked source could not be read or escaped the tracked root."""


def _resolve_tracked_source(tracked_root: Path, rel: str) -> Path:
    """Confine ``rel`` under ``tracked_root`` and return the realpath'd file.

    Rejects any escape — a ``..`` component, an absolute ``rel``, or a symlink
    pointing outside the tree — by realpath-ing BOTH sides and requiring the
    candidate to stay under the tracked root. Raises :class:`LocalSourceError`
    (never reads) on escape, and again if the confined path is not a regular
    file. The realpath resolves symlinks, so a link inside ``tracked/`` aimed
    at a host path outside it is caught here, before any bytes are read.
    """
    real_root = Path(tracked_root).resolve()
    candidate = (real_root / rel).resolve()
    if not _is_relative_to(candidate, real_root):
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


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True


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
        # None ⇒ resolve the config-repo tracked root lazily at apply time via
        # the source layer (the same layer that produced the loaded config).
        # Tests inject an explicit root and bypass source resolution.
        self._tracked_root = tracked_root

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
        if not isinstance(pkg, LocalPackage):  # pragma: no cover - typing guard
            raise AssertionError(
                f"local item carried {type(pkg).__name__}, not LocalPackage"
            )

        try:
            source = _resolve_tracked_source(self._resolve_tracked_root(), pkg.path)
            data = source.read_bytes()
        except LocalSourceError as exc:
            # A confinement escape OR a missing/unreadable tracked file: both
            # are HARD — an attempted-and-failed install the operator fixes in
            # the config repo (a missing committed file is a config error; a
            # path escape is a safety violation that must gate).
            LOGGER.warning("local source rejected for %s: %s", pkg.path, exc)
            return ProvisionOutcome(item=item, outcome=Outcome.HARD, detail=str(exc))
        except OSError as exc:  # pragma: no cover - defensive read guard
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
            # checksum_required=False: a committed file needs no checksum, but
            # one IF present is still verified (bit-rot guard) by the core.
            dest = install_from_bytes(data, spec, checksum_required=False)
        except InstallError as exc:
            LOGGER.warning("install failed for local %s: %s", pkg.path, exc)
            return ProvisionOutcome(item=item, outcome=exc.kind, detail=str(exc))

        self._receipts.record(
            item.identity,
            version=None,
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

    def _resolve_tracked_root(self) -> Path:
        """Return the config-repo tracked root, resolving lazily if unset.

        An explicit root (tests, or a future caller that threads it) wins;
        otherwise resolve the active source via the same source layer that
        produced the loaded config, and append ``tracked`` (mirroring
        ``setforge/cli/install.py``'s ``tracked_root`` derivation).
        """
        if self._tracked_root is not None:
            return self._tracked_root
        source_dir = resolve_source_dir(get_resolved_source())
        return source_dir / "tracked"
