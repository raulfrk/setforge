"""Regression tests for the extension-ID regex / case-handling audit fix.

`code --list-extensions` echoes IDs with their publisher's original
casing (e.g. `GitHub.copilot`). The old lowercase-only `_EXT_ID_RE`
silently dropped any uppercase-bearing ID from `list_installed()`,
which made reconcile re-install such an extension on every run because
it never appeared in the installed set. These tests assert that:

1. uppercase-publisher IDs survive `list_installed()`, and
2. reconcile is idempotent for an already-installed uppercase ID (and
   case-insensitive when the declared casing differs from the live one).

The fake-``code`` harness mirrors the one in test_vscode_extensions.py.
"""

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from setforge.config import Extensions, ReconcilePolicy
from setforge.vscode_extensions import list_installed, reconcile


class FakeCode:
    """Tracks calls to a faked ``code`` CLI and mutates installed state."""

    def __init__(self, installed: list[str]) -> None:
        self.installed: list[str] = list(installed)
        self.calls: list[list[str]] = []

    def run(self, args, **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        if args[1] == "--list-extensions":
            stdout = "\n".join(self.installed) + ("\n" if self.installed else "")
            return subprocess.CompletedProcess(args, 0, stdout, "")
        if args[1] == "--install-extension":
            if args[2] not in self.installed:
                self.installed.append(args[2])
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[1] == "--uninstall-extension":
            if args[2] in self.installed:
                self.installed.remove(args[2])
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected code invocation: {args!r}")

    @property
    def install_args(self) -> list[str]:
        return [c[2] for c in self.calls if c[1] == "--install-extension"]

    @property
    def uninstall_args(self) -> list[str]:
        return [c[2] for c in self.calls if c[1] == "--uninstall-extension"]


@pytest.fixture
def fake_code(monkeypatch: pytest.MonkeyPatch) -> Callable[[list[str]], FakeCode]:
    def factory(installed: list[str]) -> FakeCode:
        fake = FakeCode(installed)
        monkeypatch.setattr(
            "setforge.vscode_extensions.resolve_binary",
            lambda name: Path("/usr/bin/code") if name == "code" else None,
        )
        monkeypatch.setattr("setforge.vscode_extensions.subprocess.run", fake.run)
        return fake

    return factory


def test_list_installed_keeps_uppercase_publisher_ids(fake_code) -> None:
    """Real IDs carry uppercase letters; they must not be dropped."""
    fake_code(
        [
            "GitHub.copilot",
            "VisualStudioExptTeam.vscodeintellicode",
            "ms-vscode.PowerShell",
            "ms-python.python",
        ]
    )
    assert list_installed() == {
        "GitHub.copilot",
        "VisualStudioExptTeam.vscodeintellicode",
        "ms-vscode.PowerShell",
        "ms-python.python",
    }


def test_list_installed_still_drops_ssh_header(fake_code) -> None:
    """Case-insensitive regex must still reject the Remote-SSH header line."""
    fake = fake_code([])
    fake.installed = [
        "Extensions installed on SSH: 1.2.3.4:",
        "GitHub.copilot",
    ]
    assert list_installed() == {"GitHub.copilot"}


def test_reconcile_idempotent_for_installed_uppercase_id(fake_code) -> None:
    """An already-installed uppercase-publisher extension is never reinstalled."""
    fake = fake_code(["GitHub.copilot", "ms-python.python"])
    ext = Extensions(include=["GitHub.copilot"], reconcile=ReconcilePolicy.ADDITIVE)
    report = reconcile(ext)
    assert report.to_install == []
    assert fake.install_args == []


def test_reconcile_case_insensitive_match_no_churn(fake_code) -> None:
    """Declared casing differing from live casing must not churn under PRUNE."""
    fake = fake_code(["GitHub.copilot"])
    ext = Extensions(include=["github.copilot"], reconcile=ReconcilePolicy.PRUNE)
    report = reconcile(ext)
    assert report.to_install == []
    assert report.to_uninstall == []
    assert fake.install_args == []
    assert fake.uninstall_args == []
