"""Tests for the github_release resolver (fetch injected; never hits github.com)."""

from __future__ import annotations

import hashlib
import json

import pytest

from setforge.config import GitHubReleasePackage
from setforge.errors import ResolveError
from setforge.provision.resolve.github_release import GitHubReleaseResolver
from setforge.provision.resolve.protocol import IntegrityKind, PackageType

_ASSET_BYTES = b"\x7fELF fake-binary-tarball payload"
_ASSET_SHA = hashlib.sha256(_ASSET_BYTES).hexdigest()

_LATEST_API_JSON = json.dumps(
    {
        "tag_name": "v0.8.9",
        "name": "Release 0.8.9",
        "draft": False,
        "prerelease": False,
    }
).encode("utf-8")


def _pkg(tag: str = "v0.8.9") -> GitHubReleasePackage:
    return GitHubReleasePackage(
        repo="owner/tool",
        tag=tag,
        asset="tool-linux.tar.gz",
        binary="tool",
        install="~/.local/bin",
    )


def _fetcher(
    responses: dict[str, bytes], *, record: list[tuple[str, str | None]] | None = None
):
    def _fetch(url: str, *, user_agent: str | None = None) -> bytes:
        if record is not None:
            record.append((url, user_agent))
        try:
            return responses[url]
        except KeyError:
            raise ResolveError(f"unexpected URL fetched: {url!r}") from None

    return _fetch


def test_resolve_concrete_tag_hashes_asset() -> None:
    asset_url = (
        "https://github.com/owner/tool/releases/download/v0.8.9/tool-linux.tar.gz"
    )
    record: list[tuple[str, str | None]] = []
    resolver = GitHubReleaseResolver(
        fetch=_fetcher({asset_url: _ASSET_BYTES}, record=record)
    )
    pin = resolver.resolve(_pkg("v0.8.9"))
    assert pin.type is PackageType.GITHUB_RELEASE
    assert pin.key == "owner/tool"
    assert pin.version == "v0.8.9"
    assert pin.integrity == f"sha256:{_ASSET_SHA}"
    assert pin.integrity_kind is IntegrityKind.CHECKSUM
    assert [u for u, _ in record] == [asset_url]


def test_resolve_latest_queries_api_then_hashes_asset() -> None:
    api_url = "https://api.github.com/repos/owner/tool/releases/latest"
    asset_url = (
        "https://github.com/owner/tool/releases/download/v0.8.9/tool-linux.tar.gz"
    )
    record: list[tuple[str, str | None]] = []
    resolver = GitHubReleaseResolver(
        fetch=_fetcher(
            {api_url: _LATEST_API_JSON, asset_url: _ASSET_BYTES}, record=record
        )
    )
    pin = resolver.resolve(_pkg("latest"))
    assert pin.version == "v0.8.9"
    assert pin.integrity == f"sha256:{_ASSET_SHA}"
    api_calls = [(u, ua) for u, ua in record if u == api_url]
    assert api_calls
    assert api_calls[0][1] is not None


def test_resolve_never_returns_latest_token() -> None:
    api_url = "https://api.github.com/repos/owner/tool/releases/latest"
    asset_url = (
        "https://github.com/owner/tool/releases/download/v0.8.9/tool-linux.tar.gz"
    )
    resolver = GitHubReleaseResolver(
        fetch=_fetcher({api_url: _LATEST_API_JSON, asset_url: _ASSET_BYTES})
    )
    pin = resolver.resolve(_pkg("latest"))
    assert pin.version != "latest"


def test_resolve_missing_asset_raises_clean_error() -> None:
    api_url = "https://api.github.com/repos/owner/tool/releases/latest"
    resolver = GitHubReleaseResolver(fetch=_fetcher({api_url: _LATEST_API_JSON}))
    with pytest.raises(ResolveError):
        resolver.resolve(_pkg("latest"))


def test_resolve_api_missing_tag_name_raises() -> None:
    api_url = "https://api.github.com/repos/owner/tool/releases/latest"
    bad = json.dumps({"name": "no tag here"}).encode("utf-8")
    resolver = GitHubReleaseResolver(fetch=_fetcher({api_url: bad}))
    with pytest.raises(ResolveError, match="tag_name"):
        resolver.resolve(_pkg("latest"))


def test_resolve_api_non_json_raises() -> None:
    api_url = "https://api.github.com/repos/owner/tool/releases/latest"
    resolver = GitHubReleaseResolver(fetch=_fetcher({api_url: b"<html>nope"}))
    with pytest.raises(ResolveError):
        resolver.resolve(_pkg("latest"))


def test_resolve_registered_and_retrievable() -> None:
    from setforge.provision.resolve import github_release  # noqa: F401  (registers)
    from setforge.provision.resolve.registry import get_resolver

    assert isinstance(get_resolver(PackageType.GITHUB_RELEASE), GitHubReleaseResolver)
