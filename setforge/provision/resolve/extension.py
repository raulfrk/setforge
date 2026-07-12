"""The extension (VS Code) :class:`Resolver`: TOFU VSIX sha256, hashed only on
bytes that passed the guarded fetch, never on the ``latest`` alias."""

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

__all__ = ["ExtensionResolveItem", "ExtensionResolver", "download_vsix", "vsix_url"]

_GALLERY_BASE = "https://marketplace.visualstudio.com/_apis/public/gallery"
_QUERY_URL = f"{_GALLERY_BASE}/extensionquery"
_LATEST = "latest"
_FETCH_TIMEOUT_S = 30.0
_MAX_QUERY_BYTES = 16 * 1024 * 1024
_MAX_VSIX_BYTES = 512 * 1024 * 1024
_USER_AGENT = "setforge-lock-resolver"
_ACCEPT = "application/json;api-version=7.2-preview.1"
_CONTENT_TYPE = "application/json"
_QUERY_FLAGS = 0x1 | 0x2 | 0x10
_FILTER_TYPE_EXTENSION_NAME = 7

Fetch = Callable[..., bytes]


class ExtensionResolveItem(BaseModel):
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
        data = download_vsix(publisher, name, version, fetch=self._fetch)
        return hashlib.sha256(data).hexdigest()


def vsix_url(publisher: str, name: str, version: str) -> str:
    """Build the ``vspackage`` download URL for a CONCRETE extension version."""
    return (
        f"{_GALLERY_BASE}/publishers/{publisher}/vsextensions/"
        f"{name}/{version}/vspackage"
    )


def download_vsix(
    publisher: str,
    name: str,
    version: str,
    *,
    fetch: Fetch | None = None,
) -> bytes:
    """Download the gzip-decoded VSIX bytes for a CONCRETE extension version.

    Shared by the resolver (hashes for TOFU) and the strong install path
    (verifies against the locked hash) — the same decoded bytes ``code``
    consumes, so the install hash matches the locked hash.
    """
    do_fetch = fetch if fetch is not None else _default_fetch
    return do_fetch(
        vsix_url(publisher, name, version),
        user_agent=_USER_AGENT,
        decode_gzip=True,
    )


def _split_ext_id(ext_id: str) -> tuple[str, str]:
    publisher, sep, name = ext_id.partition(".")
    if not sep or not publisher or not name:
        raise ResolveError(
            f"malformed extension id {ext_id!r}: expected 'publisher.name'"
        )
    return publisher, name


def _query_body(ext_id: str) -> bytes:
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
