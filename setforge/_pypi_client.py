"""Stdlib PyPI JSON-API client for ``setforge upgrade``.

Single public entry point: :func:`fetch_latest_version`. Talks to
``https://pypi.org/pypi/<package>/json`` via ``urllib.request`` (stdlib
— no new runtime dep) and returns a :class:`PyPIVersionInfo` for the
highest non-prerelease, non-yanked version (or includes prereleases
when the caller opts in).

Sends a ``User-Agent`` header identifying setforge + version + repo
(PyPI rejects default ``Python-urllib/...`` UA in some contexts), and
caches the response body keyed by ``ETag`` at
``<cache_dir>/pypi-etag-<package>.json``. A 304 reply reuses the
cached body verbatim — keeps the call cheap on the common-case "user
ran ``setforge upgrade --check`` an hour ago" path.

Raises :class:`PyPIFetchError` on network / HTTP / decode / cache-IO
failures; the CLI handler renders that as a single-line error.
"""

from __future__ import annotations

import contextlib
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

from setforge.errors import PyPIFetchError

_DEFAULT_PYPI_BASE: str = "https://pypi.org/pypi"
# ``SETFORGE_PYPI_BASE`` overrides the base URL for the PyPI JSON API.
# The full request URL is ``f"{base}/{package}/json"``. Reserved for
# the Docker e2e fixture that points the client at a local
# ``python -m http.server`` serving a hand-crafted JSON body.
_PYPI_BASE_ENV: str = "SETFORGE_PYPI_BASE"


@dataclass(slots=True, frozen=True)
class PyPIVersionInfo:
    """Selected-release metadata returned by :func:`fetch_latest_version`.

    Carries only the four fields the upgrade flow needs:

    * ``version`` — the normalized version string (e.g. ``"0.3.0"``).
    * ``is_prerelease`` — ``True`` when the version is a pre-release
      (``a``/``b``/``rc``/``dev``); the caller surfaces this in the
      release-notes panel.
    * ``yanked`` — ``True`` when PyPI's ``info.yanked`` flag is set.
    * ``yanked_reason`` — PyPI's ``info.yanked_reason`` (``None`` when
      the release was not yanked or PyPI returned no reason).
    """

    version: str
    is_prerelease: bool
    yanked: bool
    yanked_reason: str | None


def _default_cache_dir() -> Path:
    """Return ``~/.cache/setforge``; not auto-created (writer ensures it)."""
    return Path.home() / ".cache" / "setforge"


def _etag_cache_path(*, cache_dir: Path, package: str) -> Path:
    """Path of the on-disk ETag sidecar for ``package`` under ``cache_dir``."""
    return cache_dir / f"pypi-etag-{package}.json"


