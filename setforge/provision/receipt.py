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
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from setforge.atomicio import atomic_write_text
from setforge.errors import CorruptReceiptError
from setforge.provision.protocol import Identity
from setforge.transitions import state_root

_RECEIPT_SUFFIX = ".json"


@dataclass(slots=True, frozen=True)
class ReceiptEntry:
    """One receipt read off disk, or a marker that its file was corrupt.

    A fault-tolerant read result for :meth:`ReceiptStore.iter_receipts`.
    Exactly one of ``identity`` / ``corrupt_path`` is set: a good read
    carries the parsed ``identity`` + recorded ``path`` (``None`` for an
    old pre-path receipt); a corrupt read carries the offending
    ``corrupt_path`` so the caller can name + skip that one file and
    continue reading the rest.
    """

    identity: Identity | None
    path: Path | None
    corrupt_path: Path | None


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
        """Delete ``identity``'s receipt file (idempotent).

        The receipt-unlink step of the ``cleanup`` wizard's crash-consistent
        ordering (binary-unlink → receipt-unlink). Removing an absent receipt
        is a no-op — a crash that already reaped the file must not turn a
        re-run into a raise. Only the one named receipt is touched; the store
        never walks or recurses.
        """
        receipt = self._root / _receipt_name(identity)
        try:
            receipt.unlink()
        except FileNotFoundError:
            return

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

    def iter_receipts(self) -> Iterator[ReceiptEntry]:
        """Yield one :class:`ReceiptEntry` per receipt file, fault-tolerantly.

        Unlike :meth:`installed` (which aborts the whole scan on the first
        corrupt receipt), a bad file yields a ``corrupt_path``-only entry so
        the caller can name + skip that one and keep reading the rest — the
        ``cleanup`` wizard's discovery must not be taken down by a single
        malformed receipt. A missing root yields nothing.
        """
        if not self._root.is_dir():
            return
        for path in sorted(self._root.glob(f"*{_RECEIPT_SUFFIX}")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                identity = Identity(key=data["key"], display=data["display"])
                recorded = data.get("path")
            except (json.JSONDecodeError, KeyError, TypeError):
                yield ReceiptEntry(identity=None, path=None, corrupt_path=path)
                continue
            bin_path = Path(recorded) if recorded is not None else None
            yield ReceiptEntry(identity=identity, path=bin_path, corrupt_path=None)

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
