"""Tests for the cargo resolver (fetch injected; never hits crates.io)."""

from __future__ import annotations

import pytest

from setforge.config import CargoPackage
from setforge.errors import ResolveError
from setforge.provision.resolve.cargo import (
    CargoResolver,
    _index_path,
    registry_checksum,
)
from setforge.provision.resolve.protocol import IntegrityKind, PackageType

_SERDE_INDEX = "\n".join(
    [
        f'{{"name":"serde","vers":"1.0.0","cksum":"{"a" * 64}",'
        '"yanked":false,"deps":[]}',
        f'{{"name":"serde","vers":"1.0.100","cksum":"{"b" * 64}",'
        '"yanked":false,"deps":[]}',
        f'{{"name":"serde","vers":"1.0.99","cksum":"{"c" * 64}",'
        '"yanked":false,"deps":[]}',
        f'{{"name":"serde","vers":"1.0.101","cksum":"{"d" * 64}",'
        '"yanked":true,"deps":[]}',
    ]
)


def _resolver(body: str, *, record: list[str] | None = None) -> CargoResolver:
    def _fetch(url: str) -> str:
        if record is not None:
            record.append(url)
        return body

    return CargoResolver(fetch=_fetch)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a", "1/a"),
        ("ab", "2/ab"),
        ("abc", "3/a/abc"),
        ("serde", "se/rd/serde"),
        ("Serde", "se/rd/serde"),  # must lowercase
    ],
)
def test_index_path_buckets(name: str, expected: str) -> None:
    assert _index_path(name) == expected


def test_resolve_picks_max_non_yanked_and_builds_checksum() -> None:
    record: list[str] = []
    resolver = _resolver(_SERDE_INDEX, record=record)
    pin = resolver.resolve(CargoPackage(crate="serde"))
    assert pin.type is PackageType.CARGO
    assert pin.key == "serde"
    assert pin.version == "1.0.100"  # 1.0.101 is yanked; not lexicographic 1.0.99
    assert pin.integrity == f"sha256:{'b' * 64}"
    assert pin.integrity_kind is IntegrityKind.CHECKSUM
    assert record == ["https://index.crates.io/se/rd/serde"]


def test_resolve_skips_all_yanked() -> None:
    body = '{"name":"x","vers":"1.0.0","cksum":"z","yanked":true}'
    with pytest.raises(ResolveError, match="no non-yanked"):
        _resolver(body).resolve(CargoPackage(crate="x"))


def test_resolve_empty_index_body_raises() -> None:
    with pytest.raises(ResolveError):
        _resolver("").resolve(CargoPackage(crate="x"))


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        "\n{}\n",
        '{"vers":[],"cksum":"bad","yanked":false}',
        f'{{"name":"x","vers":[],"cksum":"{"a" * 64}","yanked":false}}',
        f'{{"name":"x","vers":"not a version","cksum":"{"a" * 64}","yanked":false}}',
        '{"name":"x","vers":"1.2.3","cksum":"bad","yanked":false}',
        f'{{"name":"other","vers":"1.2.3","cksum":"{"a" * 64}","yanked":false}}',
    ],
)
def test_resolve_rejects_malformed_index_rows(body: str) -> None:
    with pytest.raises(ResolveError):
        _resolver(body).resolve(CargoPackage(crate="x"))


def test_registry_checksum_selects_exact_row_including_yanked() -> None:
    body = "\n".join(
        [
            f'{{"name":"serde","vers":"1.0.0","cksum":"{"a" * 64}","yanked":false}}',
            f'{{"name":"serde","vers":"1.1.0","cksum":"{"B" * 64}","yanked":true}}',
        ]
    )
    urls: list[str] = []

    def _fetch(url: str) -> str:
        urls.append(url)
        return body

    assert registry_checksum("Serde", "1.1.0", fetch=_fetch) == (f"sha256:{'b' * 64}")
    assert urls == ["https://index.crates.io/se/rd/serde"]


def test_registry_checksum_uses_bounded_default_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from setforge.provision.resolve import cargo as cargo_resolve

    body = f'{{"name":"serde","vers":"1.0.0","cksum":"{"a" * 64}"}}'.encode()
    calls: list[tuple[str, float, int]] = []

    def _fetch(url: str, *, timeout: float, max_bytes: int) -> bytes:
        calls.append((url, timeout, max_bytes))
        return body

    monkeypatch.setattr(cargo_resolve, "fetch_bytes", _fetch)
    assert registry_checksum("serde", "1.0.0") == f"sha256:{'a' * 64}"
    assert calls == [
        (
            "https://index.crates.io/se/rd/serde",
            cargo_resolve._FETCH_TIMEOUT_S,
            cargo_resolve._MAX_INDEX_BYTES,
        )
    ]


def test_registry_checksum_translates_invalid_utf8_from_default_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from setforge.provision.resolve import cargo as cargo_resolve

    monkeypatch.setattr(cargo_resolve, "fetch_bytes", lambda *_args, **_kwargs: b"\xff")
    with pytest.raises(ResolveError, match="valid UTF-8"):
        registry_checksum("serde", "1.0.0")


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ('{"name":"serde","vers":"2.0.0","cksum":"bad"}', "malformed"),
        ('{"vers":"1.0.0","cksum":"' + "a" * 64 + '"}', "no registry release"),
        ("not json", "non-JSON"),
        ("[]", "no registry release"),
        ("\n", "no registry release"),
        (
            '{"name":"other","vers":"2.0.0","cksum":"' + "a" * 64 + '"}',
            "wrong name",
        ),
    ],
)
def test_registry_checksum_rejects_malformed_or_missing_exact_row(
    body: str, match: str
) -> None:
    with pytest.raises(ResolveError, match=match):
        registry_checksum("serde", "2.0.0", fetch=lambda _url: body)


def test_resolve_registered_and_retrievable() -> None:
    from setforge.provision.resolve import cargo  # noqa: F401  (import registers)
    from setforge.provision.resolve.registry import get_resolver

    assert isinstance(get_resolver(PackageType.CARGO), CargoResolver)
