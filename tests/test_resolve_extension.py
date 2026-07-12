"""Tests for the extension resolver (fetch injected; never hits
marketplace.visualstudio.com)."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import zipfile
from collections.abc import Mapping

import pytest

from setforge.errors import ResolveError
from setforge.provision.resolve.extension import (
    _QUERY_URL,
    ExtensionResolveItem,
    ExtensionResolver,
    download_vsix,
    vsix_url,
)
from setforge.provision.resolve.protocol import IntegrityKind, PackageType


def _vsix_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("extension.vsixmanifest", "<manifest/>")
        zf.writestr("extension/package.json", '{"name": "prettier-vscode"}')
    return buf.getvalue()


_VSIX = _vsix_bytes()
_VSIX_SHA = hashlib.sha256(_VSIX).hexdigest()

_QUERY_JSON = json.dumps(
    {
        "results": [
            {
                "extensions": [
                    {
                        "publisher": {"publisherName": "esbenp"},
                        "extensionName": "prettier-vscode",
                        "versions": [
                            {
                                "version": "10.4.0",
                                "assetUri": "https://…/10.4.0",
                                "files": [{"assetType": "Microsoft.VSCode.Manifest"}],
                                "properties": [{"key": "Microsoft.Foo", "value": "x"}],
                            },
                            {"version": "10.1.0", "assetUri": "https://…/10.1.0"},
                            {"version": "9.10.4", "assetUri": "https://…/9.10.4"},
                        ],
                    }
                ]
            }
        ]
    }
).encode("utf-8")


def _vspackage_url(publisher: str, name: str, version: str) -> str:
    return (
        "https://marketplace.visualstudio.com/_apis/public/gallery/publishers/"
        f"{publisher}/vsextensions/{name}/{version}/vspackage"
    )


def _fetcher(
    responses: dict[str, bytes],
    *,
    record: list[tuple[str, bytes | None]] | None = None,
    gzip_urls: frozenset[str] = frozenset(),
):
    def _fetch(
        url: str,
        *,
        user_agent: str | None = None,
        data: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        decode_gzip: bool = False,
    ) -> bytes:
        if record is not None:
            record.append((url, data))
        try:
            payload = responses[url]
        except KeyError:
            raise ResolveError(f"unexpected URL fetched: {url!r}") from None
        if url in gzip_urls:
            if not decode_gzip:
                return gzip.compress(payload)
            return payload
        return payload

    return _fetch


def _item(key: str = "esbenp.prettier-vscode", version: str | None = None):
    return ExtensionResolveItem(key=key, version=version)


def test_download_vsix_builds_concrete_url_and_decodes_gzip() -> None:
    url = _vspackage_url("esbenp", "prettier-vscode", "10.4.0")
    record: list[tuple[str, bytes | None]] = []
    fetch = _fetcher({url: _VSIX}, record=record, gzip_urls=frozenset({url}))
    data = download_vsix("esbenp", "prettier-vscode", "10.4.0", fetch=fetch)
    # decoded bytes returned (helper requested decode_gzip) over the concrete URL
    assert data == _VSIX
    assert [u for u, _ in record] == [url]
    assert vsix_url("esbenp", "prettier-vscode", "10.4.0") == url
    assert "latest" not in url


def test_resolve_picks_latest_version_and_hashes_vsix() -> None:
    url = _vspackage_url("esbenp", "prettier-vscode", "10.4.0")
    resolver = ExtensionResolver(fetch=_fetcher({_QUERY_URL: _QUERY_JSON, url: _VSIX}))
    pin = resolver.resolve(_item())
    assert pin.type is PackageType.EXTENSION
    assert pin.key == "esbenp.prettier-vscode"
    assert pin.version == "10.4.0"
    assert pin.integrity == f"sha256:{_VSIX_SHA}"
    assert pin.integrity_kind is IntegrityKind.CHECKSUM


def test_pinned_version_skips_selection_and_uses_that_url() -> None:
    url = _vspackage_url("esbenp", "prettier-vscode", "9.10.4")
    record: list[tuple[str, bytes | None]] = []
    resolver = ExtensionResolver(
        fetch=_fetcher({_QUERY_URL: _QUERY_JSON, url: _VSIX}, record=record)
    )
    pin = resolver.resolve(_item(version="9.10.4"))
    assert pin.version == "9.10.4"
    assert any(u == url for u, _ in record)


def test_vspackage_gzip_response_is_decoded_before_hashing() -> None:
    url = _vspackage_url("esbenp", "prettier-vscode", "10.4.0")
    resolver = ExtensionResolver(
        fetch=_fetcher(
            {_QUERY_URL: _QUERY_JSON, url: _VSIX}, gzip_urls=frozenset({url})
        )
    )
    pin = resolver.resolve(_item())
    assert pin.integrity == f"sha256:{_VSIX_SHA}"


def test_built_url_never_contains_latest() -> None:
    url = _vspackage_url("esbenp", "prettier-vscode", "10.4.0")
    record: list[tuple[str, bytes | None]] = []
    resolver = ExtensionResolver(
        fetch=_fetcher({_QUERY_URL: _QUERY_JSON, url: _VSIX}, record=record)
    )
    pin = resolver.resolve(_item())
    assert pin.version != "latest"
    assert all("latest" not in u for u, _ in record)


def test_query_post_carries_body_and_publisher_name() -> None:
    url = _vspackage_url("esbenp", "prettier-vscode", "10.4.0")
    record: list[tuple[str, bytes | None]] = []
    resolver = ExtensionResolver(
        fetch=_fetcher({_QUERY_URL: _QUERY_JSON, url: _VSIX}, record=record)
    )
    resolver.resolve(_item())
    query = next((data for u, data in record if u == _QUERY_URL), None)
    assert query is not None  # the query call was a POST with a body
    body = json.loads(query.decode("utf-8"))
    criteria = body["filters"][0]["criteria"]
    ids = [c["value"] for c in criteria if c["filterType"] == 7]
    assert "esbenp.prettier-vscode" in ids


def test_publisher_name_split_on_first_dot() -> None:
    url = (
        "https://marketplace.visualstudio.com/_apis/public/gallery/publishers/"
        "ms-python/vsextensions/python/10.4.0/vspackage"
    )
    query = json.dumps(
        {"results": [{"extensions": [{"versions": [{"version": "10.4.0"}]}]}]}
    ).encode("utf-8")
    resolver = ExtensionResolver(fetch=_fetcher({_QUERY_URL: query, url: _VSIX}))
    pin = resolver.resolve(_item(key="ms-python.python"))
    assert pin.key == "ms-python.python"
    assert pin.version == "10.4.0"


def test_malformed_id_no_dot_raises_clean_error() -> None:
    resolver = ExtensionResolver(fetch=_fetcher({}))
    with pytest.raises(ResolveError, match=r"publisher\.name"):
        resolver.resolve(_item(key="nodothere"))


def test_empty_publisher_or_name_raises() -> None:
    resolver = ExtensionResolver(fetch=_fetcher({}))
    with pytest.raises(ResolveError):
        resolver.resolve(_item(key=".prettier-vscode"))
    with pytest.raises(ResolveError):
        resolver.resolve(_item(key="esbenp."))


def test_empty_version_list_raises() -> None:
    query = json.dumps({"results": [{"extensions": [{"versions": []}]}]}).encode()
    resolver = ExtensionResolver(fetch=_fetcher({_QUERY_URL: query}))
    with pytest.raises(ResolveError, match="no versions"):
        resolver.resolve(_item())


def test_version_entry_missing_version_field_raises() -> None:
    query = json.dumps(
        {"results": [{"extensions": [{"versions": [{"assetUri": "https://…/x"}]}]}]}
    ).encode()
    resolver = ExtensionResolver(fetch=_fetcher({_QUERY_URL: query}))
    with pytest.raises(ResolveError, match="'version' field"):
        resolver.resolve(_item())


def test_extension_not_found_raises() -> None:
    query = json.dumps({"results": [{"extensions": []}]}).encode()
    resolver = ExtensionResolver(fetch=_fetcher({_QUERY_URL: query}))
    with pytest.raises(ResolveError, match="not found"):
        resolver.resolve(_item())


def test_query_non_json_raises() -> None:
    resolver = ExtensionResolver(fetch=_fetcher({_QUERY_URL: b"<html>nope"}))
    with pytest.raises(ResolveError):
        resolver.resolve(_item())


def test_tofu_hash_not_computed_when_fetch_guard_rejects() -> None:
    # A guard failure on the vspackage URL raises before any hashing (TOFU).
    def _fetch(
        url: str,
        *,
        user_agent: str | None = None,
        data: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        decode_gzip: bool = False,
    ) -> bytes:
        if url == _QUERY_URL:
            return _QUERY_JSON
        raise ResolveError("refusing non-HTTPS redirect target")

    resolver = ExtensionResolver(fetch=_fetch)
    with pytest.raises(ResolveError, match="non-HTTPS"):
        resolver.resolve(_item())


def test_resolve_registered_and_retrievable() -> None:
    from setforge.provision.resolve import extension  # noqa: F401  (registers)
    from setforge.provision.resolve.registry import get_resolver

    assert isinstance(get_resolver(PackageType.EXTENSION), ExtensionResolver)
