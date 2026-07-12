"""Tests for the extension :class:`Provisioner` (``setforge.provision.extension``).

``vscode_extensions`` is patched so no real ``code`` CLI is invoked. Covers
the casefold-key / display-preserve probe, fail-open on a missing binary
or a failed list command,
the pure additive ``plan`` (no subprocess), the empty-delta second run, the
original-casing ``apply_one``, and the HARD failure wrap.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from setforge.errors import (
    ExtensionInstallFailed,
    ExtensionToolMissing,
    ProvisionItemFailed,
)
from setforge.provision.extension import ExtensionProvisioner
from setforge.provision.protocol import Identity, Outcome, ProvisionItem

_LIST = "setforge.provision.extension.vscode_extensions.list_installed"
_INSTALL = "setforge.provision.extension.vscode_extensions.install_one"


def _item(ext_id: str) -> ProvisionItem:
    return ProvisionItem(
        type="extension", identity=Identity(key=ext_id.casefold(), display=ext_id)
    )


def test_probe_casefolds_key_keeps_display() -> None:
    with patch(_LIST, return_value={"GitHub.copilot"}):
        got = ExtensionProvisioner().probe()
    ident = next(iter(got))
    assert ident.key == "github.copilot"
    assert ident.display == "GitHub.copilot"


def test_probe_fails_open_on_missing_binary() -> None:
    with patch(_LIST, side_effect=ExtensionToolMissing("no code")):
        assert ExtensionProvisioner().probe() == set()


def test_probe_fails_open_on_list_failure() -> None:
    with patch(_LIST, side_effect=ExtensionInstallFailed("list failed")):
        assert ExtensionProvisioner().probe() == set()


def test_plan_is_pure_and_additive() -> None:
    prov = ExtensionProvisioner()
    installed = {Identity(key="github.copilot", display="GitHub.copilot")}
    items = [_item("GitHub.copilot"), _item("ms-python.python")]
    with patch("setforge.provision.extension.subprocess") as sp, patch(_LIST) as lst:
        delta = prov.plan(items, installed)
    sp.run.assert_not_called()
    lst.assert_not_called()
    assert {i.key for i in delta.installed} == {"ms-python.python"}
    assert delta.activated == ()


def test_plan_idempotent_second_run_empty() -> None:
    prov = ExtensionProvisioner()
    installed = {Identity(key="ms-python.python", display="ms-python.python")}
    assert prov.plan([_item("ms-python.python")], installed).is_empty()


def test_apply_one_installs_with_original_casing() -> None:
    prov = ExtensionProvisioner()
    with patch(_INSTALL) as ins:
        out = prov.apply_one(_item("GitHub.copilot"))
    ins.assert_called_once_with("GitHub.copilot")
    assert out.outcome is Outcome.OK


def test_apply_one_failure_is_hard() -> None:
    prov = ExtensionProvisioner()
    with (
        patch(_INSTALL, side_effect=ExtensionInstallFailed("boom")),
        pytest.raises(ProvisionItemFailed) as excinfo,
    ):
        prov.apply_one(_item("ms-python.python"))
    assert excinfo.value.kind is Outcome.HARD
