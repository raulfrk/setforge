"""The cargo :class:`Resolver` — resolves via the crates.io sparse index.

Queries ``https://index.crates.io/{prefix}/{name}`` for the concrete version +
``.crate`` sha256, WITHOUT installing (that stays with the provisioner). The
prefix is LENGTH-BUCKETED on the LOWERCASED crate name (:func:`_index_path`) —
the known trap, tested across all four buckets. The response is
newline-delimited JSON, one object per version; the resolver picks the MAX
non-yanked version (:mod:`packaging.version` order, not lexicographic) and emits
a ``sha256:{cksum}`` checksum pin.

The network boundary is injected (the ``fetch`` callable) so unit tests mock it;
the default fetch mirrors :mod:`setforge.provision.github_release`'s HTTPS-only,
redirect-downgrade-rejecting, explicitly-timed-out urllib discipline.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import ClassVar

from packaging.version import InvalidVersion, Version

from setforge.config import CargoPackage
from setforge.errors import ResolveError
from setforge.provision.resolve.protocol import (
    IntegrityKind,
    PackageType,
    ResolvedPin,
)
from setforge.provision.resolve.registry import register

__all__ = ["CargoResolver"]

_INDEX_BASE = "https://index.crates.io"
_FETCH_TIMEOUT_S = 30
_MAX_INDEX_BYTES = 16 * 1024 * 1024  # 16 MiB — well above any real crate index.

# A fetch takes the full index URL and returns the newline-delimited JSON body.
Fetch = Callable[[str], str]


def _index_path(name: str) -> str:
    """Return the length-bucketed sparse-index path segment for ``name``.

    crates.io buckets on the LOWERCASED name: 1-char -> ``1/{name}``; 2-char ->
    ``2/{name}``; 3-char -> ``3/{first}/{name}``; >=4-char ->
    ``{first-two}/{next-two}/{name}``. The bucket dirs use the lowercased form
    but the trailing name segment is the lowercased crate name too (the index is
    case-insensitive; crates.io normalizes to lowercase).
    """
    lower = name.lower()
    n = len(lower)
    if n == 1:
        return f"1/{lower}"
    if n == 2:
        return f"2/{lower}"
    if n == 3:
        return f"3/{lower[0]}/{lower}"
    return f"{lower[:2]}/{lower[2:4]}/{lower}"


def _default_fetch(url: str) -> str:
    """Fetch ``url`` over HTTPS with an explicit timeout, rejecting downgrades.

    Mirrors :meth:`GitHubReleaseProvisioner._download` (github_release.py
    ~:122-135): refuses a non-HTTPS URL up front AND re-checks the final URL so a
    silent https->http redirect cannot smuggle the fetch onto plaintext.
    """
    if not url.startswith("https://"):
        raise ResolveError(f"refusing non-HTTPS crates.io index URL: {url!r}")
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_S) as response:
            final_url = response.geturl()
            if not final_url.startswith("https://"):
                raise ResolveError(f"refusing non-HTTPS redirect target: {final_url!r}")
            body = response.read(_MAX_INDEX_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ResolveError(f"network error fetching {url}: {exc}") from exc
    if len(body) > _MAX_INDEX_BYTES:
        raise ResolveError(f"crates.io index for {url} exceeds the wire cap")
    return body.decode("utf-8")


@register(PackageType.CARGO)
class CargoResolver:
    """Resolve a crate to its max non-yanked version + ``.crate`` sha256."""

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
    """Return ``(version, cksum)`` for the max non-yanked release in ``body``.

    ``body`` is newline-delimited JSON, one object per version. Yanked entries
    are skipped; the winner is the :class:`packaging.version.Version` max (so
    ``1.0.100`` beats ``1.0.99``, unlike a string sort). Raises
    :class:`ResolveError` when no non-yanked, parseable version survives.
    """
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
        cksum = entry.get("cksum")
        if not isinstance(raw_vers, str) or not isinstance(cksum, str):
            continue
        try:
            ver = Version(raw_vers)
        except InvalidVersion:
            continue
        if best is None or ver > best[0]:
            best = (ver, raw_vers, cksum)
    if best is None:
        raise ResolveError(f"no non-yanked release found for crate {crate!r}")
    return best[1], best[2]
