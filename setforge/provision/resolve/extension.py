"""The extension (VS Code) :class:`Resolver` — resolves version + VSIX sha256.

The most complex resolver. An extension key is ``publisher.name`` (e.g.
``esbenp.prettier-vscode``). Unlike the other ecosystems, the VS Marketplace
publishes NO per-version checksum, so the pin's integrity is Trust-On-First-Use:
setforge downloads the VSIX ONCE, sha256s it, and records ITS OWN hash — exactly
what ``code --install-extension`` would consume. Two marketplace calls:

1. **Resolve the concrete version** — an ``extensionquery`` POST to
   ``…/gallery/extensionquery`` with a ``filters → criteria`` body (``filterType
   7`` = the ``publisher.name`` id) and a ``flags`` requesting the version list.
   The response's ``results[0].extensions[0].versions[]`` is newest-first; the
   concrete latest is ``versions[0].version`` (or the spec's pinned version).
2. **Download + hash the VSIX** — a ``vspackage`` GET built with the CONCRETE
   version (NEVER the ``latest`` alias — the marketplace serves a different
   sha256 for ``latest`` vs a concrete version, pitfall B10-latest). The
   response may be gzip transfer-encoded; the hash is computed on the DECODED
   VSIX bytes.

The POST + GET boundary is injected (the ``fetch`` callable) so unit tests never
touch marketplace.visualstudio.com; the default delegates the HTTPS-only +
redirect-downgrade + timeout + wire-cap discipline to
:func:`~setforge.provision.resolve._fetch.fetch_bytes` (extended with a POST /
gzip-decode path). TOFU discipline (pitfall B10-tofu-unverified-hash): the hash
is computed ONLY on bytes returned through that guarded path — a downgraded or
redirected fetch raises before any hashing.

The extensionquery body/response shape is the cleanest plausible one built from
the documented field names; if it drifts from the live API, Task-6's docker e2e
(a real ``setforge lock`` for a real extension) corrects it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from setforge.errors import ResolveError
from setforge.provision.resolve._fetch import fetch_bytes
from setforge.provision.resolve.protocol import (
    IntegrityKind,
    PackageType,
    ResolvedPin,
)
from setforge.provision.resolve.registry import register

__all__ = ["ExtensionResolveItem", "ExtensionResolver"]

_GALLERY_BASE = "https://marketplace.visualstudio.com/_apis/public/gallery"
_QUERY_URL = f"{_GALLERY_BASE}/extensionquery"
_LATEST = "latest"
_FETCH_TIMEOUT_S = 30.0
# The extensionquery JSON is small; the VSIX can be MB-scale (mirror the
# github_release asset ceiling, big enough for any real VSIX but bounded so a
# runaway/hostile response cannot exhaust memory — pitfall B10 wire cap).
_MAX_QUERY_BYTES = 16 * 1024 * 1024
_MAX_VSIX_BYTES = 512 * 1024 * 1024
# Marketplace deployments occasionally 400 a UA-less / client-id-less request;
# send a User-Agent defensively (harmless when unneeded).
_USER_AGENT = "setforge-lock-resolver"
# extensionquery negotiates its JSON shape via an api-version'd Accept header.
_ACCEPT = "application/json;api-version=7.2-preview.1"
_CONTENT_TYPE = "application/json"
# extensionquery flag bits: IncludeVersions (0x1) | IncludeFiles (0x2) |
# IncludeVersionProperties (0x10) — enough to get the versions[] list back.
_QUERY_FLAGS = 0x1 | 0x2 | 0x10
# filterType 7 = "extension name" (the publisher.name id) in the gallery API.
_FILTER_TYPE_EXTENSION_NAME = 7

# A fetch takes the URL + optional UA/POST-body/headers/gzip-decode and returns
# the raw (or decoded) bytes.
Fetch = Callable[..., bytes]


class ExtensionResolveItem(BaseModel):
    """The input an :class:`ExtensionResolver` resolves.

    ``key`` is the ``publisher.name`` extension id (the lock key). ``version``
    optionally pins a concrete version; when ``None`` the resolver picks the
    marketplace's latest. Distinct from the raw config ``include:`` string so a
    future ``@version`` pin syntax has a typed home without reusing
    ``vscode_extensions._EXT_ID_RE`` (which would drop a suffix).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    version: str | None = None


def _default_fetch(
    url: str,
    *,
    user_agent: str | None = None,
    data: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    decode_gzip: bool = False,
) -> bytes:
    """Fetch ``url`` via the shared capped fetch helper (GET or POST).

    A thin wrapper matching the injected test double's signature; delegates
    HTTPS-only + redirect-downgrade + timeout + wire-cap (and the POST body /
    gzip-decode paths) to
    :func:`~setforge.provision.resolve._fetch.fetch_bytes`. The wire cap differs
    per call (tiny query JSON vs MB-scale VSIX), so the caller picks it.
    """
    # data is None -> the VSIX GET (MB-scale cap); a POST body -> the small
    # query JSON. A future third call site must pick its cap explicitly rather
    # than silently inheriting the 512 MiB VSIX ceiling.
    max_bytes = _MAX_VSIX_BYTES if data is None else _MAX_QUERY_BYTES
    return fetch_bytes(
        url,
        timeout=_FETCH_TIMEOUT_S,
        max_bytes=max_bytes,
        user_agent=user_agent,
        data=data,
        headers=headers,
        decode_gzip=decode_gzip,
    )


