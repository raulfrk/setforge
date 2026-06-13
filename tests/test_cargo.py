"""Tests for cargo-binary install orchestration.

``subprocess.run`` and binary resolution are monkeypatched so no real
``cargo`` is invoked. Covers the missing-toolchain soft-warn path, the
skip-if-present path (no ``cargo install`` when the crate is already in
``cargo install --list``), and per-crate failure isolation.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from setforge import cargo


class FakeCargo:
    """Scripted ``cargo`` driver recording argv lists.

    ``installed`` is the set of crate names ``cargo install --list``
    reports. ``install_errors`` maps a crate -> stderr to raise on its
    ``cargo install``.
    """

    def __init__(
        self,
        *,
        installed: set[str] | None = None,
        install_errors: dict[str, str] | None = None,
    ) -> None:
        self.installed = installed or set()
        self.install_errors = install_errors or {}
        self.calls: list[list[str]] = []

    def run(self, argv, **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        # [cargo, install, --list] | [cargo, install, <crate>]
        if argv[1] == "install" and argv[2] == "--list":
            lines = []
            for name in sorted(self.installed):
                lines.append(f"{name} v1.0.0:")
                lines.append(f"    {name}")
            return subprocess.CompletedProcess(argv, 0, stdout="\n".join(lines) + "\n")
        if argv[1] == "install":
            crate = argv[2]
            if crate in self.install_errors:
                raise subprocess.CalledProcessError(
                    1, argv, stderr=self.install_errors[crate]
                )
            self.installed.add(crate)
            return subprocess.CompletedProcess(argv, 0, stdout="")
        raise AssertionError(f"unexpected cargo argv {argv!r}")


@pytest.fixture
def fake_cargo(monkeypatch: pytest.MonkeyPatch) -> Any:
    def _install(*, present: bool = True, **kwargs: Any) -> FakeCargo | None:
        cli = FakeCargo(**kwargs)
        monkeypatch.setattr(
            cargo,
            "resolve_binary",
            lambda _name: Path("/fake/cargo") if present else None,
        )
        monkeypatch.setattr(cargo.subprocess, "run", cli.run)
        return cli if present else None

    return _install


def test_missing_cargo_warns_and_continues(
    fake_cargo, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_cargo(present=False)
    failed = cargo.install_cargo_binaries(["ast-grep"])
    assert failed == []  # soft — not a failure
    err = capsys.readouterr().err
    assert "skipping cargo binaries" in err
    assert "ast-grep" in err


def test_skip_if_present_does_not_invoke_install(fake_cargo) -> None:
    cli = fake_cargo(installed={"ast-grep"})
    failed = cargo.install_cargo_binaries(["ast-grep"])
    assert failed == []
    # `cargo install --list` ran, but no `cargo install ast-grep`.
    install_calls = [c for c in cli.calls if c[1] == "install" and c[2] != "--list"]
    assert install_calls == []


def test_installs_absent_crate(fake_cargo) -> None:
    cli = fake_cargo(installed=set())
    failed = cargo.install_cargo_binaries(["ast-grep"])
    assert failed == []
    assert "ast-grep" in cli.installed
    assert ["/fake/cargo", "install", "ast-grep"] in cli.calls


def test_per_crate_failure_isolated(fake_cargo) -> None:
    cli = fake_cargo(installed=set(), install_errors={"bad": "compile error"})
    failed = cargo.install_cargo_binaries(["bad", "good"])
    assert failed == [("bad", "compile error")]
    assert "good" in cli.installed


def test_install_uses_generous_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _run(argv, **kwargs: Any) -> subprocess.CompletedProcess:
        if argv[1] == "install" and argv[2] == "--list":
            return subprocess.CompletedProcess(argv, 0, stdout="")
        captured["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(argv, 0, stdout="")

    monkeypatch.setattr(cargo, "resolve_binary", lambda _name: Path("/fake/cargo"))
    monkeypatch.setattr(cargo.subprocess, "run", _run)
    cargo.install_cargo_binaries(["slow-crate"])
    assert captured["timeout"] == cargo._INSTALL_TIMEOUT_S
    assert cargo._INSTALL_TIMEOUT_S >= 600  # compiles are minutes
