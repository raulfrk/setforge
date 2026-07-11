"""Tests for the go :class:`Provisioner` (``setforge.provision.go``)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from setforge.provision import go as prov_go
from setforge.provision.protocol import (
    Identity,
    Outcome,
    ProvisionItem,
)
from setforge.provision.receipt import ReceiptStore


class FakeGo:
    def __init__(
        self,
        *,
        env: dict[str, str] | None = None,
        install_errors: dict[str, str] | None = None,
        gobin_dir: Path | None = None,
    ) -> None:
        self.env = env or {}
        self.install_errors = install_errors or {}
        self.gobin_dir = gobin_dir
        self.calls: list[list[str]] = []
        self.timeouts: list[object] = []

    def run(self, argv, **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        self.timeouts.append(kwargs.get("timeout"))
        if argv[1] == "env":
            return subprocess.CompletedProcess(
                argv, 0, stdout=self.env.get(argv[2], "") + "\n"
            )
        if argv[1] == "install":
            assert argv[2] == "--", f"expected literal -- separator, got {argv[2]!r}"
            spec = argv[3]
            if spec in self.install_errors:
                raise subprocess.CalledProcessError(
                    1, argv, stderr=self.install_errors[spec]
                )
            if self.gobin_dir is not None:
                module = spec.rsplit("@", 1)[0]
                name = prov_go._binary_name(module)
                self.gobin_dir.mkdir(parents=True, exist_ok=True)
                (self.gobin_dir / name).write_text("", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, stdout="")
        raise AssertionError(f"unexpected go argv {argv!r}")


@pytest.fixture
def fake_go(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    def _install(
        *, present: bool = True, env: dict[str, str] | None = None, **kwargs: Any
    ) -> tuple[Any, Any, ReceiptStore, Path]:
        gobin_dir = tmp_path / "gobin"
        if env is None:
            env = {"GOBIN": str(gobin_dir), "GOPATH": ""}
        store = ReceiptStore(tmp_path / "receipts")
        cli = FakeGo(env=env, gobin_dir=gobin_dir, **kwargs) if present else None
        monkeypatch.setattr(
            prov_go,
            "resolve_binary",
            lambda _name: Path("/fake/go") if present else None,
        )
        if cli is not None:
            monkeypatch.setattr(prov_go.subprocess, "run", cli.run)
        prov = prov_go.GoProvisioner(receipts=store)
        return prov, cli, store, gobin_dir

    return _install


def _item(module: str, version: str | None = None) -> ProvisionItem:
    return ProvisionItem(
        type="go",
        identity=Identity(key=module, display=module),
        version=version,
    )


def _install_calls(cli: FakeGo) -> list[list[str]]:
    return [c for c in cli.calls if c[1] == "install"]


def test_binary_name_plain_last_segment() -> None:
    assert prov_go._binary_name("github.com/owner/tool") == "tool"


def test_binary_name_strips_vN_major_suffix() -> None:
    assert prov_go._binary_name("github.com/owner/mod/v2") == "mod"


def test_binary_name_honors_cmd_leaf() -> None:
    assert prov_go._binary_name("github.com/owner/proj/cmd/foo") == "foo"


def test_binary_name_cmd_leaf_with_vN() -> None:
    assert prov_go._binary_name("github.com/owner/proj/v3/cmd/foo") == "foo"


def test_gobin_prefers_go_env_gobin(fake_go) -> None:
    prov, _cli, _store, _gobin = fake_go(
        env={"GOBIN": "/explicit/gobin", "GOPATH": "/home/u/go"}
    )
    assert prov._gobin_dir() == Path("/explicit/gobin")


def test_gobin_falls_back_to_gopath_first_entry(fake_go) -> None:
    import os

    gopath = os.pathsep.join(["/first/go", "/second/go"])
    prov, _cli, _store, _gobin = fake_go(env={"GOBIN": "", "GOPATH": gopath})
    assert prov._gobin_dir() == Path("/first/go/bin")


def test_gobin_final_fallback_home_go_bin(fake_go) -> None:
    prov, _cli, _store, _gobin = fake_go(env={"GOBIN": "", "GOPATH": ""})
    assert prov._gobin_dir() == Path.home() / "go" / "bin"


def test_probe_requires_both_receipt_and_binary(fake_go) -> None:
    prov, _cli, store, gobin = fake_go()
    ident = Identity(key="github.com/o/tool", display="github.com/o/tool")
    store.record(ident, version="1", checksum=None, path=str(gobin / "tool"))
    assert prov.probe() == set()
    gobin.mkdir(parents=True, exist_ok=True)
    (gobin / "tool").write_text("", encoding="utf-8")
    assert prov.probe() == {ident}


def test_probe_empty_when_go_missing(fake_go) -> None:
    prov, _cli, store, gobin = fake_go(present=False)
    gobin.mkdir(parents=True, exist_ok=True)
    (gobin / "tool").write_text("", encoding="utf-8")
    ident = Identity(key="github.com/o/tool", display="github.com/o/tool")
    store.record(ident, version="1", checksum=None, path=str(gobin / "tool"))
    assert prov.probe() == set()


def test_probe_reads_receipts_fresh_from_disk(fake_go) -> None:
    prov, _cli, store, gobin = fake_go()
    ident = Identity(key="github.com/o/tool", display="github.com/o/tool")
    assert prov.probe() == set()
    gobin.mkdir(parents=True, exist_ok=True)
    (gobin / "tool").write_text("", encoding="utf-8")
    store.record(ident, version="1", checksum=None, path=str(gobin / "tool"))
    assert prov.probe() == {ident}


def test_plan_excludes_present_and_is_pure(fake_go) -> None:
    prov, cli, _store, _gobin = fake_go()
    a = _item("github.com/o/a")
    b = _item("github.com/o/b")
    installed = {a.identity}
    delta = prov.plan([a, b], installed)
    assert delta.installed == (b.identity,)
    assert cli.calls == []


def test_apply_installs_absent_module_and_writes_receipt(fake_go) -> None:
    prov, cli, store, gobin = fake_go()
    outcome = prov.apply_one(_item("github.com/o/tool", version="1.2.3"))
    assert outcome.outcome is Outcome.OK
    assert ["/fake/go", "install", "--", "github.com/o/tool@1.2.3"] in cli.calls
    ident = Identity(key="github.com/o/tool", display="github.com/o/tool")
    assert store.installed() == {ident}
    assert store.path_for(ident) == gobin / "tool"


def test_apply_uses_latest_when_no_version(fake_go) -> None:
    prov, cli, _store, _gobin = fake_go()
    prov.apply_one(_item("github.com/o/tool"))
    assert ["/fake/go", "install", "--", "github.com/o/tool@latest"] in cli.calls


def test_apply_skips_present_module_without_install(fake_go) -> None:
    prov, cli, store, gobin = fake_go()
    ident = Identity(key="github.com/o/tool", display="github.com/o/tool")
    gobin.mkdir(parents=True, exist_ok=True)
    (gobin / "tool").write_text("", encoding="utf-8")
    store.record(ident, version="1", checksum=None, path=str(gobin / "tool"))
    outcome = prov.apply_one(_item("github.com/o/tool", version="1"))
    assert outcome.outcome is Outcome.SKIP
    assert _install_calls(cli) == []


def test_apply_skip_writes_no_receipt(fake_go) -> None:
    prov, _cli, store, gobin = fake_go()
    ident = Identity(key="github.com/o/tool", display="github.com/o/tool")
    gobin.mkdir(parents=True, exist_ok=True)
    (gobin / "tool").write_text("", encoding="utf-8")
    store.record(ident, version="1", checksum=None, path=str(gobin / "tool"))
    before = (store._root / next(store._root.iterdir()).name).stat().st_mtime_ns
    prov.apply_one(_item("github.com/o/tool", version="1"))
    after = (store._root / next(store._root.iterdir()).name).stat().st_mtime_ns
    assert before == after


def test_apply_soft_when_go_missing(fake_go) -> None:
    prov, _cli, store, _gobin = fake_go(present=False)
    outcome = prov.apply_one(_item("github.com/o/tool"))
    assert outcome.outcome is Outcome.SOFT
    assert "go" in outcome.detail.lower()
    assert "go.dev" in outcome.detail
    assert store.installed() == set()


def test_apply_soft_on_install_failure_with_stderr_detail(fake_go) -> None:
    prov, _cli, store, _gobin = fake_go(
        install_errors={"github.com/o/bad@latest": "build failed boom"}
    )
    outcome = prov.apply_one(_item("github.com/o/bad"))
    assert outcome.outcome is Outcome.SOFT
    assert "build failed boom" in outcome.detail
    assert store.installed() == set()


def test_apply_double_dash_before_module(fake_go) -> None:
    prov, cli, _store, _gobin = fake_go()
    prov.apply_one(_item("github.com/o/tool", version="1"))
    call = _install_calls(cli)[0]
    assert call.index("--") < call.index("github.com/o/tool@1")


def test_apply_uses_generous_install_timeout(fake_go) -> None:
    prov, cli, _store, _gobin = fake_go()
    prov.apply_one(_item("github.com/o/slow"))
    install_call = _install_calls(cli)[0]
    timeout = cli.timeouts[cli.calls.index(install_call)]
    assert timeout == prov_go._INSTALL_TIMEOUT_S
    assert prov_go._INSTALL_TIMEOUT_S >= 600


def test_second_run_yields_empty_delta(fake_go) -> None:
    prov, _cli, _store, _gobin = fake_go()
    item = _item("github.com/o/tool", version="1")
    assert prov.apply_one(item).outcome is Outcome.OK
    delta = prov.plan([item], prov.probe())
    assert delta.is_empty()


def test_uninstall_removes_receipt_and_binary(fake_go) -> None:
    prov, _cli, store, gobin = fake_go()
    ident = Identity(key="github.com/o/tool", display="github.com/o/tool")
    gobin.mkdir(parents=True, exist_ok=True)
    binary = gobin / "tool"
    binary.write_text("", encoding="utf-8")
    store.record(ident, version="1", checksum=None, path=str(binary))
    prov.uninstall_one(ident)
    assert store.installed() == set()
    assert not binary.exists()


def test_uninstall_tolerates_missing_binary(fake_go) -> None:
    prov, _cli, store, gobin = fake_go()
    ident = Identity(key="github.com/o/tool", display="github.com/o/tool")
    store.record(ident, version="1", checksum=None, path=str(gobin / "tool"))
    prov.uninstall_one(ident)
    assert store.installed() == set()


def test_uninstall_tolerates_no_receipt(fake_go) -> None:
    prov, _cli, _store, _gobin = fake_go()
    ident = Identity(key="github.com/o/absent", display="github.com/o/absent")
    prov.uninstall_one(ident)


def test_reconcile_idempotent_second_run_empty(fake_go) -> None:
    from setforge.config import ReconcilePolicy
    from setforge.provision.driver import reconcile

    prov, _cli, _store, _gobin = fake_go()
    items = [_item("github.com/o/tool", version="1")]
    first = reconcile(prov, items, policy=ReconcilePolicy.ADDITIVE)
    assert [o.outcome for o in first.outcomes] == [Outcome.OK]
    second = reconcile(prov, items, policy=ReconcilePolicy.ADDITIVE)
    assert second.delta.is_empty()
    assert second.outcomes == ()


def test_reconcile_report_only_writes_nothing(fake_go) -> None:
    from setforge.config import ReconcilePolicy
    from setforge.provision.driver import reconcile

    prov, cli, store, _gobin = fake_go()
    items = [_item("github.com/o/tool", version="1")]
    result = reconcile(prov, items, policy=ReconcilePolicy.ADDITIVE, report_only=True)
    assert result.reported is True
    assert result.delta.installed == (items[0].identity,)
    assert _install_calls(cli) == []
    assert store.installed() == set()


def test_reconcile_partial_failure_isolated(fake_go) -> None:
    from setforge.config import ReconcilePolicy
    from setforge.provision.driver import reconcile

    prov, _cli, store, _gobin = fake_go(
        install_errors={"github.com/o/bad@latest": "boom"}
    )
    good = _item("github.com/o/good")
    bad = _item("github.com/o/bad")
    result = reconcile(prov, [good, bad], policy=ReconcilePolicy.ADDITIVE)
    outcomes = {o.item.identity.key: o.outcome for o in result.outcomes}
    assert outcomes["github.com/o/good"] is Outcome.OK
    assert outcomes["github.com/o/bad"] is Outcome.SOFT
    assert good.identity in store.installed()
    assert bad.identity not in store.installed()
