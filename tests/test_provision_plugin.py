"""Tests for the plugin :class:`Provisioner` (``setforge.provision.plugin``).

``claude_plugins`` is patched so no real ``claude`` CLI is invoked. Covers
the present-union / memoized-split probe, fail-open on a missing binary, the
pure 3-state ``plan`` reading memoized state (no subprocess), the empty-delta
second run, the absent-installs-then-enables and disabled-enables-only
``apply_one`` branches, and the HARD failure wrap on an install error.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from setforge.errors import PluginToolMissing, ProvisionItemFailed
from setforge.provision.plugin import PluginProvisioner
from setforge.provision.protocol import Identity, Outcome, ProvisionItem

_LIST = "setforge.provision.plugin.claude_plugins.list_installed"
_PLUGIN_INSTALL = "setforge.provision.plugin.claude_plugins.plugin_install"
_PLUGIN_ENABLE = "setforge.provision.plugin.claude_plugins.plugin_enable"


def _item(pid: str) -> ProvisionItem:
    return ProvisionItem(type="plugin", identity=Identity(key=pid, display=pid))


def _live(enabled: list[str], disabled: list[str]) -> dict[str, dict]:
    return {pid: {"id": pid, "enabled": True} for pid in enabled} | {
        pid: {"id": pid, "enabled": False} for pid in disabled
    }


def test_probe_returns_present_union_and_memoizes_split() -> None:
    prov = PluginProvisioner()
    with patch(_LIST, return_value=_live(["a@mk"], ["b@mk"])):
        got = prov.probe()
    assert {i.key for i in got} == {"a@mk", "b@mk"}
    assert prov._disabled == {Identity(key="b@mk", display="b@mk")}


def test_probe_fails_open_on_missing_binary() -> None:
    prov = PluginProvisioner()
    with patch(_LIST, side_effect=PluginToolMissing("no claude")):
        assert prov.probe() == set()


def test_plan_pure_absent_to_install_disabled_to_activate() -> None:
    prov = PluginProvisioner()
    with patch(_LIST, return_value=_live(["a@mk"], ["b@mk"])):
        installed = prov.probe()
    items = [_item("a@mk"), _item("b@mk"), _item("c@mk")]
    with patch("setforge.provision.plugin.subprocess") as sp, patch(_LIST) as lst:
        delta = prov.plan(items, installed)
    sp.run.assert_not_called()
    lst.assert_not_called()
    assert {i.key for i in delta.installed} == {"c@mk"}
    assert {i.key for i in delta.activated} == {"b@mk"}


def test_plan_idempotent_when_all_enabled() -> None:
    prov = PluginProvisioner()
    with patch(_LIST, return_value=_live(["a@mk"], [])):
        installed = prov.probe()
    assert prov.plan([_item("a@mk")], installed).is_empty()


def test_apply_one_absent_installs_then_enables() -> None:
    prov = PluginProvisioner()
    with patch(_LIST, return_value={}):
        prov.probe()
    with patch(_PLUGIN_INSTALL) as ins, patch(_PLUGIN_ENABLE) as en:
        out = prov.apply_one(_item("c@mk"))
    ins.assert_called_once_with("c", "mk")
    en.assert_called_once_with("c@mk")
    assert out.outcome is Outcome.OK


def test_apply_one_disabled_enables_only() -> None:
    prov = PluginProvisioner()
    with patch(_LIST, return_value=_live([], ["b@mk"])):
        prov.probe()
    with patch(_PLUGIN_INSTALL) as ins, patch(_PLUGIN_ENABLE) as en:
        prov.apply_one(_item("b@mk"))
    ins.assert_not_called()
    en.assert_called_once_with("b@mk")


def test_apply_one_install_failure_is_hard() -> None:
    prov = PluginProvisioner()
    with patch(_LIST, return_value={}):
        prov.probe()
    with (
        patch(_PLUGIN_INSTALL, side_effect=subprocess.CalledProcessError(1, "claude")),
        pytest.raises(ProvisionItemFailed) as excinfo,
    ):
        prov.apply_one(_item("c@mk"))
    assert excinfo.value.kind is Outcome.HARD


def test_apply_one_malformed_id_is_hard_not_valueerror() -> None:
    prov = PluginProvisioner()
    with patch(_LIST, return_value={}):
        prov.probe()
    with pytest.raises(ProvisionItemFailed) as excinfo:
        prov.apply_one(_item("no-at-sign"))
    assert excinfo.value.kind is Outcome.HARD
