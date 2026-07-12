"""Adapter tests: ``vscode_extensions.reconcile`` routed through the driver.

These pin the behavior of the additive install path after it was rewired to
run through :func:`setforge.provision.driver.reconcile` +
:class:`~setforge.provision.extension.ExtensionProvisioner`, while the PRUNE
uninstall loop stays local to ``vscode_extensions``. The provisioner's
``list_installed`` / ``install_one`` / ``uninstall_one`` seams are patched so
no real ``code`` CLI is invoked.
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import patch

from setforge.config import Extensions, ReconcilePolicy
from setforge.errors import ExtensionInstallFailed
from setforge.vscode_extensions import reconcile

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
