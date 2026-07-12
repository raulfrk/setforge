"""Tests for the cargo resolver (spec §B3).

Resolves the concrete version + ``.crate`` sha256 from the crates.io SPARSE
INDEX (``https://index.crates.io/{prefix}/{name}``). The path prefix is
LENGTH-BUCKETED on the LOWERCASED crate name — the known trap this suite pins
across all four buckets. The response is newline-delimited JSON, one object per
version; the resolver picks the MAX non-yanked version and builds a
``sha256:{cksum}`` checksum pin.

The network boundary is INJECTABLE (a ``fetch`` callable) so these unit tests
never hit crates.io — the real fetch is Task-6's docker e2e.
"""

from __future__ import annotations

import pytest

from setforge.config import CargoPackage
from setforge.errors import ResolveError
from setforge.provision.resolve.cargo import CargoResolver, _index_path
from setforge.provision.resolve.protocol import IntegrityKind, PackageType

# A realistic crates.io sparse-index body: newline-delimited JSON, one object
# per version, each with vers/cksum/yanked (real index lines carry more fields —
# name/deps/features — which the resolver must ignore).
_SERDE_INDEX = "\n".join(
    [
        '{"name":"serde","vers":"1.0.0","cksum":"aaa0","yanked":false,"deps":[]}',
        '{"name":"serde","vers":"1.0.100","cksum":"c100","yanked":false,"deps":[]}',
        '{"name":"serde","vers":"1.0.99","cksum":"c099","yanked":false,"deps":[]}',
        '{"name":"serde","vers":"1.0.101","cksum":"c101","yanked":true,"deps":[]}',
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
        # Uppercase must lowercase for the bucket path but keep the name segment.
        ("Serde", "se/rd/serde"),
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
    # 1.0.101 is yanked, so 1.0.100 wins (NOT lexicographic max 1.0.99).
    assert pin.version == "1.0.100"
    assert pin.integrity == "sha256:c100"
    assert pin.integrity_kind is IntegrityKind.CHECKSUM
    # The fetch hit the length-bucketed sparse-index URL.
    assert record == ["https://index.crates.io/se/rd/serde"]


def test_resolve_skips_all_yanked() -> None:
    body = '{"name":"x","vers":"1.0.0","cksum":"z","yanked":true}'
    with pytest.raises(ResolveError, match="no non-yanked"):
        _resolver(body).resolve(CargoPackage(crate="x"))


def test_resolve_empty_index_body_raises() -> None:
    with pytest.raises(ResolveError):
        _resolver("").resolve(CargoPackage(crate="x"))


def test_resolve_registered_and_retrievable() -> None:
    from setforge.provision.resolve import cargo  # noqa: F401  (import registers)
    from setforge.provision.resolve.registry import get_resolver

    assert isinstance(get_resolver(PackageType.CARGO), CargoResolver)
