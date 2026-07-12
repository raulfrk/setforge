"""Lock CONSUMPTION at install — the lock overrides ProvisionItems offline.

Task 6: when ``setforge.lock`` is present, ``run_provisioning`` overrides each
:class:`ProvisionItem`'s version/integrity from the matching lock pin
(matched by ``(type, key)``) BEFORE reconcile, with ZERO resolver/network
calls. These unit tests cover the pure override (:mod:`setforge.provision.lock_apply`)
and its wiring into ``run_provisioning``:

- python/go: the locked ``version`` lands on ``item.version``.
- cargo: the locked version/checksum are recorded on the item (native index
  handles bytes) — no crash, values visible.
- github_release: the locked CONCRETE tag flows into ``item.config.tag`` (so
  the download URL uses ``v1.2.3``, never literal ``latest``) AND the locked
  checksum lands on ``item.config.checksum``.
- offline: a lock present means NO resolver is instantiated/called.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from setforge.config import (
    CargoPackage,
    Config,
    GitHubReleasePackage,
    GoPackage,
    Profile,
    PythonPackage,
    ResolvedProfile,
    TrackedFile,
)
from setforge.lockfile import LockFile
from setforge.provision.dispatch import resolve_provision_items, run_provisioning
from setforge.provision.lock_apply import apply_lock_to_items
from setforge.provision.protocol import Outcome, ProvisionItem, ProvisionOutcome
from setforge.provision.resolve.protocol import (
    IntegrityKind,
    PackageType,
    ResolvedPin,
)


def _cfg(**kw: object) -> Config:
    return Config(
        tracked_files={"d": TrackedFile(src=Path("a"), dst="b")},
        profiles={"p": Profile()},
        **kw,  # type: ignore[arg-type]
    )


def _pin(
    pkg_type: PackageType, key: str, version: str, integrity: str, kind: IntegrityKind
) -> ResolvedPin:
    return ResolvedPin(
        type=pkg_type,
        key=key,
        version=version,
        integrity=integrity,
        integrity_kind=kind,
        profiles=("p",),
    )


# ---------------------------------------------------------------------------
# apply_lock_to_items — the pure override
# ---------------------------------------------------------------------------


def test_python_locked_version_lands_on_item_version() -> None:
    cfg = _cfg(packages={"tool": PythonPackage(package="some-tool", version=None)})
    items = resolve_provision_items(cfg, ResolvedProfile(packages=["tool"]))
    lock = LockFile(
        packages=(
            _pin(
                PackageType.PYTHON,
                "some-tool",
                "1.2.3",
                "sha256:dead",
                IntegrityKind.CHECKSUM,
            ),
        )
    )
    out = apply_lock_to_items(items, lock)
    assert out[0].version == "1.2.3"
    assert out[0].checksum == "sha256:dead"


def test_go_locked_version_lands_on_item_version() -> None:
    cfg = _cfg(packages={"m": GoPackage(module="github.com/x/y", version=None)})
    items = resolve_provision_items(cfg, ResolvedProfile(packages=["m"]))
    lock = LockFile(
        packages=(
            _pin(
                PackageType.GO,
                "github.com/x/y",
                "v1.4.2",
                "h1:abc",
                IntegrityKind.SUM,
            ),
        )
    )
    out = apply_lock_to_items(items, lock)
    assert out[0].version == "v1.4.2"


def test_cargo_locked_values_recorded_no_crash() -> None:
    cfg = _cfg(packages={"rg": CargoPackage(crate="ripgrep")})
    items = resolve_provision_items(cfg, ResolvedProfile(packages=["rg"]))
    lock = LockFile(
        packages=(
            _pin(
                PackageType.CARGO,
                "ripgrep",
                "14.0.0",
                "sha256:beef",
                IntegrityKind.CHECKSUM,
            ),
        )
    )
    out = apply_lock_to_items(items, lock)
    assert out[0].version == "14.0.0"
    assert out[0].checksum == "sha256:beef"


def test_github_release_latest_spec_gets_pinned_concrete_tag() -> None:
    # The KEY github_release regression: a `tag: latest` spec must install the
    # PINNED concrete tag — the provisioner builds .../download/<tag>/... and
    # `latest` is not a valid download path.
    pkg = GitHubReleasePackage(
        repo="owner/tool",
        tag="latest",
        asset="tool.tar.gz",
        binary="tool",
        install="~/.local/bin",
        checksum=None,
    )
    cfg = _cfg(packages={"tool": pkg})
    items = resolve_provision_items(cfg, ResolvedProfile(packages=["tool"]))
    lock = LockFile(
        packages=(
            _pin(
                PackageType.GITHUB_RELEASE,
                "owner/tool",
                "v1.2.3",
                "sha256:9f3c",
                IntegrityKind.CHECKSUM,
            ),
        )
    )
    out = apply_lock_to_items(items, lock)
    locked_pkg = out[0].config
    assert isinstance(locked_pkg, GitHubReleasePackage)
    # The concrete tag replaced literal `latest` — the download URL is built
    # from pkg.tag, so this is what makes install hit .../download/v1.2.3/... .
    assert locked_pkg.tag == "v1.2.3"
    assert locked_pkg.checksum == "sha256:9f3c"
    from setforge.provision.github_release import _asset_url

    assert "download/v1.2.3/" in _asset_url(locked_pkg)
    assert "latest" not in _asset_url(locked_pkg)


def test_unlocked_item_passes_through_unchanged() -> None:
    # A package with NO lock entry is left exactly as resolved (no lock forces
    # partial coverage; --locked is the fail-closed coverage gate, not apply).
    cfg = _cfg(packages={"rg": CargoPackage(crate="ripgrep")})
    items = resolve_provision_items(cfg, ResolvedProfile(packages=["rg"]))
    out = apply_lock_to_items(items, LockFile(packages=()))
    assert out[0] == items[0]


# ---------------------------------------------------------------------------
# run_provisioning wiring — lock present ⇒ apply, no resolver ⇒ offline
# ---------------------------------------------------------------------------


def test_run_provisioning_applies_lock_and_calls_no_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A lock present overrides the item BEFORE reconcile, and the resolver
    # registry is NEVER consulted (offline-safe). Patch get_resolver to raise.
    import setforge.provision.lock_apply as lock_apply
    import setforge.provision.python as python_prov

    def _boom_resolver(*_a: object, **_k: object) -> object:
        raise AssertionError("offline install must not resolve when a lock exists")

    monkeypatch.setattr(lock_apply, "get_resolver", _boom_resolver, raising=False)

    applied: list[str | None] = []

    def _apply(self: object, item: ProvisionItem) -> ProvisionOutcome:
        applied.append(item.version)
        return ProvisionOutcome(item=item, outcome=Outcome.OK, detail="installed")

    monkeypatch.setattr(python_prov.PythonProvisioner, "probe", lambda self: set())
    monkeypatch.setattr(python_prov.PythonProvisioner, "apply_one", _apply)

    cfg = _cfg(packages={"tool": PythonPackage(package="some-tool", version=None)})
    resolved = ResolvedProfile(packages=["tool"])
    lock = LockFile(
        packages=(
            _pin(
                PackageType.PYTHON,
                "some-tool",
                "9.9.9",
                "sha256:dead",
                IntegrityKind.CHECKSUM,
            ),
        )
    )
    run_provisioning(cfg, resolved, lock=lock)
    assert applied == ["9.9.9"]  # the LOCKED version flowed into apply
