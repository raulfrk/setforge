"""Shared HTTPS fetch helper for resolvers.

Guard order is load-bearing: reject non-HTTPS up front, re-check the FINAL
URL after redirects (a silent https->http downgrade is otherwise followed),
then cap via ``read(max_bytes + 1)`` before decode — one byte over proves the
body is too large without buffering the whole stream. When ``decode_gzip`` is
set the DECODED output is bounded too: it is inflated incrementally against
``max_decompressed`` so a gzip bomb (tiny on the wire, huge inflated) cannot
allocate past the cap.
"""

from __future__ import annotations

import urllib.error
import urllib.request
import zlib
from collections.abc import Mapping

from setforge.errors import ResolveError

__all__ = ["fetch_bytes"]

_MAX_DECOMPRESSED_BYTES = 1024 * 1024 * 1024

_DECODE_CHUNK = 1024 * 1024


def fetch_bytes(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    user_agent: str | None = None,
    data: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    decode_gzip: bool = False,
    max_decompressed: int = _MAX_DECOMPRESSED_BYTES,
) -> bytes:
    """Fetch ``url`` over HTTPS with an explicit timeout + hard wire cap.

    ``data`` makes it a POST; see module docstring for the gzip-decode cap.
    """
    if not url.startswith("https://"):
        raise ResolveError(f"refusing non-HTTPS URL: {url!r}")
    merged: dict[str, str] = dict(headers) if headers is not None else {}
    if user_agent is not None:
        merged["User-Agent"] = user_agent
    request = urllib.request.Request(url, data=data, headers=merged)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final_url = response.geturl()
            if not final_url.startswith("https://"):
                raise ResolveError(f"refusing non-HTTPS redirect target: {final_url!r}")
            body = response.read(max_bytes + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ResolveError(f"network error fetching {url}: {exc}") from exc
    if len(body) > max_bytes:
        raise ResolveError(f"response for {url} exceeds the {max_bytes}-byte wire cap")
    if decode_gzip:
        body = _gunzip_bounded(body, url=url, max_decompressed=max_decompressed)
    return body


def _gunzip_bounded(body: bytes, *, url: str, max_decompressed: int) -> bytes:
    """Inflate incrementally so peak memory stays near the cap, not the full size."""
    decompressor = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
    out = bytearray()
    try:
        for start in range(0, len(body), _DECODE_CHUNK):
            pending: bytes | None = body[start : start + _DECODE_CHUNK]
            while pending is not None:
                chunk = decompressor.decompress(pending, _DECODE_CHUNK)
                out += chunk
                if len(out) > max_decompressed:
                    raise ResolveError(
                        f"gzip-decoded response for {url} exceeds the "
                        f"{max_decompressed}-byte decompressed cap "
                        "(possible decompression bomb)"
                    )
                if decompressor.unconsumed_tail:
                    pending = decompressor.unconsumed_tail
                elif decompressor.eof and decompressor.unused_data:
                    pending = decompressor.unused_data
                    decompressor = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
                else:
                    pending = None
        out += decompressor.flush()
    except (zlib.error, OSError, EOFError) as exc:
        raise ResolveError(f"failed to gzip-decode response for {url}: {exc}") from exc
    if len(out) > max_decompressed:
        raise ResolveError(
            f"gzip-decoded response for {url} exceeds the "
            f"{max_decompressed}-byte decompressed cap (possible decompression bomb)"
        )
    return bytes(out)
