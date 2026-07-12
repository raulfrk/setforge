"""The python :class:`Resolver` — resolves the correct wheel's sha256 from PyPI.

Fetches ``https://pypi.org/pypi/{name}/json`` (latest) or
``/{name}/{version}/json`` (pinned) and reads the concrete ``info.version`` plus
the correct wheel's ``digests.sha256``. This is a RECORD+DRIFT pin: the actual
install stays ``uv tool install`` (PRAGMATIC, spec §B3); the lock records the
hash for drift-detection.

**Wheel-selection rule** (:func:`_select_wheel`). A compiled tool ships many
wheels (per platform/abi/interpreter); a tag mismatch would pin the WRONG hash.
The rule, in priority order:

1. A wheel whose tags match the TARGET — linux ``x86_64`` (manylinux/musllinux
   or a bare ``linux_x86_64``) built for the RUNNING CPython major (``cp3X``) or
   the abi3 stable ABI (``abi3``/``cp3``).
2. The pure-python universal wheel (``py3-none-any`` / ``none-any``) — a pure
   tool ships exactly this.

If neither matches (sdist-only, or only foreign-platform wheels) the resolve
fails closed rather than pinning an arbitrary hash.

The PyPI-fetch boundary is injected (the ``fetch_json`` callable) so unit tests
mock it; the default fetch mirrors ``_pypi_client``'s HTTPS-only, timed-out
urllib discipline (spec §C anti-pitfall: explicit timeout, reject downgrade).
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, ClassVar

from setforge.config import PythonPackage
from setforge.errors import ResolveError
from setforge.provision.resolve.protocol import (
    IntegrityKind,
    PackageType,
    ResolvedPin,
)
from setforge.provision.resolve.registry import register

__all__ = ["PythonResolver"]

_PYPI_BASE = "https://pypi.org/pypi"
# ``_pypi_client`` uses a (connect, read) tuple; urllib takes one scalar, so we
# reuse its max() convention as a single timeout here.
_FETCH_TIMEOUT_S = 10.0

# A fetch_json takes (name, version|None) and returns the parsed PyPI JSON body.
FetchJson = Callable[[str, "str | None"], "dict[str, Any]"]


def _cpython_tag() -> str:
    """The running interpreter's CPython tag, e.g. ``cp311``."""
    return f"cp{sys.version_info.major}{sys.version_info.minor}"


def _is_linux_x86_64_wheel(filename: str) -> bool:
    """True when ``filename``'s platform tag targets linux x86_64."""
    lower = filename.lower()
    return (
        "manylinux" in lower or "musllinux" in lower or "linux_x86_64" in lower
    ) and "x86_64" in lower


def _select_wheel(urls: list[dict[str, Any]]) -> str | None:
    """Return the chosen wheel's sha256, or ``None`` if no wheel matches.

    Priority: a linux-x86_64 wheel for the running CPython major (or an abi3
    stable-ABI wheel), then the pure ``py3-none-any`` wheel. ``None`` means the
    caller must fail closed (sdist-only or foreign-platform-only).
    """
    cp = _cpython_tag()
    compiled: str | None = None
    pure: str | None = None
    for entry in urls:
        if entry.get("packagetype") != "bdist_wheel":
            continue
        filename = entry.get("filename")
        digest = _sha256_of(entry)
        if not isinstance(filename, str) or digest is None:
            continue
        lower = filename.lower()
        if "py3-none-any" in lower or "none-any" in lower:
            pure = pure or digest
            continue
        # First matching compiled wheel wins; any linux/cpython (or abi3) match
        # for THIS release is correct, so the first is as good as any.
        if (
            compiled is None
            and _is_linux_x86_64_wheel(filename)
            and (cp in lower or "abi3" in lower)
        ):
            compiled = digest
    return compiled if compiled is not None else pure


def _sha256_of(entry: dict[str, Any]) -> str | None:
    """Extract ``digests.sha256`` from a PyPI url/file entry, or ``None``."""
    digests = entry.get("digests")
    if not isinstance(digests, dict):
        return None
    sha = digests.get("sha256")
    return sha if isinstance(sha, str) and sha else None


def _default_fetch_json(name: str, version: str | None) -> dict[str, Any]:
    """Fetch the PyPI JSON body for ``name`` (optionally pinned to ``version``).

    HTTPS-only with a redirect-downgrade re-check and an explicit timeout,
    mirroring ``_pypi_client``/``github_release`` discipline.
    """
    url = (
        f"{_PYPI_BASE}/{name}/json"
        if version is None
        else f"{_PYPI_BASE}/{name}/{version}/json"
    )
    if not url.startswith("https://"):  # pragma: no cover - constant base
        raise ResolveError(f"refusing non-HTTPS PyPI URL: {url!r}")
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_S) as response:
            if not response.geturl().startswith("https://"):
                raise ResolveError(
                    f"refusing non-HTTPS redirect target: {response.geturl()!r}"
                )
            body = response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ResolveError(f"network error fetching {url}: {exc}") from exc
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResolveError(f"PyPI returned non-JSON body for {url}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ResolveError(f"PyPI returned a non-object body for {url}")
    return payload


@register(PackageType.PYTHON)
class PythonResolver:
    """Resolve a Python package to its version + correct-wheel sha256."""

    type: ClassVar[PackageType] = PackageType.PYTHON

    def __init__(self, *, fetch_json: FetchJson | None = None) -> None:
        self._fetch_json = fetch_json if fetch_json is not None else _default_fetch_json

    def resolve(self, item: object) -> ResolvedPin:
        if not isinstance(item, PythonPackage):  # pragma: no cover - typing guard
            raise ResolveError(
                f"python resolver received {type(item).__name__}, not PythonPackage"
            )
        name = item.package
        body = self._fetch_json(name, item.version)
        version = _concrete_version(body, name)
        urls = _wheel_entries(body, name)
        sha = _select_wheel(urls)
        if sha is None:
            raise ResolveError(
                f"no linux x86_64 / py3-none-any wheel found for {name!r} "
                f"version {version!r} on PyPI"
            )
        return ResolvedPin(
            type=PackageType.PYTHON,
            key=name,
            version=version,
            integrity=f"sha256:{sha}",
            integrity_kind=IntegrityKind.CHECKSUM,
        )


def _concrete_version(body: dict[str, Any], name: str) -> str:
    """Return ``info.version`` — the concrete version, NEVER ``latest``."""
    info = body.get("info")
    version = info.get("version") if isinstance(info, dict) else None
    if not isinstance(version, str) or not version:
        raise ResolveError(f"PyPI body for {name!r} is missing 'info.version'")
    return version


def _wheel_entries(body: dict[str, Any], name: str) -> list[dict[str, Any]]:
    """Return the file list to select a wheel from.

    Prefers ``urls`` (present on both the latest and pinned endpoints for the
    selected release). Falls back to ``releases[version]`` if a caller supplies a
    latest-endpoint body without ``urls``.
    """
    urls = body.get("urls")
    if isinstance(urls, list) and urls:
        return [e for e in urls if isinstance(e, dict)]
    releases = body.get("releases")
    info = body.get("info")
    version = info.get("version") if isinstance(info, dict) else None
    if isinstance(releases, dict) and isinstance(version, str):
        files = releases.get(version)
        if isinstance(files, list):
            return [e for e in files if isinstance(e, dict)]
    raise ResolveError(f"PyPI body for {name!r} has no file list ('urls'/'releases')")
