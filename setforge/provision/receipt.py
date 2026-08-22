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

from setforge.atomicio import atomic_write_text, fsync_dir
from setforge.errors import CorruptReceiptError
from setforge.provision.protocol import Identity
from setforge.transitions import state_root

_RECEIPT_SUFFIX = ".json"


@dataclass(slots=True, frozen=True)
class ReceiptEntry:
    identity: Identity | None
    path: Path | None
    corrupt_path: Path | None
    provider: str | None = None
    version: str | None = None
    checksum: str | None = None
    source_digest: str | None = None
    artifact: str | None = None
    platform: str | None = None


def default_receipt_root() -> Path:
    """Return the real per-host receipt directory.

    ``state_root() / "receipts"`` — NOT the future resolved-graph lockfile
    (``setforge.lock``) and NOT the ``locks/`` lock directory. A caller
    passing an explicit ``root`` (tests) bypasses this entirely.
    """
    return state_root() / "receipts"


def _receipt_name(identity: Identity, provider: str | None = None) -> str:
    """Derive a filesystem-safe receipt filename from an identity key.

    The key is hashed so arbitrary key characters (slashes, spaces) can
    never escape the receipt directory. One key maps to one filename, so a
    re-record replaces the same file.
    """
    coordinate = identity.key if provider is None else f"{provider}\0{identity.key}"
    digest = hashlib.sha256(coordinate.encode("utf-8")).hexdigest()
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
        source_digest: str | None = None,
        provider: str | None = None,
        artifact: str | None = None,
        platform: str | None = None,
    ) -> None:
        """Write one receipt for ``identity`` atomically, replacing any prior.

        Called by a marker-based provisioner immediately after an install
        succeeds. Reuses :func:`~setforge.atomicio.atomic_write_text` so a
        crash mid-write never leaves a torn file.

        ``source_digest`` is the digest of the bytes the install was made
        FROM — distinct from ``checksum``, which is the value the config
        *declared*. A provisioner whose identity carries no version (``local``)
        needs it to tell a rebuilt source from an unchanged one; the others
        leave it None.
        """
        self._root.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "2.0" if provider is not None else "1.0",
            "provider": provider,
            "key": identity.key,
            "display": identity.display,
            "version": version,
            "checksum": checksum,
            "path": str(path) if path is not None else None,
            "source_digest": source_digest,
            "artifact": artifact,
            "platform": platform,
        }
        atomic_write_text(
            self._root / _receipt_name(identity, provider),
            json.dumps(payload, sort_keys=True) + "\n",
        )

    def remove(self, identity: Identity, *, provider: str | None = None) -> None:
        # Idempotent: cleanup's crash-consistent retry must not raise if already reaped.
        names = [_receipt_name(identity, provider)]
        for name in names:
            try:
                (self._root / name).unlink()
            except FileNotFoundError:
                continue
            fsync_dir(self._root)

    def receipt_path(self, identity: Identity, *, provider: str | None) -> Path:
        """Return the exact journal path for one typed or legacy receipt."""
        return self._root / _receipt_name(identity, provider)

    def migrate_legacy(self, identity: Identity, *, provider: str) -> ReceiptEntry:
        """Upgrade one unambiguous legacy receipt to provider-qualified schema."""
        entry = self.entry_for(identity, provider)
        if entry is None or entry.provider is not None:
            raise CorruptReceiptError(self._root)
        self.record(
            identity,
            version=entry.version,
            checksum=entry.checksum,
            path=entry.path,
            source_digest=entry.source_digest,
            provider=provider,
            artifact=entry.artifact,
            platform=entry.platform,
        )
        self.remove(identity)
        migrated = self.entry_for(identity, provider)
        if migrated is None or migrated.provider != provider:  # pragma: no cover
            raise CorruptReceiptError(self._root)
        return migrated

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
            except FileNotFoundError:
                # Unlinked between glob enumeration and read: not corrupt, just gone.
                continue
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise CorruptReceiptError(path) from exc
        return result

    def installed_for(self, provider: str) -> set[Identity]:
        """Return current typed receipts plus unambiguous legacy receipts."""
        return {
            entry.identity
            for entry in self.iter_receipts()
            if entry.identity is not None and entry.provider in {None, provider}
        }

    def iter_receipts(self) -> Iterator[ReceiptEntry]:
        # Unlike installed(), a corrupt file yields a corrupt_path entry, not a raise.
        if not self._root.is_dir():
            return
        for path in sorted(self._root.glob(f"*{_RECEIPT_SUFFIX}")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                identity = Identity(key=data["key"], display=data["display"])
                recorded = data.get("path")
            except FileNotFoundError:
                # Unlinked between glob enumeration and read: not corrupt, just gone.
                continue
            except (json.JSONDecodeError, KeyError, TypeError):
                yield ReceiptEntry(identity=None, path=None, corrupt_path=path)
                continue
            bin_path = Path(recorded) if recorded is not None else None
            yield ReceiptEntry(
                identity=identity,
                path=bin_path,
                corrupt_path=None,
                provider=(
                    data.get("provider")
                    if isinstance(data.get("provider"), str)
                    else None
                ),
                version=(
                    data.get("version")
                    if isinstance(data.get("version"), str)
                    else None
                ),
                checksum=(
                    data.get("checksum")
                    if isinstance(data.get("checksum"), str)
                    else None
                ),
                source_digest=(
                    data.get("source_digest")
                    if isinstance(data.get("source_digest"), str)
                    else None
                ),
                artifact=(
                    data.get("artifact")
                    if isinstance(data.get("artifact"), str)
                    else None
                ),
                platform=(
                    data.get("platform")
                    if isinstance(data.get("platform"), str)
                    else None
                ),
            )

    def entry_for(self, identity: Identity, provider: str) -> ReceiptEntry | None:
        """Read one provider-qualified receipt, falling back to legacy evidence."""
        identity_matches = [
            entry for entry in self.iter_receipts() if entry.identity == identity
        ]
        if any(entry.provider is None for entry in identity_matches) and any(
            entry.provider is not None for entry in identity_matches
        ):
            raise CorruptReceiptError(self._root)
        matches = [
            entry for entry in identity_matches if entry.provider in {None, provider}
        ]
        typed = [entry for entry in matches if entry.provider == provider]
        if len(matches) > 1:
            raise CorruptReceiptError(self._root)
        if len(typed) == 1:
            return typed[0]
        if len(typed) > 1:
            raise CorruptReceiptError(self._root)
        return matches[0] if matches else None

    def digest_for(
        self, identity: Identity, *, provider: str | None = None
    ) -> str | None:
        """Return the recorded ``source_digest``, or None when there is none.

        None covers three cases the caller must treat alike: no receipt, a
        receipt written before this field existed, and a provisioner that does
        not record one. All three mean "cannot prove the source is unchanged".
        """
        receipt = self._lookup_path(identity, provider)
        if not receipt.is_file():
            return None
        try:
            data = json.loads(receipt.read_text(encoding="utf-8"))
            # Discarded: validates key/display shape only, so a malformed
            # receipt raises here rather than reading a digest off it.
            Identity(key=data["key"], display=data["display"])
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise CorruptReceiptError(receipt) from exc
        recorded = data.get("source_digest")
        return recorded if isinstance(recorded, str) else None

    def path_for(
        self, identity: Identity, *, provider: str | None = None
    ) -> Path | None:
        # Only source of an UNDECLARED item's install path (needed to unlink it).
        receipt = self._lookup_path(identity, provider)
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

    def _lookup_path(self, identity: Identity, provider: str | None) -> Path:
        typed = self._root / _receipt_name(identity, provider)
        if typed.is_file():
            return typed
        legacy = self._root / _receipt_name(identity)
        if provider is not None or legacy.is_file():
            return legacy
        matches: list[Path] = []
        if self._root.is_dir():
            for candidate in self._root.glob(f"*{_RECEIPT_SUFFIX}"):
                try:
                    raw = json.loads(candidate.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if raw.get("key") == identity.key:
                    matches.append(candidate)
        if len(matches) > 1:
            raise CorruptReceiptError(self._root)
        return matches[0] if matches else legacy
