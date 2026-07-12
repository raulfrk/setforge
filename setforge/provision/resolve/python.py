"""The python :class:`Resolver`: wheel selection prefers linux-x86_64/CPython,
else pure, else fails closed."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any, ClassVar

from setforge.config import PythonPackage
from setforge.errors import ResolveError
from setforge.provision.resolve._fetch import fetch_bytes
from setforge.provision.resolve.protocol import (
    IntegrityKind,
    PackageType,
    ResolvedPin,
)
from setforge.provision.resolve.registry import register

__all__ = ["PythonResolver"]

_PYPI_BASE = "https://pypi.org/pypi"
_FETCH_TIMEOUT_S = 10.0
_MAX_JSON_BYTES = 32 * 1024 * 1024

FetchJson = Callable[[str, "str | None"], "dict[str, Any]"]


def _cpython_tag() -> str:
    return f"cp{sys.version_info.major}{sys.version_info.minor}"


def _is_linux_x86_64_wheel(filename: str) -> bool:
    lower = filename.lower()
    # and "x86_64": manylinux/musllinux also tag aarch64, so family alone over-matches.
    return (
        "manylinux" in lower or "musllinux" in lower or "linux_x86_64" in lower
    ) and "x86_64" in lower


def _select_wheel(urls: list[dict[str, Any]]) -> str | None:
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
        if "none-any" in lower:
            pure = pure or digest
            continue
        if (
            compiled is None
            and _is_linux_x86_64_wheel(filename)
            and (cp in lower or "abi3" in lower)
        ):
            compiled = digest
    return compiled if compiled is not None else pure


def _sha256_of(entry: dict[str, Any]) -> str | None:
    digests = entry.get("digests")
    if not isinstance(digests, dict):
        return None
    sha = digests.get("sha256")
    return sha if isinstance(sha, str) and sha else None


def _default_fetch_json(name: str, version: str | None) -> dict[str, Any]:
    url = (
        f"{_PYPI_BASE}/{name}/json"
        if version is None
        else f"{_PYPI_BASE}/{name}/{version}/json"
    )
    body = fetch_bytes(url, timeout=_FETCH_TIMEOUT_S, max_bytes=_MAX_JSON_BYTES)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResolveError(f"PyPI returned non-JSON body for {url}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ResolveError(f"PyPI returned a non-object body for {url}")
    return payload


@register(PackageType.PYTHON)
class PythonResolver:
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
