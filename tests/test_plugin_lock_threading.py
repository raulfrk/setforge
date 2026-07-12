"""Lock -> plugin reconcile threading: pins the cache to the sha BEFORE
install, never ``origin/HEAD``. git/claude boundaries mocked."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from setforge import claude_marketplace_cache as mp_cache
from setforge.claude_marketplace_cache import checkout_marketplace_at
from setforge.config import (
    ClaudeInstallMode,
    ClaudePluginRef,
    MarketplaceSource,
    MarketplaceSourceKind,
)
from setforge.errors import MarketplaceCacheMiss, ProvisionItemFailed
from setforge.lockfile import LockFile
from setforge.provision.lock_apply import plugin_pins
from setforge.provision.plugin import PluginProvisioner
from setforge.provision.protocol import Identity, Outcome, ProvisionItem
from setforge.provision.resolve.protocol import IntegrityKind, PackageType, ResolvedPin

_SHA = "4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f"

_PLUGIN_INSTALL = "setforge.provision.plugin.claude_plugins.plugin_install"
_PLUGIN_ENABLE = "setforge.provision.plugin.claude_plugins.plugin_enable"
_CHECKOUT = "setforge.provision.plugin.claude_marketplace_cache.checkout_marketplace_at"


def _plugin_pin(key: str, sha: str = _SHA) -> ResolvedPin:
    return ResolvedPin(
        type=PackageType.PLUGIN,
        key=key,
        version=sha,
        integrity=sha,
        integrity_kind=IntegrityKind.SHA,
    )


def _item(pid: str) -> ProvisionItem:
    return ProvisionItem(type="plugin", identity=Identity(key=pid, display=pid))


def test_plugin_pins_filters_and_keeps_case() -> None:
    plug = _plugin_pin("Revdiff@Revdiff")
    ext = ResolvedPin(
        type=PackageType.EXTENSION,
        key="esbenp.prettier-vscode",
        version="1.2.3",
        integrity="sha256:" + "a" * 64,
        integrity_kind=IntegrityKind.CHECKSUM,
    )
    lock = LockFile(version=1, packages=(plug, ext))

    pins = plugin_pins(lock)

    assert set(pins) == {"Revdiff@Revdiff"}
    assert "revdiff@revdiff" not in pins
    assert pins["Revdiff@Revdiff"].version == _SHA


def test_plugin_pins_none_is_empty() -> None:
    assert plugin_pins(None) == {}


def test_plugin_pins_imports_no_resolver() -> None:
    import setforge.provision.lock_apply as lock_apply

    assert not hasattr(lock_apply, "get_resolver")
    assert "resolve.registry" not in {
        getattr(v, "__name__", "") for v in vars(lock_apply).values()
    }


def test_checkout_resets_to_pinned_sha_not_origin_head(
    fake_git, tmp_path: Path
) -> None:
    fake = fake_git()
    cache_dir = tmp_path / "mp"
    cache_dir.mkdir()

    checkout_marketplace_at(cache_dir, _SHA)

    assert fake.reset_targets == [_SHA]
    assert "origin/HEAD" not in fake.reset_targets
    reset_calls = [c for c in fake.calls if c[3:5] == ["reset", "--hard"]]
    assert reset_calls
    argv = reset_calls[0]
    assert argv[5] == _SHA
    assert argv[6] == "--"


def test_checkout_rejects_non_sha_ref(fake_git, tmp_path: Path) -> None:
    fake = fake_git()
    cache_dir = tmp_path / "mp"
    cache_dir.mkdir()

    with pytest.raises(MarketplaceCacheMiss, match="non-SHA"):
        checkout_marketplace_at(cache_dir, "origin/HEAD")
    assert fake.reset_targets == []


def test_checkout_git_failure_surfaces_as_cache_miss(fake_git, tmp_path: Path) -> None:
    fake_git(unknown_shas={_SHA})  # wires git; the reset for _SHA fails
    cache_dir = tmp_path / "mp"
    cache_dir.mkdir()

    with pytest.raises(MarketplaceCacheMiss):
        checkout_marketplace_at(cache_dir, _SHA)


def _provisioner(checkouts=None) -> PluginProvisioner:
    prov = PluginProvisioner(checkouts=checkouts)
    with patch(
        "setforge.provision.plugin.claude_plugins.list_installed", return_value={}
    ):
        prov.probe()
    return prov


def test_apply_one_pins_cache_before_install(tmp_path: Path) -> None:
    cache_dir = tmp_path / "mp"
    prov = _provisioner(checkouts={"revdiff@revdiff": (cache_dir, _SHA)})
    order: list[str] = []
    with (
        patch(_CHECKOUT, side_effect=lambda *a: order.append(f"checkout:{a[1]}")),
        patch(_PLUGIN_INSTALL, side_effect=lambda *a: order.append("install")),
        patch(_PLUGIN_ENABLE),
    ):
        out = prov.apply_one(_item("revdiff@revdiff"))

    assert out.outcome is Outcome.OK
    assert order == [f"checkout:{_SHA}", "install"]


def test_apply_one_no_checkout_target_installs_unchanged() -> None:
    prov = _provisioner(checkouts={})
    with (
        patch(_CHECKOUT) as chk,
        patch(_PLUGIN_INSTALL) as ins,
        patch(_PLUGIN_ENABLE) as en,
    ):
        prov.apply_one(_item("revdiff@revdiff"))

    chk.assert_not_called()
    ins.assert_called_once_with("revdiff", "revdiff")
    en.assert_called_once_with("revdiff@revdiff")


def test_apply_one_checkout_failure_is_hard_no_install(tmp_path: Path) -> None:
    prov = _provisioner(checkouts={"revdiff@revdiff": (tmp_path / "mp", _SHA)})
    with (
        patch(_CHECKOUT, side_effect=MarketplaceCacheMiss("bad sha")),
        patch(_PLUGIN_INSTALL) as ins,
        patch(_PLUGIN_ENABLE),
        pytest.raises(ProvisionItemFailed) as excinfo,
    ):
        prov.apply_one(_item("revdiff@revdiff"))

    assert excinfo.value.kind is Outcome.HARD
    ins.assert_not_called()


def test_apply_one_disabled_plugin_skips_checkout_and_install(tmp_path: Path) -> None:
    prov = PluginProvisioner(checkouts={"b@mk": (tmp_path / "mp", _SHA)})
    with patch(
        "setforge.provision.plugin.claude_plugins.list_installed",
        return_value={"b@mk": {"id": "b@mk", "enabled": False}},
    ):
        prov.probe()
    with (
        patch(_CHECKOUT) as chk,
        patch(_PLUGIN_INSTALL) as ins,
        patch(_PLUGIN_ENABLE) as en,
    ):
        prov.apply_one(_item("b@mk"))

    chk.assert_not_called()
    ins.assert_not_called()
    en.assert_called_once_with("b@mk")


from setforge.claude_plugins import _plugin_checkout_targets  # noqa: E402
from tests.conftest import _make_config  # noqa: E402


def test_checkout_targets_local_clone_pinned_plugin(tmp_path: Path) -> None:
    cfg = _make_config(
        marketplaces={
            "revdiff": MarketplaceSource(
                source=MarketplaceSourceKind.GITHUB, repo="owner/revdiff"
            )
        },
        claude_plugins={"revdiff": ClaudePluginRef(marketplace="revdiff")},
    )
    pins = {"revdiff@revdiff": _plugin_pin("revdiff@revdiff")}
    targets = _plugin_checkout_targets(
        cfg,
        {"revdiff@revdiff"},
        pins,
        ClaudeInstallMode.LOCAL_CLONE,
        tmp_path / "cache",
    )
    assert "revdiff@revdiff" in targets
    cache_dir, sha = targets["revdiff@revdiff"]
    assert cache_dir == tmp_path / "cache" / "revdiff"
    assert sha == _SHA


def test_checkout_targets_empty_in_regular_mode(tmp_path: Path) -> None:
    cfg = _make_config(
        marketplaces={
            "revdiff": MarketplaceSource(
                source=MarketplaceSourceKind.GITHUB, repo="owner/revdiff"
            )
        },
        claude_plugins={"revdiff": ClaudePluginRef(marketplace="revdiff")},
    )
    pins = {"revdiff@revdiff": _plugin_pin("revdiff@revdiff")}
    assert (
        _plugin_checkout_targets(
            cfg,
            {"revdiff@revdiff"},
            pins,
            ClaudeInstallMode.REGULAR,
            tmp_path / "cache",
        )
        == {}
    )


def test_checkout_targets_empty_without_pin(tmp_path: Path) -> None:
    cfg = _make_config(
        marketplaces={
            "revdiff": MarketplaceSource(
                source=MarketplaceSourceKind.GITHUB, repo="owner/revdiff"
            )
        },
        claude_plugins={"revdiff": ClaudePluginRef(marketplace="revdiff")},
    )
    assert (
        _plugin_checkout_targets(
            cfg,
            {"revdiff@revdiff"},
            {},
            ClaudeInstallMode.LOCAL_CLONE,
            tmp_path / "cache",
        )
        == {}
    )


def test_checkout_targets_skip_path_marketplace(tmp_path: Path) -> None:
    cfg = _make_config(
        marketplaces={
            "local": MarketplaceSource(
                source=MarketplaceSourceKind.PATH, path=tmp_path / "preinstalled"
            )
        },
        claude_plugins={"revdiff": ClaudePluginRef(marketplace="local")},
    )
    pins = {"revdiff@local": _plugin_pin("revdiff@local")}
    assert (
        _plugin_checkout_targets(
            cfg,
            {"revdiff@local"},
            pins,
            ClaudeInstallMode.LOCAL_CLONE,
            tmp_path / "cache",
        )
        == {}
    )


def test_reconcile_threads_pins_into_checkout(monkeypatch, tmp_path: Path) -> None:
    import setforge.claude_plugins as cp

    cfg = _make_config(
        marketplaces={
            "revdiff": MarketplaceSource(
                source=MarketplaceSourceKind.GITHUB, repo="owner/revdiff"
            )
        },
        claude_plugins={"revdiff": ClaudePluginRef(marketplace="revdiff")},
    )
    from setforge.config import ResolvedProfile

    resolved = ResolvedProfile(claude_plugins=["revdiff"])
    lock = LockFile(version=1, packages=(_plugin_pin("revdiff@revdiff"),))

    monkeypatch.setattr(mp_cache, "MARKETPLACE_CACHE_ROOT", tmp_path / "cache")

    class _HL:
        class claude:
            install_mode = ClaudeInstallMode.LOCAL_CLONE

    monkeypatch.setattr(cp, "load_host_local_config", lambda: _HL)
    monkeypatch.setattr(cp, "_marketplaces_to_add", lambda *a, **k: [])
    monkeypatch.setattr(cp, "list_marketplaces", lambda: {})
    monkeypatch.setattr(cp, "list_installed", lambda: {})

    captured: dict[str, tuple[Path, str]] = {}
    real_provisioner = PluginProvisioner

    def _capturing_provisioner(*, checkouts=None):
        captured.update(checkouts or {})
        return real_provisioner(checkouts=checkouts)

    monkeypatch.setattr(
        "setforge.provision.plugin.PluginProvisioner", _capturing_provisioner
    )
    monkeypatch.setattr(
        "setforge.provision.plugin.claude_plugins.list_installed", lambda: {}
    )
    with (
        patch(_CHECKOUT) as chk,
        patch(_PLUGIN_INSTALL),
        patch(_PLUGIN_ENABLE),
    ):
        cp.reconcile(cfg, resolved, pins=plugin_pins(lock))

    assert captured == {"revdiff@revdiff": (tmp_path / "cache" / "revdiff", _SHA)}
    chk.assert_called_once_with(tmp_path / "cache" / "revdiff", _SHA)


def test_retry_pinned_install_repins_cache(monkeypatch, tmp_path: Path) -> None:
    """A retry re-pins the cache — never a silent origin/HEAD install past the pin."""
    import setforge.cli._plugin_helpers as ph

    cfg = _make_config(
        marketplaces={
            "revdiff": MarketplaceSource(
                source=MarketplaceSourceKind.GITHUB, repo="owner/revdiff"
            )
        },
        claude_plugins={"revdiff": ClaudePluginRef(marketplace="revdiff")},
    )
    monkeypatch.setattr(
        ph.claude_marketplace_cache, "MARKETPLACE_CACHE_ROOT", tmp_path / "cache"
    )

    class _HL:
        class claude:
            install_mode = ClaudeInstallMode.LOCAL_CLONE

    monkeypatch.setattr(ph, "load_host_local_config", lambda: _HL)

    order: list[str] = []
    monkeypatch.setattr(
        ph.claude_marketplace_cache,
        "checkout_marketplace_at",
        lambda cd, sha: order.append(f"checkout:{sha}"),
    )
    monkeypatch.setattr(
        ph.claude_plugins_mod,
        "plugin_install",
        lambda name, mp: order.append(f"install:{name}@{mp}"),
    )

    pins = {"revdiff@revdiff": _plugin_pin("revdiff@revdiff")}
    err = ph._retry_plugin_op(cfg, "revdiff@revdiff", "install", pins=pins)

    assert err is None
    assert order == [f"checkout:{_SHA}", "install:revdiff@revdiff"]


def test_retry_unpinned_install_does_not_repin(monkeypatch, tmp_path: Path) -> None:
    import setforge.cli._plugin_helpers as ph

    cfg = _make_config(
        marketplaces={
            "revdiff": MarketplaceSource(
                source=MarketplaceSourceKind.GITHUB, repo="owner/revdiff"
            )
        },
        claude_plugins={"revdiff": ClaudePluginRef(marketplace="revdiff")},
    )
    chk = []
    monkeypatch.setattr(
        ph.claude_marketplace_cache,
        "checkout_marketplace_at",
        lambda cd, sha: chk.append(sha),
    )
    installs: list[tuple[str, str]] = []
    monkeypatch.setattr(
        ph.claude_plugins_mod,
        "plugin_install",
        lambda name, mp: installs.append((name, mp)),
    )

    err = ph._retry_plugin_op(cfg, "revdiff@revdiff", "install", pins={})

    assert err is None
    assert chk == []  # no re-pin without a lock
    assert installs == [("revdiff", "revdiff")]
