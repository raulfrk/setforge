"""Tests for the cargo :class:`Provisioner` (``setforge.provision.cargo``).

``subprocess.run`` and binary resolution are monkeypatched so no real
``cargo`` is invoked — mirroring :mod:`tests.test_cargo`'s ``FakeCargo``
pattern. Covers the two-tier ``cargo install --list`` parse, fail-open
probe, pure ``plan``, the OK / SKIP / SOFT ``apply_one`` outcomes (build
failure is SOFT, never HARD), the ``--`` separator guard, the generous
install timeout, the empty-delta second run (INV-7), and uninstall.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from setforge.provision import cargo as prov_cargo
from setforge.provision.protocol import (
    Identity,
    Outcome,
    ProvisionItem,
)


class FakeCargo:
    """Scripted ``cargo`` driver recording argv lists.

    ``installed`` is the set of crate names ``cargo install --list``
    reports. ``install_errors`` maps a crate -> stderr to raise on its
    ``cargo install``. ``list_error`` (when set) makes the ``--list`` probe
    raise, exercising the fail-open path.
    """

    def __init__(
        self,
        *,
        installed: set[str] | None = None,
        install_errors: dict[str, str] | None = None,
        list_error: Exception | None = None,
    ) -> None:
        self.installed = installed or set()
        self.install_errors = install_errors or {}
        self.list_error = list_error
        self.calls: list[list[str]] = []
        self.timeouts: list[object] = []

    def run(self, argv, **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        self.timeouts.append(kwargs.get("timeout"))
        # [cargo, install, --list] | [cargo, install, --, <crate>]
        #   | [cargo, uninstall, --, <crate>]
        if argv[1] == "install" and argv[2] == "--list":
            if self.list_error is not None:
                raise self.list_error
            lines = []
            for name in sorted(self.installed):
                lines.append(f"{name} v1.0.0:")
                lines.append(f"    {name}")
            return subprocess.CompletedProcess(argv, 0, stdout="\n".join(lines) + "\n")
        if argv[1] == "install":
            assert argv[2] == "--", f"expected literal -- separator, got {argv[2]!r}"
            crate = argv[3]
            if crate in self.install_errors:
                raise subprocess.CalledProcessError(
                    1, argv, stderr=self.install_errors[crate]
                )
            self.installed.add(crate)
            return subprocess.CompletedProcess(argv, 0, stdout="")
        if argv[1] == "uninstall":
            assert argv[2] == "--", f"expected literal -- separator, got {argv[2]!r}"
            self.installed.discard(argv[3])
            return subprocess.CompletedProcess(argv, 0, stdout="")
        raise AssertionError(f"unexpected cargo argv {argv!r}")


@pytest.fixture
def fake_cargo(monkeypatch: pytest.MonkeyPatch) -> Any:
    def _install(*, present: bool = True, **kwargs: Any) -> FakeCargo | None:
        cli = FakeCargo(**kwargs)
        monkeypatch.setattr(
            prov_cargo,
            "resolve_binary",
            lambda _name: Path("/fake/cargo") if present else None,
        )
        monkeypatch.setattr(prov_cargo.subprocess, "run", cli.run)
        return cli if present else None

    return _install


def _item(crate: str) -> ProvisionItem:
    """Build a cargo :class:`ProvisionItem` for ``crate`` (key=display=crate)."""
    return ProvisionItem(type="cargo", identity=Identity(key=crate, display=crate))


def _install_calls(cli: FakeCargo) -> list[list[str]]:
    return [c for c in cli.calls if c[1] == "install" and c[2] != "--list"]


# --- probe -----------------------------------------------------------------


def test_probe_parses_two_tier_list(fake_cargo) -> None:
    fake_cargo(installed={"ast-grep", "ripgrep"})
    installed = prov_cargo.CargoProvisioner().probe()
    assert installed == {
        Identity(key="ast-grep", display="ast-grep"),
        Identity(key="ripgrep", display="ripgrep"),
    }


def test_probe_fails_open_when_cargo_missing(fake_cargo) -> None:
    fake_cargo(present=False)
    assert prov_cargo.CargoProvisioner().probe() == set()


def test_probe_fails_open_on_list_error(fake_cargo) -> None:
    fake_cargo(
        installed={"ast-grep"},
        list_error=subprocess.CalledProcessError(1, ["cargo"], stderr="boom"),
    )
    # Fail OPEN — assume nothing installed, never fail-closed/assume-all.
    assert prov_cargo.CargoProvisioner().probe() == set()


def test_probe_uses_list_timeout(fake_cargo) -> None:
    cli = fake_cargo(installed=set())
    prov_cargo.CargoProvisioner().probe()
    assert cli.timeouts == [prov_cargo._LIST_TIMEOUT_S]
    assert prov_cargo._LIST_TIMEOUT_S == 30


def test_probe_fails_open_on_os_error(fake_cargo) -> None:
    fake_cargo(installed={"ast-grep"}, list_error=OSError("cargo binary vanished"))
    assert prov_cargo.CargoProvisioner().probe() == set()


# --- plan (PURE) -----------------------------------------------------------


def test_plan_excludes_present_and_is_pure(fake_cargo) -> None:
    cli = fake_cargo(installed={"ast-grep"})
    prov = prov_cargo.CargoProvisioner()
    items = [_item("ast-grep"), _item("ripgrep")]
    installed = {Identity(key="ast-grep", display="ast-grep")}
    delta = prov.plan(items, installed)
    assert delta.installed == (Identity(key="ripgrep", display="ripgrep"),)
    # PURE — plan touched no subprocess.
    assert cli.calls == []


# --- apply_one -------------------------------------------------------------


def test_apply_installs_absent_crate(fake_cargo) -> None:
    cli = fake_cargo(installed=set())
    outcome = prov_cargo.CargoProvisioner().apply_one(_item("ast-grep"))
    assert outcome.outcome is Outcome.OK
    assert "ast-grep" in cli.installed
    assert ["/fake/cargo", "install", "--", "ast-grep"] in cli.calls


def test_apply_skips_present_crate_without_install(fake_cargo) -> None:
    cli = fake_cargo(installed={"ast-grep"})
    outcome = prov_cargo.CargoProvisioner().apply_one(_item("ast-grep"))
    assert outcome.outcome is Outcome.SKIP
    # SKIP before any install subprocess — no needless recompile.
    assert _install_calls(cli) == []


def test_apply_soft_when_cargo_missing(fake_cargo) -> None:
    fake_cargo(present=False)
    outcome = prov_cargo.CargoProvisioner().apply_one(_item("ast-grep"))
    assert outcome.outcome is Outcome.SOFT  # warn-and-skip, NEVER HARD
    assert "cargo" in outcome.detail.lower()


def test_apply_soft_on_build_failure_with_stderr_detail(fake_cargo) -> None:
    fake_cargo(installed=set(), install_errors={"bad": "compile error boom"})
    outcome = prov_cargo.CargoProvisioner().apply_one(_item("bad"))
    # Build failure is SOFT and RECORDED, never HARD, never discarded.
    assert outcome.outcome is Outcome.SOFT
    assert "compile error boom" in outcome.detail


def test_apply_soft_on_os_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(argv, **kwargs: Any) -> subprocess.CompletedProcess:
        raise OSError("cargo binary vanished")

    monkeypatch.setattr(prov_cargo, "resolve_binary", lambda _n: Path("/fake/cargo"))
    monkeypatch.setattr(prov_cargo.subprocess, "run", _raise)
    outcome = prov_cargo.CargoProvisioner().apply_one(_item("ast-grep"))
    assert outcome.outcome is Outcome.SOFT
    assert "cargo binary vanished" in outcome.detail


def test_apply_double_dash_before_dash_crate(fake_cargo) -> None:
    cli = fake_cargo(installed=set())
    prov_cargo.CargoProvisioner().apply_one(_item("--evil"))
    call = _install_calls(cli)[0]
    assert call == ["/fake/cargo", "install", "--", "--evil"]
    assert call.index("--") < call.index("--evil")


def test_apply_uses_generous_install_timeout(fake_cargo) -> None:
    cli = fake_cargo(installed=set())
    prov_cargo.CargoProvisioner().apply_one(_item("slow-crate"))
    install_timeout = cli.timeouts[cli.calls.index(_install_calls(cli)[0])]
    assert install_timeout == prov_cargo._INSTALL_TIMEOUT_S
    assert prov_cargo._INSTALL_TIMEOUT_S >= 600


def test_second_run_yields_empty_delta(fake_cargo) -> None:
    # INV-7: after installing, a re-plan against the fresh probe is empty.
    fake_cargo(installed=set())
    prov = prov_cargo.CargoProvisioner()
    item = _item("ast-grep")
    assert prov.apply_one(item).outcome is Outcome.OK
    delta = prov.plan([item], prov.probe())
    assert delta.is_empty()


# --- uninstall -------------------------------------------------------------


def test_uninstall_calls_cargo_uninstall(fake_cargo) -> None:
    cli = fake_cargo(installed={"ast-grep"})
    prov_cargo.CargoProvisioner().uninstall_one(
        Identity(key="ast-grep", display="ast-grep")
    )
    assert ["/fake/cargo", "uninstall", "--", "ast-grep"] in cli.calls
    assert "ast-grep" not in cli.installed


def test_uninstall_tolerates_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    # cargo uninstall of an absent crate exits non-zero; must be swallowed.
    def _raise(argv, **kwargs: Any) -> subprocess.CompletedProcess:
        raise subprocess.CalledProcessError(1, argv, stderr="not installed")

    monkeypatch.setattr(prov_cargo, "resolve_binary", lambda _n: Path("/fake/cargo"))
    monkeypatch.setattr(prov_cargo.subprocess, "run", _raise)
    # Should not raise.
    prov_cargo.CargoProvisioner().uninstall_one(
        Identity(key="absent", display="absent")
    )


def test_uninstall_tolerates_os_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(argv, **kwargs: Any) -> subprocess.CompletedProcess:
        raise OSError("cargo binary vanished")

    monkeypatch.setattr(prov_cargo, "resolve_binary", lambda _n: Path("/fake/cargo"))
    monkeypatch.setattr(prov_cargo.subprocess, "run", _raise)
    prov_cargo.CargoProvisioner().uninstall_one(
        Identity(key="ast-grep", display="ast-grep")
    )


def test_uninstall_noop_when_cargo_missing(fake_cargo) -> None:
    fake_cargo(present=False)
    # No cargo -> nothing to uninstall, must not raise.
    prov_cargo.CargoProvisioner().uninstall_one(
        Identity(key="ast-grep", display="ast-grep")
    )
