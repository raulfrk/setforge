"""Tests for the shared resolver fetch helper (spec §B2 / §C anti-pitfalls).

:func:`setforge.provision.resolve._fetch.fetch_bytes` centralizes the urllib
discipline every resolver needs: HTTPS-only on the request URL, an explicit
timeout, a redirect-downgrade re-check on the FINAL url, and a HARD wire-size
cap. These tests mock ``urllib.request.urlopen`` with a fake response so they
never touch the network — the real fetch is Task-6's docker e2e.
"""

from __future__ import annotations

from types import TracebackType

import pytest

from setforge.errors import ResolveError
from setforge.provision.resolve import _fetch


class _FakeResponse:
    """A minimal urlopen() context-manager stand-in.

    ``final_url`` models the URL AFTER redirects (what ``geturl()`` returns);
    ``body`` is the wire payload ``read(n)`` serves (respecting the ``n`` cap
    the helper passes, so the over-cap case is exercised realistically).
    ``read_calls`` records the ``n`` argument so a test can assert the helper
    reads ``max_bytes + 1``.
    """

    def __init__(
        self, body: bytes, final_url: str, read_calls: list[int] | None = None
    ) -> None:
        self._body = body
        self._final_url = final_url
        self._read_calls = read_calls

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def geturl(self) -> str:
        return self._final_url

    def read(self, n: int) -> bytes:
        if self._read_calls is not None:
            self._read_calls.append(n)
        return self._body[:n]


def _patch_urlopen(
    monkeypatch: pytest.MonkeyPatch,
    response: _FakeResponse,
    *,
    captured: dict[str, object] | None = None,
) -> None:
    def _fake_urlopen(request: object, timeout: float | None = None) -> _FakeResponse:
        if captured is not None:
            captured["timeout"] = timeout
            captured["request"] = request
        return response

    monkeypatch.setattr(_fetch.urllib.request, "urlopen", _fake_urlopen)


def test_rejects_http_request_url() -> None:
    with pytest.raises(ResolveError, match="non-HTTPS"):
        _fetch.fetch_bytes("http://example.com/x", timeout=5, max_bytes=1024)


def test_rejects_redirect_downgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    # Request URL is https, but the FINAL url (post-redirect) is http — the
    # only downgrade the opener would silently follow.
    _patch_urlopen(
        monkeypatch, _FakeResponse(b"payload", final_url="http://evil.example/x")
    )
    with pytest.raises(ResolveError, match="redirect target"):
        _fetch.fetch_bytes("https://example.com/x", timeout=5, max_bytes=1024)


def test_enforces_wire_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    # 5-byte cap, 6-byte body -> over the limit -> ResolveError.
    _patch_urlopen(
        monkeypatch, _FakeResponse(b"abcdef", final_url="https://example.com/x")
    )
    with pytest.raises(ResolveError, match="wire cap"):
        _fetch.fetch_bytes("https://example.com/x", timeout=5, max_bytes=5)


def test_reads_max_bytes_plus_one(monkeypatch: pytest.MonkeyPatch) -> None:
    read_calls: list[int] = []
    _patch_urlopen(
        monkeypatch,
        _FakeResponse(b"ok", final_url="https://example.com/x", read_calls=read_calls),
    )
    _fetch.fetch_bytes("https://example.com/x", timeout=5, max_bytes=100)
    # read(max_bytes + 1) proves the over-cap without buffering the full stream.
    assert read_calls == [101]


def test_passes_timeout_through(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    _patch_urlopen(
        monkeypatch,
        _FakeResponse(b"ok", final_url="https://example.com/x"),
        captured=captured,
    )
    _fetch.fetch_bytes("https://example.com/x", timeout=17.5, max_bytes=1024)
    assert captured["timeout"] == 17.5


def test_returns_body_under_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_urlopen(
        monkeypatch, _FakeResponse(b"hello", final_url="https://example.com/x")
    )
    assert (
        _fetch.fetch_bytes("https://example.com/x", timeout=5, max_bytes=1024)
        == b"hello"
    )


def test_user_agent_added_when_supplied(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    _patch_urlopen(
        monkeypatch,
        _FakeResponse(b"ok", final_url="https://example.com/x"),
        captured=captured,
    )
    _fetch.fetch_bytes(
        "https://api.example.com/x", timeout=5, max_bytes=1024, user_agent="setforge/1"
    )
    request = captured["request"]
    # urllib normalizes header keys to Title-case on Request.add_header.
    assert request.get_header("User-agent") == "setforge/1"  # type: ignore[attr-defined]


def test_network_error_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(request: object, timeout: float | None = None) -> _FakeResponse:
        raise TimeoutError("slow")

    monkeypatch.setattr(_fetch.urllib.request, "urlopen", _boom)
    with pytest.raises(ResolveError, match="network error"):
        _fetch.fetch_bytes("https://example.com/x", timeout=5, max_bytes=1024)
