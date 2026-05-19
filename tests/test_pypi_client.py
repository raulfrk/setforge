"""Unit tests for :mod:`setforge._pypi_client` (offline, urllib mocked)."""

from __future__ import annotations

import json
import urllib.error
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from setforge import _pypi_client
from setforge._pypi_client import (
    PyPIVersionInfo,
    fetch_latest_version,
)
from setforge.errors import PyPIFetchError


class _FakeResponse:
    """Stand-in for the ``http.client.HTTPResponse`` returned by urlopen."""

    def __init__(
        self,
        *,
        status: int = 200,
        body: bytes = b"{}",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _pypi_body(
    *,
    info_version: str = "0.3.0",
    releases: dict[str, list[dict]] | None = None,
    yanked_reason: str | None = None,
) -> dict[str, Any]:
    releases = releases or {
        "0.1.0": [{"yanked": False}],
        "0.2.0": [{"yanked": False}],
        "0.3.0": [{"yanked": False}],
    }
    info: dict[str, Any] = {"version": info_version}
    if yanked_reason is not None:
        info["yanked_reason"] = yanked_reason
    return {"info": info, "releases": releases}


def _patch_urlopen(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response_factory: Callable[..., _FakeResponse],
) -> list[tuple[str, dict[str, str]]]:
    """Replace ``urllib.request.urlopen`` with a factory; record calls."""
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_urlopen(request: Any, timeout: float | None = None) -> _FakeResponse:
        headers = {k.title(): v for k, v in request.header_items()}
        calls.append((request.full_url, headers))
        return response_factory(request)

    monkeypatch.setattr(_pypi_client.urllib.request, "urlopen", fake_urlopen)
    return calls


def test_fetch_latest_version_returns_highest_stable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The highest non-prerelease, non-yanked version wins."""
    body = _pypi_body()

    def factory(_request: Any) -> _FakeResponse:
        return _FakeResponse(
            status=200,
            body=json.dumps(body).encode("utf-8"),
            headers={"ETag": "abc123"},
        )

    _patch_urlopen(monkeypatch, response_factory=factory)
    info = fetch_latest_version(
        package="setforge",
        current_version="0.1.0",
        cache_dir=tmp_path,
    )
    assert isinstance(info, PyPIVersionInfo)
    assert info.version == "0.3.0"
    assert info.is_prerelease is False
    assert info.yanked is False
    assert info.yanked_reason is None


def test_fetch_latest_version_filters_prereleases_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``0.4.0rc1`` is hidden when ``include_prereleases=False``."""
    body = _pypi_body(
        releases={
            "0.3.0": [{"yanked": False}],
            "0.4.0rc1": [{"yanked": False}],
        }
    )

    def factory(_request: Any) -> _FakeResponse:
        return _FakeResponse(
            status=200, body=json.dumps(body).encode("utf-8"), headers={"ETag": "x"}
        )

    _patch_urlopen(monkeypatch, response_factory=factory)
    info = fetch_latest_version(
        package="setforge", current_version="0.1.0", cache_dir=tmp_path
    )
    assert info.version == "0.3.0"


def test_fetch_latest_version_includes_prereleases_when_asked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    body = _pypi_body(
        releases={
            "0.3.0": [{"yanked": False}],
            "0.4.0rc1": [{"yanked": False}],
        }
    )

    def factory(_request: Any) -> _FakeResponse:
        return _FakeResponse(
            status=200, body=json.dumps(body).encode("utf-8"), headers={"ETag": "x"}
        )

    _patch_urlopen(monkeypatch, response_factory=factory)
    info = fetch_latest_version(
        package="setforge",
        current_version="0.1.0",
        cache_dir=tmp_path,
        include_prereleases=True,
    )
    assert info.version == "0.4.0rc1"
    assert info.is_prerelease is True


def test_fetch_latest_version_skips_yanked_releases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    body = _pypi_body(
        releases={
            "0.2.0": [{"yanked": False}],
            "0.3.0": [{"yanked": True, "yanked_reason": "broken on 3.13"}],
        }
    )

    def factory(_request: Any) -> _FakeResponse:
        return _FakeResponse(
            status=200, body=json.dumps(body).encode("utf-8"), headers={"ETag": "x"}
        )

    _patch_urlopen(monkeypatch, response_factory=factory)
    info = fetch_latest_version(
        package="setforge", current_version="0.1.0", cache_dir=tmp_path
    )
    assert info.version == "0.2.0"


def test_fetch_latest_version_sends_user_agent_and_etag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """User-Agent identifies setforge; If-None-Match present when cache exists."""
    cache_path = tmp_path / "pypi-etag-setforge.json"
    cache_path.write_text(
        json.dumps({"etag": "cached-etag", "body": _pypi_body()}),
        encoding="utf-8",
    )

    def factory(_request: Any) -> _FakeResponse:
        return _FakeResponse(
            status=200,
            body=json.dumps(_pypi_body()).encode("utf-8"),
            headers={"ETag": "new-etag"},
        )

    calls = _patch_urlopen(monkeypatch, response_factory=factory)
    fetch_latest_version(
        package="setforge", current_version="0.1.5", cache_dir=tmp_path
    )
    assert len(calls) == 1
    url, headers = calls[0]
    assert url.endswith("/pypi/setforge/json")
    assert "setforge/0.1.5" in headers["User-Agent"]
    assert headers["If-None-Match"] == "cached-etag"


def test_fetch_latest_version_uses_304_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A 304 reply reuses the cached body verbatim."""
    cache_path = tmp_path / "pypi-etag-setforge.json"
    cache_path.write_text(
        json.dumps({"etag": "cached", "body": _pypi_body()}),
        encoding="utf-8",
    )

    def factory(request: Any) -> _FakeResponse:
        raise urllib.error.HTTPError(
            url=request.full_url,
            code=304,
            msg="Not Modified",
            hdrs=None,  # type: ignore[arg-type]
            fp=BytesIO(b""),
        )

    _patch_urlopen(monkeypatch, response_factory=factory)
    info = fetch_latest_version(
        package="setforge", current_version="0.1.0", cache_dir=tmp_path
    )
    assert info.version == "0.3.0"


def test_fetch_latest_version_writes_etag_cache_after_200(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    body = _pypi_body()

    def factory(_request: Any) -> _FakeResponse:
        return _FakeResponse(
            status=200,
            body=json.dumps(body).encode("utf-8"),
            headers={"ETag": "fresh-etag"},
        )

    _patch_urlopen(monkeypatch, response_factory=factory)
    fetch_latest_version(
        package="setforge", current_version="0.1.0", cache_dir=tmp_path
    )
    cached = json.loads(
        (tmp_path / "pypi-etag-setforge.json").read_text(encoding="utf-8")
    )
    assert cached["etag"] == "fresh-etag"
    assert cached["body"]["releases"]["0.3.0"]


def test_fetch_latest_version_raises_on_http_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def factory(request: Any) -> _FakeResponse:
        raise urllib.error.HTTPError(
            url=request.full_url,
            code=503,
            msg="Service Unavailable",
            hdrs=None,  # type: ignore[arg-type]
            fp=BytesIO(b""),
        )

    _patch_urlopen(monkeypatch, response_factory=factory)
    with pytest.raises(PyPIFetchError, match="HTTP 503"):
        fetch_latest_version(
            package="setforge", current_version="0.1.0", cache_dir=tmp_path
        )


def test_fetch_latest_version_raises_on_network_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def factory(_request: Any) -> _FakeResponse:
        raise urllib.error.URLError("no route to host")

    _patch_urlopen(monkeypatch, response_factory=factory)
    with pytest.raises(PyPIFetchError, match="network error"):
        fetch_latest_version(
            package="setforge", current_version="0.1.0", cache_dir=tmp_path
        )


def test_fetch_latest_version_raises_on_invalid_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def factory(_request: Any) -> _FakeResponse:
        return _FakeResponse(status=200, body=b"not-json", headers={"ETag": "x"})

    _patch_urlopen(monkeypatch, response_factory=factory)
    with pytest.raises(PyPIFetchError, match="non-JSON"):
        fetch_latest_version(
            package="setforge", current_version="0.1.0", cache_dir=tmp_path
        )


def test_fetch_latest_version_raises_when_releases_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def factory(_request: Any) -> _FakeResponse:
        return _FakeResponse(
            status=200, body=json.dumps({"info": {}}).encode(), headers={"ETag": "x"}
        )

    _patch_urlopen(monkeypatch, response_factory=factory)
    with pytest.raises(PyPIFetchError, match="missing 'releases'"):
        fetch_latest_version(
            package="setforge", current_version="0.1.0", cache_dir=tmp_path
        )


def test_fetch_latest_version_raises_when_all_versions_yanked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    body = _pypi_body(
        releases={
            "0.1.0": [{"yanked": True}],
            "0.2.0": [{"yanked": True}],
        }
    )

    def factory(_request: Any) -> _FakeResponse:
        return _FakeResponse(
            status=200, body=json.dumps(body).encode(), headers={"ETag": "x"}
        )

    _patch_urlopen(monkeypatch, response_factory=factory)
    with pytest.raises(PyPIFetchError, match="no non-yanked release"):
        fetch_latest_version(
            package="setforge", current_version="0.1.0", cache_dir=tmp_path
        )


def test_fetch_latest_version_propagates_yanked_reason_when_target_yanked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the selected (only) release is yanked, surface yanked_reason."""
    body = {
        "info": {"version": "0.3.0", "yanked_reason": "fatal bug"},
        "releases": {
            "0.3.0": [{"yanked": True, "yanked_reason": "fatal bug"}],
        },
    }

    def factory(_request: Any) -> _FakeResponse:
        return _FakeResponse(
            status=200, body=json.dumps(body).encode(), headers={"ETag": "x"}
        )

    _patch_urlopen(monkeypatch, response_factory=factory)
    # All releases are yanked → PyPIFetchError. Sanity: client refuses to pick.
    with pytest.raises(PyPIFetchError):
        fetch_latest_version(
            package="setforge", current_version="0.1.0", cache_dir=tmp_path
        )
