"""Install-receipt store (spec §4).

A per-package marker for list-less ecosystems (go, github_release): each
install writes ONE receipt file recording the identity + version + checksum,
atomically and immediately after the install succeeds. :meth:`installed`
reads every receipt at call time so the skip decision rests on on-disk
ground truth, never in-memory state.

This is NOT the future resolved-graph lockfile (``setforge.lock`` — a resolved
dependency graph + CI drift-gate, a different file, location, and schema). The
default root is ``receipts/`` under the
setforge state dir, deliberately distinct from the ``locks/`` directory and
any ``.lock`` filename.
"""

import hashlib
import json
from pathlib import Path

from setforge.atomicio import atomic_write_text
from setforge.errors import CorruptReceiptError
from setforge.provision.protocol import Identity
from setforge.transitions import state_root

_RECEIPT_SUFFIX = ".json"


def default_receipt_root() -> Path:
    """Return the real per-host receipt directory.

    ``state_root() / "receipts"`` — NOT the future resolved-graph lockfile
    (``setforge.lock``) and NOT the ``locks/`` lock directory. A caller
    passing an explicit ``root`` (tests) bypasses this entirely.
    """
    return state_root() / "receipts"


def _receipt_name(identity: Identity) -> str:
    """Derive a filesystem-safe receipt filename from an identity key.

    The key is hashed so arbitrary key characters (slashes, spaces) can
    never escape the receipt directory. One key maps to one filename, so a
    re-record replaces the same file.
    """
    digest = hashlib.sha256(identity.key.encode("utf-8")).hexdigest()
    return f"{digest}{_RECEIPT_SUFFIX}"


class ReceiptStore:
    """Atomic per-package install receipts under ``root``."""

    def __init__(self, root: Path) -> None:
        """Bind the store to ``root`` (tests pass ``tmp_path``)."""
        self._root = root

    def record(
        self,
        identity: Identity,
        *,
        version: str | None,
        checksum: str | None,
        path: Path | str | None = None,
    ) -> None:
        """Write one receipt for ``identity`` atomically, replacing any prior.

        Called by a marker-based provisioner immediately after an install
        succeeds. Reuses :func:`~setforge.atomicio.atomic_write_text` so a
        crash mid-write never leaves a torn file.
        """
        self._root.mkdir(parents=True, exist_ok=True)
        payload = {
            "key": identity.key,
            "display": identity.display,
            "version": version,
            "checksum": checksum,
            "path": str(path) if path is not None else None,
        }
        atomic_write_text(
            self._root / _receipt_name(identity),
            json.dumps(payload, sort_keys=True) + "\n",
        )

    def remove(self, identity: Identity) -> None:
        # Idempotent: a missing file is a silent no-op, so revert can re-run safely.
        (self._root / _receipt_name(identity)).unlink(missing_ok=True)

    def installed(self) -> set[Identity]:
        """Return every recorded identity, read fresh from disk.

        Top-of-run ground truth: never trusts in-memory state. A missing
        root reads as empty. Only final ``*.json`` receipts are read, so a
        stray ``.tmp`` from a crashed atomic write is ignored. A malformed
        receipt raises :class:`~setforge.errors.CorruptReceiptError` naming
        the path rather than leaking a raw parse error into reconcile.
        """
        result: set[Identity] = set()
        if not self._root.is_dir():
            return result
        for path in self._root.glob(f"*{_RECEIPT_SUFFIX}"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                result.add(Identity(key=data["key"], display=data["display"]))
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise CorruptReceiptError(path) from exc
        return result

    def remove(self, identity: Identity) -> None:
        """Delete ``identity``'s receipt file, if present (idempotent).

        The receipt-side half of the revert/uninstall path for a marker
        ecosystem: a missing file is a no-op, so a double uninstall never
        raises. Best-effort unlink of the recorded binary is the caller's
        concern; this touches only the receipt.
        """
        self._root.joinpath(_receipt_name(identity)).unlink(missing_ok=True)

    def path_for(self, identity: Identity) -> Path | None:
        # Only source of an UNDECLARED item's install path (needed to unlink it).
        receipt = self._root / _receipt_name(identity)
        if not receipt.is_file():
            return None
        try:
            data = json.loads(receipt.read_text(encoding="utf-8"))
            # Discarded: validates key/display shape only, to raise CorruptReceiptError
            # on a malformed receipt before returning a path from it.
            Identity(key=data["key"], display=data["display"])
            recorded = data.get("path")
            return Path(recorded) if recorded is not None else None
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise CorruptReceiptError(receipt) from exc
