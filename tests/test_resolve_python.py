"""Tests for the python resolver (fetch injected; never hits pypi.org)."""

from __future__ import annotations

import sys

import pytest

from setforge.config import PythonPackage
from setforge.errors import ResolveError
from setforge.provision.resolve.protocol import IntegrityKind, PackageType
from setforge.provision.resolve.python import PythonResolver, _select_wheel

_CP = f"cp{sys.version_info.major}{sys.version_info.minor}"


def _wheel(filename: str, sha: str) -> dict[str, object]:
    return {
        "filename": filename,
        "packagetype": "bdist_wheel",
        "digests": {"sha256": sha},
    }


_SDIST: dict[str, object] = {
    "filename": "tool-2.0.tar.gz",
    "packagetype": "sdist",
    "digests": {"sha256": "src"},
}
_COMPILED_URLS: list[dict[str, object]] = [
    _SDIST,
    _wheel(f"tool-2.0-{_CP}-{_CP}-macosx_11_0_arm64.whl", "mac"),
    _wheel(f"tool-2.0-{_CP}-{_CP}-win_amd64.whl", "win"),
    _wheel("tool-2.0-cp39-cp39-manylinux_2_17_x86_64.whl", "linux-foreign"),
    _wheel(
        f"tool-2.0-{_CP}-{_CP}-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
        "linux",
    ),
]

_PURE_URLS: list[dict[str, object]] = [_wheel("puretool-1.0-py3-none-any.whl", "pure")]


def _body(urls: list[dict[str, object]], version: str = "2.0") -> dict[str, object]:
    return {"info": {"version": version}, "urls": urls}


def _resolver(body: dict[str, object], *, record: list[str] | None = None):
    def _fetch_json(name: str, version: str | None) -> dict[str, object]:
        if record is not None:
            record.append(f"{name}@{version}")
        return body

    return PythonResolver(fetch_json=_fetch_json)


def test_select_wheel_prefers_linux_cpython_over_other_platforms() -> None:
    sha = _select_wheel(_COMPILED_URLS)
    assert sha == "linux"


def test_select_wheel_falls_back_to_pure_py3() -> None:
    assert _select_wheel(_PURE_URLS) == "pure"


def test_resolve_compiled_picks_linux_wheel_sha() -> None:
    pin = _resolver(_body(_COMPILED_URLS)).resolve(PythonPackage(package="tool"))
    assert pin.type is PackageType.PYTHON
    assert pin.key == "tool"
    assert pin.version == "2.0"
    assert pin.integrity == "sha256:linux"
    assert pin.integrity_kind is IntegrityKind.CHECKSUM


def test_resolve_pure_picks_universal_wheel() -> None:
    body = _body(_PURE_URLS, version="1.0")
    pin = _resolver(body).resolve(PythonPackage(package="puretool"))
    assert pin.integrity == "sha256:pure"
    assert pin.version == "1.0"


def test_resolve_never_returns_latest() -> None:
    record: list[str] = []
    pin = _resolver(_body(_COMPILED_URLS), record=record).resolve(
        PythonPackage(package="tool")
    )
    assert pin.version != "latest"
    assert pin.version == "2.0"


def test_resolve_pinned_version_forwarded_to_fetch() -> None:
    record: list[str] = []
    _resolver(_body(_COMPILED_URLS), record=record).resolve(
        PythonPackage(package="tool", version="2.0")
    )
    assert record == ["tool@2.0"]


def test_resolve_no_wheel_raises() -> None:
    body = _body([_SDIST])
    with pytest.raises(ResolveError, match=r"no .* wheel"):
        _resolver(body).resolve(PythonPackage(package="x"))


def test_resolve_registered_and_retrievable() -> None:
    from setforge.provision.resolve import python  # noqa: F401  (import registers)
    from setforge.provision.resolve.registry import get_resolver

    assert isinstance(get_resolver(PackageType.PYTHON), PythonResolver)
