from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from setforge.provision import python as prov_python
from setforge.provision.driver import reconcile
from setforge.provision.protocol import (
    Identity,
    Outcome,
    ProvisionItem,
)


class FakeUv:
    """Scripted ``uv`` driver recording argv lists."""

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
        if argv[1] == "tool" and argv[2] == "list":
            if self.list_error is not None:
                raise self.list_error
            lines = []
            for name in sorted(self.installed):
                lines.append(f"\x1b[1m{name} v1.0.0\x1b[0m")
                lines.append(f"- {name}")
            return subprocess.CompletedProcess(argv, 0, stdout="\n".join(lines) + "\n")
        if argv[1] == "tool" and argv[2] == "install":
            assert argv[3] == "--", f"expected literal -- separator, got {argv[3]!r}"
            package = argv[4]
            name = package.split("==", 1)[0]
            if name in self.install_errors:
                raise subprocess.CalledProcessError(
                    1, argv, stderr=self.install_errors[name]
                )
            self.installed.add(name)
            return subprocess.CompletedProcess(argv, 0, stdout="")
        if argv[1] == "tool" and argv[2] == "uninstall":
            assert argv[3] == "--", f"expected literal -- separator, got {argv[3]!r}"
            self.installed.discard(argv[4])
            return subprocess.CompletedProcess(argv, 0, stdout="")
        raise AssertionError(f"unexpected uv argv {argv!r}")


@pytest.fixture
def fake_uv(monkeypatch: pytest.MonkeyPatch) -> Any:
    def _install(*, present: bool = True, **kwargs: Any) -> FakeUv | None:
        cli = FakeUv(**kwargs)
        monkeypatch.setattr(
            prov_python,
            "resolve_binary",
            lambda _name: Path("/fake/uv") if present else None,
        )
        monkeypatch.setattr(prov_python.subprocess, "run", cli.run)
        return cli if present else None

    return _install


def _item(package: str, *, version: str | None = None) -> ProvisionItem:
    return ProvisionItem(
        type="python",
        identity=Identity(key=package, display=package),
        version=version,
    )


def _install_calls(cli: FakeUv) -> list[list[str]]:
    return [c for c in cli.calls if c[1] == "tool" and c[2] == "install"]


def test_probe_parses_two_tier_list(fake_uv) -> None:
    fake_uv(installed={"ruff", "pre-commit"})
    installed = prov_python.PythonProvisioner().probe()
    assert installed == {
        Identity(key="ruff", display="ruff"),
        Identity(key="pre-commit", display="pre-commit"),
    }


def test_probe_no_prefix_false_match(fake_uv) -> None:
    fake_uv(installed={"foobar"})
    installed = prov_python.PythonProvisioner().probe()
    assert installed == {Identity(key="foobar", display="foobar")}
    assert Identity(key="foo", display="foo") not in installed


def test_probe_skips_entrypoint_lines(fake_uv) -> None:
    fake_uv(installed={"ruff"})
    installed = prov_python.PythonProvisioner().probe()
    assert installed == {Identity(key="ruff", display="ruff")}


def test_probe_fails_open_when_uv_missing(fake_uv) -> None:
    fake_uv(present=False)
    assert prov_python.PythonProvisioner().probe() == set()


def test_probe_fails_open_on_list_error(fake_uv) -> None:
    fake_uv(
        installed={"ruff"},
        list_error=subprocess.CalledProcessError(1, ["uv"], stderr="boom"),
    )
    assert prov_python.PythonProvisioner().probe() == set()


def test_probe_uses_list_timeout(fake_uv) -> None:
    cli = fake_uv(installed=set())
    prov_python.PythonProvisioner().probe()
    assert cli.timeouts == [prov_python._LIST_TIMEOUT_S]
    assert prov_python._LIST_TIMEOUT_S == 30


def test_plan_excludes_present_and_is_pure(fake_uv) -> None:
    cli = fake_uv(installed={"ruff"})
    prov = prov_python.PythonProvisioner()
    items = [_item("ruff"), _item("pre-commit")]
    installed = {Identity(key="ruff", display="ruff")}
    delta = prov.plan(items, installed)
    assert delta.installed == (Identity(key="pre-commit", display="pre-commit"),)
    assert cli.calls == []


def test_apply_installs_absent_package(fake_uv) -> None:
    cli = fake_uv(installed=set())
    outcome = prov_python.PythonProvisioner().apply_one(_item("ruff"))
    assert outcome.outcome is Outcome.OK
    assert "ruff" in cli.installed
    assert ["/fake/uv", "tool", "install", "--", "ruff"] in cli.calls


def test_apply_skips_present_package_without_install(fake_uv) -> None:
    cli = fake_uv(installed={"ruff"})
    outcome = prov_python.PythonProvisioner().apply_one(_item("ruff"))
    assert outcome.outcome is Outcome.SKIP
    assert _install_calls(cli) == []


def test_apply_soft_when_uv_missing(fake_uv) -> None:
    fake_uv(present=False)
    outcome = prov_python.PythonProvisioner().apply_one(_item("ruff"))
    assert outcome.outcome is Outcome.SOFT
    assert "uv" in outcome.detail.lower()