@register(PackageType.EXTENSION)
class ExtensionResolver:
    """Resolve a VS Code extension to its concrete version + VSIX sha256 (TOFU)."""

    type: ClassVar[PackageType] = PackageType.EXTENSION

    def __init__(self, *, fetch: Fetch | None = None) -> None:
        self._fetch = fetch if fetch is not None else _default_fetch

    def resolve(self, item: object) -> ResolvedPin:
        if not isinstance(item, ExtensionResolveItem):  # pragma: no cover - guard
            raise ResolveError(
                f"extension resolver received {type(item).__name__}, not "
                "ExtensionResolveItem"
            )
        publisher, name = _split_ext_id(item.key)
        version = self._resolve_version(item.key, item.version)
        digest = self._hash_vsix(publisher, name, version)
        return ResolvedPin(
            type=PackageType.EXTENSION,
            key=item.key,
            version=version,
            integrity=f"sha256:{digest}",
            integrity_kind=IntegrityKind.CHECKSUM,
        )

    def _resolve_version(self, ext_id: str, pinned: str | None) -> str:
        """Return the concrete version: the pin verbatim, or the query's latest.

        A concrete pin is used as-is (no assumption it is ``latest``); ``latest``
        is NEVER a valid stored value, so a pin literally equal to ``latest`` is
        rejected. Otherwise the extensionquery response's first (newest) version
        is picked.
        """
        if pinned is not None:
            if pinned == _LATEST:
                raise ResolveError(
                    f"extension {ext_id!r} pinned to the {_LATEST!r} alias; a lock "
                    "must record a concrete version"
                )
            return pinned
        body = self._fetch(
            _QUERY_URL,
            user_agent=_USER_AGENT,
            data=_query_body(ext_id),
            headers={"Accept": _ACCEPT, "Content-Type": _CONTENT_TYPE},
        )
        return _pick_latest_version(body, ext_id)

    def _hash_vsix(self, publisher: str, name: str, version: str) -> str:
        """Fetch the VSIX ONCE (gzip-decoded) and return its sha256 hex digest.

        The URL is built with the CONCRETE ``version`` (never ``latest``). The
        fetch runs through the guarded path, so a downgraded/redirected response
        raises BEFORE any bytes are hashed (TOFU discipline, pitfall
        B10-tofu-unverified-hash). ``decode_gzip`` ensures the hash covers the
        VSIX bytes ``code --install-extension`` consumes, not the wire bytes.
        """
        url = (
            f"{_GALLERY_BASE}/publishers/{publisher}/vsextensions/"
            f"{name}/{version}/vspackage"
        )
        data = self._fetch(url, user_agent=_USER_AGENT, decode_gzip=True)
        return hashlib.sha256(data).hexdigest()


def _split_ext_id(ext_id: str) -> tuple[str, str]:
    """Split ``publisher.name`` on the FIRST ``.`` into ``(publisher, name)``.

    VS Code ids are ``publisher.name`` where each part is ``[a-z0-9][a-z0-9-]*``
    (no dots inside a part), so the first dot is the boundary. A missing dot or
    an empty publisher/name is a malformed id -> clean :class:`ResolveError`.
    """
    publisher, sep, name = ext_id.partition(".")
    if not sep or not publisher or not name:
        raise ResolveError(
            f"malformed extension id {ext_id!r}: expected 'publisher.name'"
        )
    return publisher, name


def _query_body(ext_id: str) -> bytes:
    """Build the ``extensionquery`` POST body for ``ext_id`` (UTF-8 JSON).

    A single ``filters`` page whose ``criteria`` carries the ``publisher.name``
    id under ``filterType 7``, plus the version-requesting ``flags``. This is the
    documented gallery-query shape; Task-6's e2e confirms it against the live
    API.
    """
    payload = {
        "filters": [
            {
                "criteria": [
                    {
                        "filterType": _FILTER_TYPE_EXTENSION_NAME,
                        "value": ext_id,
                    }
                ],
                "pageNumber": 1,
                "pageSize": 1,
            }
        ],
        "flags": _QUERY_FLAGS,
    }
    return json.dumps(payload).encode("utf-8")


def _pick_latest_version(body: bytes, ext_id: str) -> str:
    """Return the newest concrete version from an extensionquery response.

    Walks ``results[0].extensions[0].versions[0].version`` (the list is
    newest-first). A missing extension, an empty version list, or a non-string
    version fails closed with :class:`ResolveError` — a resolver must never guess
    or store ``latest``.
    """
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResolveError(
            f"marketplace extensionquery returned non-JSON for {ext_id!r}: {exc}"
        ) from exc
    results = payload.get("results") if isinstance(payload, dict) else None
    result = results[0] if isinstance(results, list) and results else None
    extensions = result.get("extensions") if isinstance(result, dict) else None
    if not isinstance(extensions, list) or not extensions:
        raise ResolveError(f"extension {ext_id!r} not found in the marketplace")
    first_ext = extensions[0]
    versions = first_ext.get("versions") if isinstance(first_ext, dict) else None
    if not isinstance(versions, list) or not versions:
        raise ResolveError(f"extension {ext_id!r} has no versions in the marketplace")
    latest = versions[0].get("version") if isinstance(versions[0], dict) else None
    if not isinstance(latest, str) or not latest:
        raise ResolveError(
            f"extension {ext_id!r} version entry is missing a 'version' field"
        )
    return latest
