"""Shared HTTPS fetch helper for resolvers.

Guard order is load-bearing: reject non-HTTPS up front, re-check the FINAL
URL after redirects (a silent https->http downgrade is otherwise followed),
then cap via ``read(max_bytes + 1)`` before decode — one byte over proves the
body is too large without buffering the whole stream.
"""

from __future__ import annotations

import gzip
import urllib.error
import urllib.request
from collections.abc import Mapping

from setforge.errors import ResolveError

__all__ = ["fetch_bytes"]


def fetch_bytes(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    user_agent: str | None = None,
    data: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    decode_gzip: bool = False,
) -> bytes:
    """Fetch ``url`` over HTTPS with an explicit timeout + hard wire cap.

    ``data`` makes it a POST; ``decode_gzip`` gunzips AFTER the wire cap is
    enforced on the pre-decode bytes, so a hostile response cannot inflate
    past the cap.
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
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError) as exc:
            raise ResolveError(
                f"failed to gzip-decode response for {url}: {exc}"
            ) from exc
    return body
