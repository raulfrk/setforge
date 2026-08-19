"""The cargo :class:`Resolver`: index prefix is length-bucketed on the
lowercased crate name (:func:`_index_path`) — a known trap."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import ClassVar

from packaging.version import InvalidVersion, Version

from setforge.config import CargoPackage
from setforge.errors import ResolveError
from setforge.provision.resolve._fetch import fetch_bytes
from setforge.provision.resolve.protocol import (
    IntegrityKind,
    PackageType,
    ResolvedPin,
)
from setforge.provision.resolve.registry import register

__all__ = ["CargoResolver", "registry_checksum"]

_INDEX_BASE = "https://index.crates.io"
_FETCH_TIMEOUT_S = 30
_MAX_INDEX_BYTES = 16 * 1024 * 1024

Fetch = Callable[[str], str]

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _index_path(name: str) -> str:
    lower = name.casefold()
    n = len(lower)
    if n == 1:
        return f"1/{lower}"
    if n == 2:
        return f"2/{lower}"
    if n == 3:
        return f"3/{lower[0]}/{lower}"
    return f"{lower[:2]}/{lower[2:4]}/{lower}"


def _default_fetch(url: str) -> str:
    body = fetch_bytes(url, timeout=_FETCH_TIMEOUT_S, max_bytes=_MAX_INDEX_BYTES)
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ResolveError(
            f"crates.io index response is not valid UTF-8: {exc}"
        ) from exc


def registry_checksum(crate: str, version: str, *, fetch: Fetch | None = None) -> str:
    """Return the exact registry checksum for ``crate`` at ``version``.

    Unlike resolution, this accepts a yanked row: an existing lock remains
    verifiable after a release is yanked. The returned value is normalized to
    SetForge's persisted ``sha256:<hex>`` form.
    """
    fetch_index = fetch if fetch is not None else _default_fetch
    body = fetch_index(f"{_INDEX_BASE}/{_index_path(crate)}")
    return f"sha256:{_pick_exact_checksum(body, crate, version).lower()}"


@register(PackageType.CARGO)
class CargoResolver:
    type: ClassVar[PackageType] = PackageType.CARGO

    def __init__(self, *, fetch: Fetch | None = None) -> None:
        self._fetch = fetch if fetch is not None else _default_fetch

    def resolve(self, item: object) -> ResolvedPin:
        if not isinstance(item, CargoPackage):  # pragma: no cover - typing guard
            raise ResolveError(
                f"cargo resolver received {type(item).__name__}, not CargoPackage"
            )
        crate = item.crate
        url = f"{_INDEX_BASE}/{_index_path(crate)}"
        body = self._fetch(url)
        vers, cksum = _pick_max_non_yanked(body, crate)
        return ResolvedPin(
            type=PackageType.CARGO,
            key=crate,
            version=vers,
            integrity=f"sha256:{cksum}",
            integrity_kind=IntegrityKind.CHECKSUM,
        )


def _pick_max_non_yanked(body: str, crate: str) -> tuple[str, str]:
    """Return ``(version, cksum)`` for the max non-yanked release (semver order)."""
    best: tuple[Version, str, str] | None = None
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ResolveError(
                f"crates.io index for {crate!r} has a non-JSON line: {exc}"
            ) from exc
        if not isinstance(entry, dict) or entry.get("yanked"):
            continue
        raw_vers = entry.get("vers")
        if not isinstance(raw_vers, str):
            continue
        cksum = _validated_row_checksum(entry, crate, raw_vers)
        try:
            ver = Version(raw_vers)
        except InvalidVersion:
            continue
        if best is None or ver > best[0]:
            best = (ver, raw_vers, cksum)
    if best is None:
        raise ResolveError(f"no non-yanked release found for crate {crate!r}")
    return best[1], best[2]


def _pick_exact_checksum(body: str, crate: str, version: str) -> str:
    """Return the validated SHA-256 field for one exact index row."""
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ResolveError(
                f"crates.io index for {crate!r} has a non-JSON line: {exc}"
            ) from exc
        if not isinstance(entry, dict) or entry.get("vers") != version:
            continue
        return _validated_row_checksum(entry, crate, version)
    raise ResolveError(f"crate {crate!r} has no registry release {version!r}")


def _validated_row_checksum(
    entry: Mapping[object, object], crate: str, version: str
) -> str:
    """Validate the identity and checksum fields shared by index consumers."""
    entry_name = entry.get("name")
    if not isinstance(entry_name, str) or entry_name.casefold() != crate.casefold():
        raise ResolveError(
            f"crates.io index row for {crate!r} {version!r} has wrong name"
        )
    checksum = entry.get("cksum")
    if not isinstance(checksum, str) or _SHA256_RE.fullmatch(checksum) is None:
        raise ResolveError(
            f"crates.io index checksum for {crate!r} {version!r} is malformed"
        )
    return checksum
