"""The github_release :class:`Resolver` — resolves tag + asset sha256.

The github_release PROVISIONER (:mod:`setforge.provision.github_release`)
already downloads+verifies the asset at install; this resolver only produces
the pin the lock records:

* the concrete TAG — the spec's ``tag:`` verbatim when concrete, or (when
  ``tag == "latest"``) the ``tag_name`` from the GitHub releases API
  ``/repos/{repo}/releases/latest`` — never the moving ``latest`` token.
* the asset SHA256 — computed by fetching the release asset ONCE and hashing
  it (TOFU: setforge records its OWN computed hash, exactly as the provisioner
  already does at install).

The fetch boundary is injected (the ``fetch`` callable) so unit tests never
touch github.com; the default delegates the HTTPS-only + timeout + wire-cap
discipline to :func:`~setforge.provision.resolve._fetch.fetch_bytes`. The
GitHub API rejects requests without a ``User-Agent``, so one is always sent.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import ClassVar

from setforge.config import GitHubReleasePackage
from setforge.errors import ResolveError
from setforge.provision.resolve._fetch import fetch_bytes
from setforge.provision.resolve.protocol import (
    IntegrityKind,
    PackageType,
    ResolvedPin,
)
from setforge.provision.resolve.registry import register

__all__ = ["GitHubReleaseResolver"]

_API_BASE = "https://api.github.com"
_DOWNLOAD_BASE = "https://github.com"
_LATEST = "latest"
_FETCH_TIMEOUT_S = 30.0
# One cap covers both fetches: the release-metadata JSON is tiny, so the asset
# ceiling (mirroring the install-side provisioner's 512 MiB) bounds it too.
_MAX_WIRE_BYTES = 512 * 1024 * 1024
# GitHub's REST API returns 403 to requests without a User-Agent.
_USER_AGENT = "setforge-lock-resolver"

# A fetch takes the URL + an optional UA and returns the raw bytes.
Fetch = Callable[..., bytes]


def _default_fetch(url: str, *, user_agent: str | None = None) -> bytes:
    """Fetch ``url`` over HTTPS via the shared capped fetch helper.

    A thin wrapper matching the injected test double's ``(url, *, user_agent)``
    signature; delegates HTTPS-only + timeout + wire-cap to
    :func:`~setforge.provision.resolve._fetch.fetch_bytes`.
    """
    return fetch_bytes(
        url, timeout=_FETCH_TIMEOUT_S, max_bytes=_MAX_WIRE_BYTES, user_agent=user_agent
    )


@register(PackageType.GITHUB_RELEASE)
class GitHubReleaseResolver:
    """Resolve a github_release package to its concrete tag + asset sha256."""

    type: ClassVar[PackageType] = PackageType.GITHUB_RELEASE

    def __init__(self, *, fetch: Fetch | None = None) -> None:
        self._fetch = fetch if fetch is not None else _default_fetch

    def resolve(self, item: object) -> ResolvedPin:
        if not isinstance(item, GitHubReleasePackage):  # pragma: no cover - guard
            raise ResolveError(
                f"github_release resolver received {type(item).__name__}, not "
                "GitHubReleasePackage"
            )
        tag = self._resolve_tag(item.repo, item.tag)
        digest = self._hash_asset(item.repo, tag, item.asset)
        return ResolvedPin(
            type=PackageType.GITHUB_RELEASE,
            key=item.repo,
            version=tag,
            integrity=f"sha256:{digest}",
            integrity_kind=IntegrityKind.CHECKSUM,
        )

    def _resolve_tag(self, repo: str, tag: str) -> str:
        """Return the concrete tag, querying the releases API for ``latest``.

        A concrete ``tag`` is used verbatim (no network). ``latest`` is
        resolved via ``/repos/{repo}/releases/latest`` to its ``tag_name`` —
        the moving token is NEVER stored in the pin.
        """
        if tag != _LATEST:
            return tag
        url = f"{_API_BASE}/repos/{repo}/releases/latest"
        body = self._fetch(url, user_agent=_USER_AGENT)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ResolveError(
                f"GitHub releases API returned non-JSON for {repo!r}: {exc}"
            ) from exc
        tag_name = payload.get("tag_name") if isinstance(payload, dict) else None
        if not isinstance(tag_name, str) or not tag_name:
            raise ResolveError(
                f"GitHub releases API body for {repo!r} is missing 'tag_name'"
            )
        return tag_name

    def _hash_asset(self, repo: str, tag: str, asset: str) -> str:
        """Fetch the release asset ONCE and return its sha256 hex digest."""
        url = f"{_DOWNLOAD_BASE}/{repo}/releases/download/{tag}/{asset}"
        data = self._fetch(url, user_agent=_USER_AGENT)
        return hashlib.sha256(data).hexdigest()
