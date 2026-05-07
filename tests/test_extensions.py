"""Tests for VSCode extension reconcile.

``subprocess.run`` is monkeypatched to a fake driver that records every
call and updates an in-memory installed-extensions set, so tests can
assert on the exact sequence of install/uninstall invocations without
touching a real ``code`` CLI.
"""

import logging
import subprocess

import pytest

from my_setup.config import Extensions, ReconcilePolicy
from my_setup.errors import ExtensionToolMissing
from my_setup.extensions import (
    ReconcileReport,
    list_installed,
    reconcile,
)


class FakeCode:
    """Tracks calls to a faked ``code`` CLI and mutates installed state."""

    def __init__(self, installed: list[str]):
        self.installed: list[str] = list(installed)
        self.calls: list[list[str]] = []

    def run(self, args, **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        if args[1] == "--list-extensions":
            stdout = (
                "\n".join(self.installed) + ("\n" if self.installed else "")
            )
            return subprocess.CompletedProcess(args, 0, stdout, "")
        if args[1] == "--install-extension":
            ext_id = args[2]
            if ext_id not in self.installed:
                self.installed.append(ext_id)
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[1] == "--uninstall-extension":
            ext_id = args[2]
            if ext_id in self.installed:
                self.installed.remove(ext_id)
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected code invocation: {args!r}")

    @property
    def install_args(self) -> list[str]:
        return [c[2] for c in self.calls if c[1] == "--install-extension"]

    @property
    def uninstall_args(self) -> list[str]:
        return [c[2] for c in self.calls if c[1] == "--uninstall-extension"]


@pytest.fixture
def fake_code(monkeypatch: pytest.MonkeyPatch):
    """Default fixture: ``code`` resolves and starts with no installed extensions."""

    def factory(installed: list[str]) -> FakeCode:
        fake = FakeCode(installed)
        monkeypatch.setattr(
            "my_setup.extensions.shutil.which",
            lambda name: "/usr/bin/code" if name == "code" else None,
        )
        monkeypatch.setattr("my_setup.extensions.subprocess.run", fake.run)
        return fake

    return factory


def test_list_installed_parses_lines(fake_code) -> None:
    fake_code(["a.x", "b.y", "c.z"])
    assert list_installed() == {"a.x", "b.y", "c.z"}


def test_list_installed_skips_blank_lines(fake_code) -> None:
    fake = fake_code([])
    fake.installed = ["a.x", "", "b.y", "  "]
    assert list_installed() == {"a.x", "b.y"}


def test_missing_code_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("my_setup.extensions.shutil.which", lambda _: None)
    with pytest.raises(ExtensionToolMissing, match="not found"):
        list_installed()
    with pytest.raises(ExtensionToolMissing, match="not found"):
        reconcile(Extensions(include=["x"]))


def test_additive_fresh_host_installs_declared(fake_code) -> None:
    fake = fake_code([])
    ext = Extensions(
        include=["a.x", "b.y"], reconcile=ReconcilePolicy.ADDITIVE
    )
    report = reconcile(ext)
    assert report.to_install == ["a.x", "b.y"]
    assert report.to_uninstall == []
    assert sorted(fake.install_args) == ["a.x", "b.y"]
    assert fake.uninstall_args == []


def test_additive_leaves_extras_untouched(fake_code) -> None:
    fake = fake_code(["a.x", "extra.one", "extra.two"])
    ext = Extensions(
        include=["a.x", "new.one"], reconcile=ReconcilePolicy.ADDITIVE
    )
    report = reconcile(ext)
    assert report.to_install == ["new.one"]
    assert report.to_uninstall == []
    assert fake.install_args == ["new.one"]
    assert fake.uninstall_args == []


def test_prune_removes_extras_and_excluded(fake_code) -> None:
    fake = fake_code(["a.x", "b.y", "extra.one", "github.copilot-chat"])
    ext = Extensions(
        include=["a.x", "b.y"],
        exclude=["github.copilot-chat"],
        reconcile=ReconcilePolicy.PRUNE,
    )
    report = reconcile(ext)
    assert report.to_install == []
    assert sorted(report.to_uninstall) == ["extra.one", "github.copilot-chat"]
    assert fake.install_args == []
    assert sorted(fake.uninstall_args) == ["extra.one", "github.copilot-chat"]


def test_prune_installs_missing_and_uninstalls_extras(fake_code) -> None:
    fake = fake_code(["existing.one", "extra.one"])
    ext = Extensions(
        include=["existing.one", "new.one"],
        reconcile=ReconcilePolicy.PRUNE,
    )
    report = reconcile(ext)
    assert report.to_install == ["new.one"]
    assert report.to_uninstall == ["extra.one"]
    assert fake.install_args == ["new.one"]
    assert fake.uninstall_args == ["extra.one"]


def test_report_computes_diffs_without_acting(fake_code) -> None:
    fake = fake_code(["a.x", "extra.one"])
    ext = Extensions(
        include=["a.x", "b.y"], reconcile=ReconcilePolicy.REPORT
    )
    report = reconcile(ext)
    assert report.to_install == ["b.y"]
    assert report.to_uninstall == ["extra.one"]
    assert bool(report) is True
    # Only --list-extensions runs; no install/uninstall.
    assert fake.install_args == []
    assert fake.uninstall_args == []


def test_dry_run_runs_no_install_or_uninstall(
    fake_code, caplog: pytest.LogCaptureFixture
) -> None:
    fake = fake_code(["existing.one"])
    ext = Extensions(
        include=["existing.one", "new.one"],
        reconcile=ReconcilePolicy.PRUNE,
    )
    with caplog.at_level(logging.INFO, logger="my_setup.extensions"):
        report = reconcile(ext, dry_run=True)
    assert report.dry_run is True
    assert report.to_install == ["new.one"]
    assert fake.install_args == []
    assert fake.uninstall_args == []
    assert any("would install" in rec.message for rec in caplog.records)


def test_exclude_overrides_include(fake_code) -> None:
    fake_code([])
    ext = Extensions(
        include=["keep.me", "drop.me"],
        exclude=["drop.me"],
        reconcile=ReconcilePolicy.ADDITIVE,
    )
    report = reconcile(ext)
    assert report.to_install == ["keep.me"]


def test_clean_state_returns_falsy_report(fake_code) -> None:
    fake_code(["a.x", "b.y"])
    ext = Extensions(
        include=["a.x", "b.y"], reconcile=ReconcilePolicy.PRUNE
    )
    report = reconcile(ext)
    assert isinstance(report, ReconcileReport)
    assert bool(report) is False
