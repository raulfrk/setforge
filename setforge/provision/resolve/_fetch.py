"""Shared HTTPS fetch helper for resolvers (spec §B2 / §C anti-pitfalls).

The cargo/python/github_release/plugin resolvers all need the SAME urllib
discipline: HTTPS-only on the request URL, an explicit timeout, a
redirect-downgrade re-check on the final URL, and a HARD wire-size cap so a
hostile or runaway response cannot exhaust memory. Rather than duplicate the
dance in every ``_default_fetch`` (the cargo/python copies drifted — python
was missing the wire cap), it lives here once. Callers decode/parse on top.

Mirrors :meth:`GitHubReleaseProvisioner._download`
(``github_release.py`` ~:121-150): reject non-HTTPS up front, re-check the
FINAL URL (a silent https->http redirect is the only downgrade the opener
would otherwise follow), and cap the read. The cap is enforced with a single
``read(max_bytes + 1)`` — one byte over the limit is enough to prove the body
exceeds it without buffering the whole (possibly huge) stream.
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

    Rejects a non-HTTPS request URL up front and re-checks the FINAL URL after
    redirects (a silent https->http downgrade is otherwise followed by the
    opener). Reads at most ``max_bytes`` — a body strictly larger than the cap
    raises :class:`ResolveError` rather than being buffered. An optional
    ``user_agent`` header is added when supplied (the GitHub API rejects
    request without one). Every network/OS failure surfaces as a clean
    :class:`ResolveError`, never a raw traceback.

    When ``data`` is supplied the request is a POST (urllib infers the method
    from a non-``None`` body) carrying that body — the VS Marketplace
    ``extensionquery`` endpoint (extension resolver) needs a POST + JSON body,
    and duplicating the guarded urllib dance there would be a footgun, so the
    same HTTPS-only + redirect-downgrade + timeout + wire-cap discipline covers
    it here. ``headers`` adds request headers (e.g. ``Accept`` /
    ``Content-Type``) on top of the ``user_agent`` convenience.

    ``decode_gzip`` gunzips the response body before returning it. The VSIX
    ``vspackage`` download arrives gzip-transfer-encoded; the TOFU hash must
    cover the DECODED VSIX bytes (what ``code --install-extension`` consumes),
    not the compressed wire bytes. The wire cap is enforced on the ON-THE-WIRE
    bytes (pre-decode), so a hostile response cannot inflate past the cap.
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
            # read(max_bytes + 1): one byte over the cap proves the body is too
            # large without materializing the whole stream.
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
