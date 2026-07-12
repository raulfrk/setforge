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

import urllib.error
import urllib.request

from setforge.errors import ResolveError

__all__ = ["fetch_bytes"]


def fetch_bytes(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    user_agent: str | None = None,
) -> bytes:
    """Fetch ``url`` over HTTPS with an explicit timeout + hard wire cap.

    Rejects a non-HTTPS request URL up front and re-checks the FINAL URL after
    redirects (a silent https->http downgrade is otherwise followed by the
    opener). Reads at most ``max_bytes`` — a body strictly larger than the cap
    raises :class:`ResolveError` rather than being buffered. An optional
    ``user_agent`` header is added when supplied (the GitHub API rejects
    request without one). Every network/OS failure surfaces as a clean
    :class:`ResolveError`, never a raw traceback.
    """
    if not url.startswith("https://"):
        raise ResolveError(f"refusing non-HTTPS URL: {url!r}")
    headers = {"User-Agent": user_agent} if user_agent is not None else {}
    request = urllib.request.Request(url, method="GET", headers=headers)
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
    return body