def test_apply_soft_on_install_failure_with_stderr_detail(fake_uv) -> None:
    fake_uv(installed=set(), install_errors={"bad": "resolution error boom"})
    outcome = prov_python.PythonProvisioner().apply_one(_item("bad"))
    assert outcome.outcome is Outcome.SOFT
    assert "resolution error boom" in outcome.detail


def test_apply_never_emits_hard(fake_uv) -> None:
    fake_uv(installed=set(), install_errors={"bad": "boom"})
    outcome = prov_python.PythonProvisioner().apply_one(_item("bad"))
    assert outcome.outcome is not Outcome.HARD


def test_apply_double_dash_before_dash_package(fake_uv) -> None:
    cli = fake_uv(installed=set())
    prov_python.PythonProvisioner().apply_one(_item("--evil"))
    call = _install_calls(cli)[0]
    assert call == ["/fake/uv", "tool", "install", "--", "--evil"]
    assert call.index("--") < call.index("--evil")


def test_apply_uses_generous_install_timeout(fake_uv) -> None:
    cli = fake_uv(installed=set())
    prov_python.PythonProvisioner().apply_one(_item("slow-pkg"))
    install_timeout = cli.timeouts[cli.calls.index(_install_calls(cli)[0])]
    assert install_timeout == prov_python._INSTALL_TIMEOUT_S
    assert prov_python._INSTALL_TIMEOUT_S >= 600


def test_apply_passes_version_pin_on_initial_install(fake_uv) -> None:
    cli = fake_uv(installed=set())
    outcome = prov_python.PythonProvisioner().apply_one(_item("ruff", version="0.5.0"))
    assert outcome.outcome is Outcome.OK
    assert ["/fake/uv", "tool", "install", "--", "ruff==0.5.0"] in cli.calls


def test_apply_skips_present_even_on_version_mismatch(fake_uv) -> None:
    cli = fake_uv(installed={"ruff"})
    outcome = prov_python.PythonProvisioner().apply_one(_item("ruff", version="99.0.0"))
    assert outcome.outcome is Outcome.SKIP
    assert _install_calls(cli) == []


def test_second_run_yields_empty_delta(fake_uv) -> None:
    fake_uv(installed=set())
    prov = prov_python.PythonProvisioner()
    item = _item("ruff")
    assert prov.apply_one(item).outcome is Outcome.OK
    delta = prov.plan([item], prov.probe())
    assert delta.is_empty()


def test_uninstall_calls_uv_uninstall(fake_uv) -> None:
    cli = fake_uv(installed={"ruff"})
    prov_python.PythonProvisioner().uninstall_one(Identity(key="ruff", display="ruff"))
    assert ["/fake/uv", "tool", "uninstall", "--", "ruff"] in cli.calls
    assert "ruff" not in cli.installed


def test_uninstall_tolerates_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(argv, **kwargs: Any) -> subprocess.CompletedProcess:
        raise subprocess.CalledProcessError(1, argv, stderr="not installed")

    monkeypatch.setattr(prov_python, "resolve_binary", lambda _n: Path("/fake/uv"))
    monkeypatch.setattr(prov_python.subprocess, "run", _raise)
    prov_python.PythonProvisioner().uninstall_one(
        Identity(key="absent", display="absent")
    )


def test_uninstall_noop_when_uv_missing(fake_uv) -> None:
    fake_uv(present=False)
    prov_python.PythonProvisioner().uninstall_one(Identity(key="ruff", display="ruff"))


def test_reconcile_installs_and_is_idempotent(fake_uv) -> None:
    from setforge.config import ReconcilePolicy

    cli = fake_uv(installed=set())
    prov = prov_python.PythonProvisioner()
    items = [_item("ruff"), _item("pre-commit")]
    first = reconcile(prov, items, policy=ReconcilePolicy.ADDITIVE)
    assert {o.outcome for o in first.outcomes} == {Outcome.OK}
    assert cli.installed == {"ruff", "pre-commit"}
    calls_before = len(_install_calls(cli))
    second = reconcile(prov, items, policy=ReconcilePolicy.ADDITIVE)
    assert second.delta.is_empty()
    assert len(_install_calls(cli)) == calls_before


def test_reconcile_report_only_writes_nothing(fake_uv) -> None:
    from setforge.config import ReconcilePolicy

    cli = fake_uv(installed=set())
    prov = prov_python.PythonProvisioner()
    items = [_item("ruff")]
    result = reconcile(prov, items, policy=ReconcilePolicy.ADDITIVE, report_only=True)
    assert result.reported is True
    assert result.delta.installed == (Identity(key="ruff", display="ruff"),)
    assert _install_calls(cli) == []
    assert cli.installed == set()


def test_reconcile_partial_failure_is_isolated(fake_uv) -> None:
    from setforge.config import ReconcilePolicy

    cli = fake_uv(installed=set(), install_errors={"bad": "boom"})
    prov = prov_python.PythonProvisioner()
    items = [_item("good"), _item("bad")]
    result = reconcile(prov, items, policy=ReconcilePolicy.ADDITIVE)
    by_name = {o.item.identity.key: o.outcome for o in result.outcomes}
    assert by_name["good"] is Outcome.OK
    assert by_name["bad"] is Outcome.SOFT
    assert "good" in cli.installed
