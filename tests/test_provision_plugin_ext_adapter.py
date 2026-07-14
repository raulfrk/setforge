"""Adapter tests: ``vscode_extensions`` + ``claude_plugins`` reconcile
routed through the driver.

These pin the behavior of the additive install path after each was rewired to
run through :func:`setforge.provision.driver.reconcile` + its provisioner
(:class:`~setforge.provision.extension.ExtensionProvisioner` /
:class:`~setforge.provision.plugin.PluginProvisioner`), while the PRUNE
uninstall/disable loop stays local to the caller module. The provisioner
seams are patched so no real ``code`` / ``claude`` CLI is invoked.
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import patch

from setforge import reconcile_adapter
from setforge.claude_plugins import reconcile as plugin_reconcile
from setforge.config import (
    ClaudePluginRef,
    Config,
    Extensions,
    MarketplaceSource,
    MarketplaceSourceKind,
    ReconcilePolicy,
    resolve_profile,
)
from setforge.errors import ExtensionInstallFailed
from setforge.vscode_extensions import reconcile
from tests.conftest import _make_config, _make_resolved

# The provisioner drives the additive install path; patch its seams.
_LIST = "setforge.provision.extension.vscode_extensions.list_installed"
_INSTALL = "setforge.provision.extension.vscode_extensions.install_one"
# The PRUNE loop + to_uninstall diff still call the local module seams.
_LOCAL_RESOLVE = "setforge.vscode_extensions.resolve_binary"
_LOCAL_LIST = "setforge.vscode_extensions.list_installed"
_LOCAL_SUBPROCESS_RUN = "setforge.vscode_extensions.subprocess.run"


def test_additive_install_failure_records_failed_and_lists_to_install() -> None:
    """A HARD install failure (driver-routed) must land in ``report.failed``
    while the failed id is still listed in ``report.to_install``."""
    ext = Extensions(include=["pub.ext"], reconcile=ReconcilePolicy.ADDITIVE)
    with (
        patch(_LOCAL_RESOLVE, return_value="/usr/bin/code"),
        patch(_LIST, return_value=set()),
        patch(_INSTALL, side_effect=ExtensionInstallFailed("code exited 1")),
    ):
        report = reconcile(ext)
    assert report.to_install == ["pub.ext"]
    assert len(report.failed) == 1
    failed_id, failed_msg = report.failed[0]
    assert failed_id == "pub.ext"
    assert "code exited 1" in failed_msg


def test_additive_casefold_match_skips_install() -> None:
    """A declared id casefold-matching an installed id is not installed and
    is absent from ``to_install`` — the case-insensitive diff invariant."""
    ext = Extensions(include=["github.copilot"], reconcile=ReconcilePolicy.ADDITIVE)
    with (
        patch(_LOCAL_RESOLVE, return_value="/usr/bin/code"),
        patch(_LIST, return_value={"GitHub.copilot"}),
        patch(_INSTALL) as install_one,
    ):
        report = reconcile(ext)
    install_one.assert_not_called()
    assert report.to_install == []


def test_report_policy_surfaces_drift_without_installing() -> None:
    """Under REPORT policy an absent declared id is surfaced in ``to_install``
    but no install subprocess runs (drift preview, not applied)."""
    ext = Extensions(include=["pub.ext"], reconcile=ReconcilePolicy.REPORT)
    with (
        patch(_LOCAL_RESOLVE, return_value="/usr/bin/code"),
        patch(_LIST, return_value=set()),
        patch(_INSTALL) as install_one,
    ):
        report = reconcile(ext)
    install_one.assert_not_called()
    assert report.to_install == ["pub.ext"]


def test_prune_uninstalls_extra_not_declared() -> None:
    """Under PRUNE an installed-but-not-declared id is listed in
    ``to_uninstall`` and the local ``--uninstall-extension`` subprocess runs.

    The PRUNE loop stays local to ``vscode_extensions`` (the driver has no
    removal path), so it is exercised via the module's ``subprocess.run``.
    """
    ext = Extensions(include=["keep.me"], reconcile=ReconcilePolicy.PRUNE)
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    with (
        patch(_LOCAL_RESOLVE, return_value="/usr/bin/code"),
        patch(_LIST, return_value={"keep.me", "extra.one"}),
        patch(_LOCAL_LIST, return_value={"keep.me", "extra.one"}),
        patch(_INSTALL),
        patch(_LOCAL_SUBPROCESS_RUN, side_effect=fake_run),
    ):
        report = reconcile(ext)

    assert report.to_uninstall == ["extra.one"]
    uninstall_args = [c[2] for c in calls if c[1] == "--uninstall-extension"]
    assert uninstall_args == ["extra.one"]


# ---------------------------------------------------------------------------
# plugin reconcile — additive install/enable routed through the driver
# ---------------------------------------------------------------------------
#
# ``claude_plugins.reconcile`` runs the additive install+enable path through
# the driver + ``PluginProvisioner``, while the always-on MARKETPLACE pre-step
# and the PRUNE (disable) loop stay local to ``claude_plugins``. The
# provisioner's ``list_installed`` / ``plugin_install`` / ``plugin_enable``
# seams and the local marketplace / host-local seams are patched so no real
# ``claude`` CLI is invoked.

# PluginProvisioner drives the additive path; patch its seams (the module the
# provisioner imported ``claude_plugins`` under).
_PLUGIN_PROV_LIST = "setforge.provision.plugin.claude_plugins.list_installed"
_PLUGIN_PROV_INSTALL = "setforge.provision.plugin.claude_plugins.plugin_install"
_PLUGIN_PROV_ENABLE = "setforge.provision.plugin.claude_plugins.plugin_enable"
# The always-on marketplace pre-step + the PRUNE disable loop call the local
# ``claude_plugins`` module seams.
_LOCAL_MP_TO_ADD = "setforge.claude_plugins._marketplaces_to_add"
_LOCAL_ADD_MP = "setforge.claude_plugins._add_declared_marketplaces"
_LOCAL_MP_LIST = "setforge.claude_plugins.list_marketplaces"
_LOCAL_MP_ADD = "setforge.claude_plugins.marketplace_add"
_LOCAL_PLUGIN_DISABLE = "setforge.claude_plugins.plugin_disable"


def _one_plugin_cfg_profile(
    policy: ReconcilePolicy = ReconcilePolicy.ADDITIVE,
) -> tuple[Any, Any]:
    """Config+profile declaring a single ``a@m1`` plugin, no marketplaces."""
    cfg = _make_config(claude_plugins={"a": ClaudePluginRef(marketplace="m1")})
    profile = _make_resolved(claude_plugins=["a"], plugins_reconcile=policy)
    return cfg, profile


def test_plugin_additive_install_failure_records_failed() -> None:
    """A HARD ``plugin_install`` failure (driver-routed) must land in
    ``report.failed`` while the failed id is still listed in
    ``report.to_install``."""
    cfg, profile = _one_plugin_cfg_profile()
    with (
        # No marketplaces to add; keep the pre-step a no-op.
        patch(_LOCAL_MP_TO_ADD, return_value=[]),
        patch(_LOCAL_ADD_MP),
        # a@m1 is absent → the driver plans an install.
        patch(_PLUGIN_PROV_LIST, return_value={}),
        patch(
            _PLUGIN_PROV_INSTALL,
            side_effect=subprocess.CalledProcessError(
                1, ["claude", "plugin", "install"], stderr="install bombed"
            ),
        ),
        patch(_PLUGIN_PROV_ENABLE),
    ):
        report = plugin_reconcile(cfg, profile)
    assert report.to_install == [("a", "m1")]
    failed_ids = [pid for pid, _ in report.failed]
    assert "a@m1" in failed_ids
    failed_msg = next(msg for pid, msg in report.failed if pid == "a@m1")
    assert "install bombed" in failed_msg


def test_plugin_marketplace_added_in_non_report_run() -> None:
    """A declared marketplace whose source is absent is listed in
    ``marketplaces_added`` and ``marketplace_add`` is actually called during a
    non-REPORT run — the always-on pre-step survives the rewire."""
    cfg = _make_config(
        marketplaces={
            "anthropic": MarketplaceSource(
                source=MarketplaceSourceKind.GITHUB, repo="anthropics/plugins"
            )
        }
    )
    profile = _make_resolved(plugins_reconcile=ReconcilePolicy.ADDITIVE)
    with (
        # No plugins declared/installed → driver plans nothing.
        patch(_PLUGIN_PROV_LIST, return_value={}),
        patch(_PLUGIN_PROV_INSTALL),
        patch(_PLUGIN_PROV_ENABLE),
        # Marketplace not yet registered → to-add.
        patch(_LOCAL_MP_LIST, return_value={}),
        patch(_LOCAL_MP_ADD) as marketplace_add,
    ):
        report = plugin_reconcile(cfg, profile)
    assert report.marketplaces_added == ["anthropic"]
    marketplace_add.assert_called_once()


def test_plugin_prune_disables_extra_but_additive_does_not() -> None:
    """PRUNE lists an enabled-but-undeclared plugin in ``to_disable`` and
    calls ``plugin_disable``; ADDITIVE leaves ``to_disable`` empty and never
    disables."""
    installed = {
        "a@m1": {"id": "a@m1", "enabled": True},
        "extra@m1": {"id": "extra@m1", "enabled": True},
    }

    # PRUNE: extra@m1 is disabled locally.
    cfg, prune_profile = _one_plugin_cfg_profile(ReconcilePolicy.PRUNE)
    with (
        patch(_LOCAL_MP_TO_ADD, return_value=[]),
        patch(_LOCAL_ADD_MP),
        patch(_PLUGIN_PROV_LIST, return_value=installed),
        patch(_PLUGIN_PROV_INSTALL),
        patch(_PLUGIN_PROV_ENABLE),
        # The PRUNE diff re-reads the live plugin state via the local seam.
        patch("setforge.claude_plugins.list_installed", return_value=installed),
        patch(_LOCAL_PLUGIN_DISABLE) as plugin_disable_prune,
    ):
        prune_report = plugin_reconcile(cfg, prune_profile)
    assert prune_report.to_disable == ["extra@m1"]
    plugin_disable_prune.assert_called_once_with("extra@m1")

    # ADDITIVE: no disable diff, no disable call.
    _, additive_profile = _one_plugin_cfg_profile(ReconcilePolicy.ADDITIVE)
    with (
        patch(_LOCAL_MP_TO_ADD, return_value=[]),
        patch(_LOCAL_ADD_MP),
        patch(_PLUGIN_PROV_LIST, return_value=installed),
        patch(_PLUGIN_PROV_INSTALL),
        patch(_PLUGIN_PROV_ENABLE),
        patch("setforge.claude_plugins.list_installed", return_value=installed),
        patch(_LOCAL_PLUGIN_DISABLE) as plugin_disable_additive,
    ):
        additive_report = plugin_reconcile(cfg, additive_profile)
    assert additive_report.to_disable == []
    plugin_disable_additive.assert_not_called()


def test_plugin_report_policy_lists_diffs_without_subprocess() -> None:
    """Under REPORT no install/enable/disable/marketplace-add subprocess runs,
    yet the report still lists the diffs (drift preview, not applied)."""
    cfg = _make_config(
        claude_plugins={"a": ClaudePluginRef(marketplace="m1")},
        marketplaces={
            "anthropic": MarketplaceSource(
                source=MarketplaceSourceKind.GITHUB, repo="anthropics/plugins"
            )
        },
    )
    profile = _make_resolved(
        claude_plugins=["a"], plugins_reconcile=ReconcilePolicy.REPORT
    )
    installed = {"extra@m1": {"id": "extra@m1", "enabled": True}}
    with (
        patch(_PLUGIN_PROV_LIST, return_value=installed),
        patch(_PLUGIN_PROV_INSTALL) as prov_install,
        patch(_PLUGIN_PROV_ENABLE) as prov_enable,
        patch("setforge.claude_plugins.list_installed", return_value=installed),
        patch(_LOCAL_MP_LIST, return_value={}),
        patch(_LOCAL_MP_ADD) as marketplace_add,
        patch(_LOCAL_PLUGIN_DISABLE) as plugin_disable,
    ):
        report = plugin_reconcile(cfg, profile)
    assert report.dry_run is True
    assert report.to_install == [("a", "m1")]
    assert report.to_disable == ["extra@m1"]
    assert report.marketplaces_added == ["anthropic"]
    prov_install.assert_not_called()
    prov_enable.assert_not_called()
    marketplace_add.assert_not_called()
    plugin_disable.assert_not_called()


def test_plugin_dry_run_lists_diffs_without_subprocess() -> None:
    """``dry_run=True`` suppresses every write subprocess regardless of policy,
    while the report still lists the diffs."""
    cfg, profile = _one_plugin_cfg_profile(ReconcilePolicy.PRUNE)
    installed = {"extra@m1": {"id": "extra@m1", "enabled": True}}
    with (
        patch(_PLUGIN_PROV_LIST, return_value=installed),
        patch(_PLUGIN_PROV_INSTALL) as prov_install,
        patch(_PLUGIN_PROV_ENABLE) as prov_enable,
        patch("setforge.claude_plugins.list_installed", return_value=installed),
        patch(_LOCAL_MP_TO_ADD, return_value=[]),
        patch(_LOCAL_ADD_MP) as add_mp,
        patch(_LOCAL_PLUGIN_DISABLE) as plugin_disable,
    ):
        report = plugin_reconcile(cfg, profile, dry_run=True)
    assert report.dry_run is True
    assert report.to_install == [("a", "m1")]
    assert report.to_disable == ["extra@m1"]
    prov_install.assert_not_called()
    prov_enable.assert_not_called()
    add_mp.assert_not_called()
    plugin_disable.assert_not_called()


# ---------------------------------------------------------------------------
# new-surface package reaches the engine VIA the reconcile adapter
# ---------------------------------------------------------------------------
#
# A plugin / extension declared ONLY as a NEW ``packages`` entry (never the old
# ``claude_plugins`` / ``extensions.include`` field) must still reach the
# unchanged reconcile engine, because each call site now passes
# ``reconcile_adapter.synth_plugin_profile`` / ``.extensions_input`` in place of
# the raw resolved profile. These drive that seam end-to-end.

_TF = {"t": {"src": "t", "dst": "~/t"}}
_MP = {"mp": {"source": "github", "repo": "o/r"}}


def test_plugin_package_only_reaches_engine_via_adapter() -> None:
    """A plugin declared solely as a ``packages`` entry plans its install when
    the call site's ``synth_plugin_profile`` output is fed to the engine."""
    cfg = Config.model_validate(
        {
            "tracked_files": _TF,
            "marketplaces": _MP,
            "profiles": {"p": {"packages": ["sp"]}},
            "claude_plugins": {"sp": {"marketplace": "mp"}},
            "packages": {"sp": {"type": "plugin", "plugin": "sp"}},
        }
    )
    resolved = resolve_profile(cfg, "p")
    # The raw resolved profile has NO old-field plugin — only the package.
    assert resolved.claude_plugins == []
    synth = reconcile_adapter.synth_plugin_profile(cfg, resolved)
    with (
        patch(_LOCAL_MP_TO_ADD, return_value=[]),
        patch(_LOCAL_ADD_MP),
        patch(_PLUGIN_PROV_LIST, return_value={}),
        patch(_PLUGIN_PROV_INSTALL) as prov_install,
        patch(_PLUGIN_PROV_ENABLE),
    ):
        report = plugin_reconcile(cfg, synth)
    # The engine saw the unioned selection and planned the package's install.
    assert report.to_install == [("sp", "mp")]
    prov_install.assert_called_once()


def test_extension_package_only_reaches_engine_via_adapter() -> None:
    """An extension declared solely as a ``packages`` entry plans its install
    when the call site's ``extensions_input`` output is fed to the engine."""
    cfg = Config.model_validate(
        {
            "tracked_files": _TF,
            "profiles": {"p": {"packages": ["cop"]}},
            "packages": {"cop": {"type": "extension", "extension": "pub.ext"}},
        }
    )
    resolved = resolve_profile(cfg, "p")
    # The raw resolved profile has NO old-field extension — only the package.
    assert resolved.extensions.include == []
    ext = reconcile_adapter.extensions_input(cfg, resolved)
    with (
        patch(_LOCAL_RESOLVE, return_value="/usr/bin/code"),
        patch(_LIST, return_value=set()),
        patch(_INSTALL) as install_one,
    ):
        report = reconcile(ext)
    # The engine saw the unioned selection and planned the package's install.
    assert report.to_install == ["pub.ext"]
    install_one.assert_called_once()