def _read_etag_cache(path: Path) -> tuple[str, dict[str, Any]] | None:
    """Read ``(etag, body_json)`` from ``path``; return ``None`` on any failure.

    Cache misses are not exceptional — the function silently returns
    ``None`` on missing file / decode failure / shape mismatch so the
    caller falls back to an unconditional fetch.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    etag = parsed.get("etag")
    body = parsed.get("body")
    if not isinstance(etag, str) or not isinstance(body, dict):
        return None
    return etag, body


def _write_etag_cache(path: Path, *, etag: str, body: dict[str, Any]) -> None:
    """Write ``{"etag": ..., "body": ...}`` to ``path`` atomically-ish.

    Best-effort: failures swallow silently (cache write is an
    optimization, not a correctness requirement). The cache dir is
    created on demand here so the caller need not pre-create it.
    """
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"etag": etag, "body": body}),
            encoding="utf-8",
        )


def _build_user_agent(*, current_version: str) -> str:
    """Compose the User-Agent header value for PyPI requests."""
    return f"setforge/{current_version} (+https://github.com/raulfrk/setforge)"


def _select_latest(
    *, releases: dict[str, list[dict[str, Any]]], include_prereleases: bool
) -> str | None:
    """Pick the highest non-yanked (and optionally non-prerelease) release.

    PyPI's ``releases`` map keys versions to per-file lists; we filter
    by ``yanked`` at the file level (any file yanked → release yanked,
    matching PyPI's own UI behavior) and by ``packaging.version``
    parseability. Returns the normalized string of the highest version,
    or ``None`` when no candidate survives the filter.
    """
    candidates: list[Version] = []
    for raw_ver, files in releases.items():
        try:
            ver = Version(raw_ver)
        except InvalidVersion:
            continue
        if not include_prereleases and ver.is_prerelease:
            continue
        if files and all(f.get("yanked") for f in files):
            continue
        candidates.append(ver)
    if not candidates:
        return None
    return str(max(candidates))


def _fetch_json(
    *,
    url: str,
    user_agent: str,
    timeout: tuple[float, float],
    cached_etag: str | None,
) -> tuple[dict[str, Any], str | None, bool]:
    """Issue the GET; return ``(body, new_etag, used_304)``.

    Connect-timeout / read-timeout are not separately tunable in
    stdlib ``urllib``; ``timeout=`` accepts a single seconds value, so
    we use ``max(connect, read)`` of the supplied tuple. Raises
    :class:`PyPIFetchError` on all network / HTTP / decode failures.
    """
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    if cached_etag is not None:
        request.add_header("If-None-Match", cached_etag)
    effective_timeout = max(timeout)
    try:
        with urllib.request.urlopen(request, timeout=effective_timeout) as response:
            status = response.status
            body_bytes = response.read()
            new_etag = response.headers.get("ETag")
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return ({}, cached_etag, True)
        raise PyPIFetchError(
            f"PyPI returned HTTP {exc.code} for {url}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise PyPIFetchError(
            f"network error contacting PyPI ({url}): {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise PyPIFetchError(f"timeout contacting PyPI ({url})") from exc
    if status != 200:
        raise PyPIFetchError(f"PyPI returned unexpected status {status} for {url}")
    try:
        payload = json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PyPIFetchError(f"PyPI returned non-JSON body for {url}: {exc}") from exc
    if not isinstance(payload, dict):
        raise PyPIFetchError(f"PyPI returned non-object body for {url}")
    return payload, new_etag, False


def fetch_latest_version(
    *,
    package: str,
    current_version: str,
    timeout: tuple[float, float] = (5.0, 10.0),
    cache_dir: Path | None = None,
    include_prereleases: bool = False,
) -> PyPIVersionInfo:
    """Fetch the latest version of ``package`` from PyPI's JSON API.

    Sends ``User-Agent: setforge/<current_version> (+<repo-url>)`` and
    ``If-None-Match: <etag>`` when an ETag is cached at
    ``<cache_dir>/pypi-etag-<package>.json``. A 304 reply reuses the
    cached body verbatim.

    Returns a :class:`PyPIVersionInfo` for the highest version that
    survives the prerelease + yanked filter. Raises
    :class:`PyPIFetchError` when no surviving version exists or on any
    network / HTTP / decode failure.
    """
    resolved_cache_dir = cache_dir if cache_dir is not None else _default_cache_dir()
    cache_path = _etag_cache_path(cache_dir=resolved_cache_dir, package=package)
    cached = _read_etag_cache(cache_path)
    cached_etag, cached_body = cached if cached is not None else (None, None)
    base = os.environ.get(_PYPI_BASE_ENV, _DEFAULT_PYPI_BASE).rstrip("/")
    url = f"{base}/{package}/json"
    user_agent = _build_user_agent(current_version=current_version)
    body, new_etag, used_304 = _fetch_json(
        url=url, user_agent=user_agent, timeout=timeout, cached_etag=cached_etag
    )
    if used_304 and cached_body is not None:
        body = cached_body
    elif used_304 and cached_body is None:
        # Server claimed 304 but we have no cached body — degrade to a
        # hard refresh by retrying without the conditional header.
        body, new_etag, _ = _fetch_json(
            url=url, user_agent=user_agent, timeout=timeout, cached_etag=None
        )
    releases_raw = body.get("releases")
    if not isinstance(releases_raw, dict):
        raise PyPIFetchError(f"PyPI body for {package} missing 'releases' map")
    selected = _select_latest(
        releases=releases_raw, include_prereleases=include_prereleases
    )
    if selected is None:
        raise PyPIFetchError(f"no non-yanked release found for {package} on PyPI")
    if not used_304 and new_etag is not None:
        _write_etag_cache(cache_path, etag=new_etag, body=body)
    yanked, yanked_reason = _yanked_state(body=body, selected=selected)
    try:
        is_prerelease = Version(selected).is_prerelease
    except InvalidVersion:
        is_prerelease = False
    return PyPIVersionInfo(
        version=selected,
        is_prerelease=is_prerelease,
        yanked=yanked,
        yanked_reason=yanked_reason,
    )


def _yanked_state(*, body: dict[str, Any], selected: str) -> tuple[bool, str | None]:
    """Compute ``(yanked, yanked_reason)`` for ``selected`` from PyPI body."""
    releases_raw = body.get("releases", {})
    files_for_selected = (
        releases_raw.get(selected, []) if isinstance(releases_raw, dict) else []
    )
    yanked = bool(files_for_selected) and all(
        bool(f.get("yanked")) for f in files_for_selected
    )
    if not yanked:
        return False, None
    for f in files_for_selected:
        reason = f.get("yanked_reason")
        if isinstance(reason, str) and reason:
            return True, reason
    info = body.get("info") if isinstance(body.get("info"), dict) else {}
    info_reason = info.get("yanked_reason") if isinstance(info, dict) else None
    if isinstance(info_reason, str) and info_reason:
        return True, info_reason
    return True, None
