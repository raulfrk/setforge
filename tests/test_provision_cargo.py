"""Tests for the cargo :class:`Provisioner` (``setforge.provision.cargo``).

``subprocess.run`` and binary resolution are monkeypatched so no real
``cargo`` is invoked — mirroring :mod:`tests.test_cargo`'s ``FakeCargo``
pattern. Covers structured ``cargo install --list`` parsing, fail-open probes,
exact locked-version and registry-source enforcement, frozen-plan inventory
drift, the OK / SKIP / SOFT / HARD outcomes, the ``--`` separator guard,
timeouts, idempotence, and uninstall.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from setforge.errors import ResolveError, SetforgeError
from setforge.provision import cargo as prov_cargo
from setforge.provision.driver import plan_reconcile, validate_reconcile
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
        versions: dict[str, str] | None = None,
        sources: dict[str, str] | None = None,
        install_errors: dict[str, str] | None = None,
        list_error: Exception | None = None,
        list_stdout: str | None = None,
    ) -> None:
        self.installed = installed or set()
        self.versions = versions or {}
        self.sources = sources or {}
        self.install_errors = install_errors or {}
        self.list_error = list_error
        self.list_stdout = list_stdout
        self.calls: list[list[str]] = []
        self.timeouts: list[object] = []
        self.registry_checksum = f"sha256:{'a' * 64}"
        self.registry_error: ResolveError | None = None
        self.registry_calls: list[tuple[str, str]] = []

    def run(self, argv, **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        self.timeouts.append(kwargs.get("timeout"))
        # [cargo, install, --list] | [cargo, install, --, <crate>]
        #   | [cargo, uninstall, --, <crate>]
        if argv[1] == "install" and argv[2] == "--list":
            if self.list_error is not None:
                raise self.list_error
            if self.list_stdout is not None:
                return subprocess.CompletedProcess(argv, 0, stdout=self.list_stdout)
            lines = []
            for name in sorted(self.installed):
                source = f" ({self.sources[name]})" if name in self.sources else ""
                lines.append(f"{name} v{self.versions.get(name, '1.0.0')}{source}:")
                lines.append(f"    {name}")
            return subprocess.CompletedProcess(argv, 0, stdout="\n".join(lines) + "\n")
        if argv[1] == "install":
            separator = argv.index("--")
            crate = argv[separator + 1]
            if crate in self.install_errors:
                raise subprocess.CalledProcessError(
                    1, argv, stderr=self.install_errors[crate]
                )
            self.installed.add(crate)
            if "--version" in argv:
                self.versions[crate] = argv[argv.index("--version") + 1]
                self.sources.pop(crate, None)
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

        def _registry_checksum(crate: str, version: str) -> str:
            cli.registry_calls.append((crate, version))
            if cli.registry_error is not None:
                raise cli.registry_error
            return cli.registry_checksum

        monkeypatch.setattr(prov_cargo, "registry_checksum", _registry_checksum)
        return cli if present else None

    return _install


def _item(
    crate: str, *, version: str | None = None, checksum: str | None = None
) -> ProvisionItem:
    """Build a cargo :class:`ProvisionItem` for ``crate`` (key=display=crate)."""
    return ProvisionItem(
        type="cargo",
        identity=Identity(key=crate, display=crate),
        version=version,
        checksum=checksum,
    )


def _pinned_item(crate: str, version: str = "1.2.3") -> ProvisionItem:
    return _item(crate, version=version, checksum=f"sha256:{'a' * 64}")


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


def test_probe_parses_path_source_but_keeps_identity(fake_cargo) -> None:
    fake_cargo(
        installed={"local-tool"},
        versions={"local-tool": "1.2.3"},
        sources={"local-tool": "/work/local-tool"},
    )
    provisioner = prov_cargo.CargoProvisioner()
    assert provisioner.probe() == {Identity(key="local-tool", display="local-tool")}
    assert provisioner.inventory_fingerprint(set()) == (
        True,
        (
            prov_cargo._InstalledCrate(
                name="local-tool", version="1.2.3", source="/work/local-tool"
            ),
        ),
    )


@pytest.mark.parametrize(
    "stdout",
    [
        "not a cargo header\n",
        "ripgrep v14.0.0:\nripgrep v13.0.0:\n",
    ],
)
def test_probe_invalidates_malformed_or_conflicting_inventory(
    fake_cargo, stdout: str
) -> None:
    fake_cargo(list_stdout=stdout)
    provisioner = prov_cargo.CargoProvisioner()
    assert provisioner.probe() == set()
    assert provisioner.inventory_fingerprint(set()) == (False, ())


@given(
    name=st.text(
        alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-",
        min_size=1,
        max_size=24,
    ),
    version=st.tuples(
        st.integers(min_value=0, max_value=999),
        st.integers(min_value=0, max_value=999),
        st.integers(min_value=0, max_value=999),
    ),
)
def test_parse_crates_accepts_well_formed_registry_headers(
    name: str, version: tuple[int, int, int]
) -> None:
    header = f"{name} v{version[0]}.{version[1]}.{version[2]}:"
    parsed = prov_cargo._parse_crates(f"{header}\n    binary\n")
    assert parsed is not None
    assert len(parsed) == 1


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


def test_plan_unpinned_casefold_match_is_satisfied(fake_cargo) -> None:
    fake_cargo(installed={"serde"})
    provisioner = prov_cargo.CargoProvisioner()
    installed = provisioner.probe()
    assert provisioner.plan([_item("Serde")], installed).is_empty()


def test_plan_requires_exact_registry_version_for_pinned_item(fake_cargo) -> None:
    fake_cargo(
        installed={"ripgrep"},
        versions={"ripgrep": "13.0.0"},
    )
    provisioner = prov_cargo.CargoProvisioner()
    installed = provisioner.probe()
    assert provisioner.plan([_pinned_item("ripgrep", "14.0.0")], installed).installed


def test_plan_accepts_exact_registry_version_but_not_path_source(fake_cargo) -> None:
    cli = fake_cargo(installed={"ripgrep"}, versions={"ripgrep": "14.0.0"})
    provisioner = prov_cargo.CargoProvisioner()
    installed = provisioner.probe()
    assert provisioner.plan([_pinned_item("ripgrep", "14.0.0")], installed).is_empty()

    cli.sources["ripgrep"] = "/work/ripgrep"
    installed = provisioner.probe()
    assert provisioner.plan([_pinned_item("ripgrep", "14.0.0")], installed).installed


def test_plan_selects_invalid_pin_for_hard_apply_outcome(fake_cargo) -> None:
    fake_cargo(installed={"ripgrep"}, versions={"ripgrep": "14.0.0"})
    provisioner = prov_cargo.CargoProvisioner()
    installed = provisioner.probe()
    invalid = _item("ripgrep", version="14.0.0", checksum="sha256:bad")
    assert provisioner.plan([invalid], installed).installed == (invalid.identity,)


def test_plan_fingerprint_ignores_unlocked_items(fake_cargo) -> None:
    fake_cargo(installed=set())
    provisioner = prov_cargo.CargoProvisioner()
    installed = provisioner.probe()
    assert provisioner.plan_fingerprint([_item("ripgrep")], installed) == (
        (True, ()),
        (),
    )


def test_plan_selects_exact_installed_version_when_registry_checksum_differs(
    fake_cargo,
) -> None:
    cli = fake_cargo(installed={"ripgrep"}, versions={"ripgrep": "14.0.0"})
    cli.registry_checksum = f"sha256:{'b' * 64}"
    provisioner = prov_cargo.CargoProvisioner()
    item = _pinned_item("ripgrep", "14.0.0")
    installed = provisioner.probe()
    provisioner.plan_fingerprint([item], installed)
    assert provisioner.plan([item], installed).installed == (item.identity,)


def test_plan_fingerprint_records_unavailable_registry_checksum(fake_cargo) -> None:
    cli = fake_cargo(installed=set())
    cli.registry_error = ResolveError("registry unavailable")
    provisioner = prov_cargo.CargoProvisioner()
    item = _pinned_item("ripgrep", "14.0.0")
    installed = provisioner.probe()
    assert provisioner.plan_fingerprint([item], installed)[1] == (
        ("ripgrep", "14.0.0", None),
    )


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


def test_apply_unpinned_casefold_match_skips_without_install(fake_cargo) -> None:
    cli = fake_cargo(installed={"serde"})
    outcome = prov_cargo.CargoProvisioner().apply_one(_item("Serde"))
    assert outcome.outcome is Outcome.SKIP
    assert _install_calls(cli) == []


def test_apply_pinned_absent_crate_uses_exact_locked_argv(fake_cargo) -> None:
    cli = fake_cargo(installed=set())
    outcome = prov_cargo.CargoProvisioner().apply_one(_pinned_item("ripgrep", "14.0.0"))
    assert outcome.outcome is Outcome.OK
    assert _install_calls(cli) == [
        [
            "/fake/cargo",
            "install",
            "--version",
            "14.0.0",
            "--locked",
            "--",
            "ripgrep",
        ]
    ]


@pytest.mark.parametrize(
    ("installed_version", "source"),
    [("13.0.0", None), ("14.0.0", "/work/ripgrep")],
)
def test_apply_pinned_mismatch_forces_registry_reinstall(
    fake_cargo, installed_version: str, source: str | None
) -> None:
    sources = {} if source is None else {"ripgrep": source}
    cli = fake_cargo(
        installed={"ripgrep"},
        versions={"ripgrep": installed_version},
        sources=sources,
    )
    outcome = prov_cargo.CargoProvisioner().apply_one(_pinned_item("ripgrep", "14.0.0"))
    assert outcome.outcome is Outcome.OK
    assert _install_calls(cli) == [
        [
            "/fake/cargo",
            "install",
            "--version",
            "14.0.0",
            "--locked",
            "--force",
            "--",
            "ripgrep",
        ]
    ]


def test_apply_pinned_exact_registry_version_skips(fake_cargo) -> None:
    cli = fake_cargo(installed={"ripgrep"}, versions={"ripgrep": "14.0.0"})
    outcome = prov_cargo.CargoProvisioner().apply_one(_pinned_item("ripgrep", "14.0.0"))
    assert outcome.outcome is Outcome.SKIP
    assert _install_calls(cli) == []
    assert cli.registry_calls == [("ripgrep", "14.0.0")]


def test_apply_pinned_exact_version_casefold_match_skips(fake_cargo) -> None:
    cli = fake_cargo(installed={"serde"}, versions={"serde": "1.0.0"})
    outcome = prov_cargo.CargoProvisioner().apply_one(_pinned_item("Serde", "1.0.0"))
    assert outcome.outcome is Outcome.SKIP
    assert _install_calls(cli) == []
    assert cli.registry_calls == [("Serde", "1.0.0")]


@pytest.mark.parametrize(
    ("installed_version", "source"),
    [("0.9.0", None), ("1.0.0", "/work/serde")],
)
def test_apply_pinned_casefold_match_forces_version_or_source_mismatch(
    fake_cargo, installed_version: str, source: str | None
) -> None:
    sources = {} if source is None else {"serde": source}
    cli = fake_cargo(
        installed={"serde"},
        versions={"serde": installed_version},
        sources=sources,
    )
    outcome = prov_cargo.CargoProvisioner().apply_one(_pinned_item("Serde", "1.0.0"))
    assert outcome.outcome is Outcome.OK
    assert _install_calls(cli) == [
        [
            "/fake/cargo",
            "install",
            "--version",
            "1.0.0",
            "--locked",
            "--force",
            "--",
            "Serde",
        ]
    ]


def test_apply_pinned_exact_version_does_not_skip_checksum_mismatch(fake_cargo) -> None:
    cli = fake_cargo(installed={"ripgrep"}, versions={"ripgrep": "14.0.0"})
    cli.registry_checksum = f"sha256:{'b' * 64}"
    outcome = prov_cargo.CargoProvisioner().apply_one(_pinned_item("ripgrep", "14.0.0"))
    assert outcome.outcome is Outcome.HARD
    assert "checksum mismatch" in outcome.detail
    assert _install_calls(cli) == []


def test_apply_pinned_fails_hard_when_registry_checksum_unavailable(fake_cargo) -> None:
    cli = fake_cargo(installed=set())
    cli.registry_error = ResolveError("registry unavailable")
    outcome = prov_cargo.CargoProvisioner().apply_one(_pinned_item("ripgrep", "14.0.0"))
    assert outcome.outcome is Outcome.HARD
    assert "registry unavailable" in outcome.detail
    assert _install_calls(cli) == []


def test_apply_pinned_invalid_registry_utf8_is_hard_without_cargo_call(
    fake_cargo, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge.provision.resolve import cargo as cargo_resolve

    cli = fake_cargo(installed=set())
    monkeypatch.setattr(cargo_resolve, "fetch_bytes", lambda *_args, **_kwargs: b"\xff")
    monkeypatch.setattr(
        prov_cargo, "registry_checksum", cargo_resolve.registry_checksum
    )
    outcome = prov_cargo.CargoProvisioner().apply_one(_pinned_item("ripgrep", "14.0.0"))
    assert outcome.outcome is Outcome.HARD
    assert "valid UTF-8" in outcome.detail
    assert cli.calls == []


@pytest.mark.parametrize(
    "item",
    [
        _item("ripgrep", version="^14.0", checksum=f"sha256:{'a' * 64}"),
        _item("ripgrep", version="14.0.0", checksum="sha256:bad"),
        _item("ripgrep", version="14.0.0"),
    ],
)
def test_apply_rejects_incomplete_or_invalid_lock_pin(fake_cargo, item) -> None:
    cli = fake_cargo(installed=set())
    outcome = prov_cargo.CargoProvisioner().apply_one(item)
    assert outcome.outcome is Outcome.HARD
    assert cli.calls == []


def test_frozen_plan_revalidation_detects_cargo_version_drift(fake_cargo) -> None:
    cli = fake_cargo(installed={"ripgrep"}, versions={"ripgrep": "14.0.0"})
    provisioner = prov_cargo.CargoProvisioner()
    plan = plan_reconcile(provisioner, [_pinned_item("ripgrep", "14.0.0")])
    cli.versions["ripgrep"] = "13.0.0"

    with pytest.raises(SetforgeError, match="inventory changed"):
        validate_reconcile(plan)


def test_frozen_plan_revalidation_detects_registry_checksum_drift(fake_cargo) -> None:
    cli = fake_cargo(installed={"ripgrep"}, versions={"ripgrep": "14.0.0"})
    provisioner = prov_cargo.CargoProvisioner()
    plan = plan_reconcile(provisioner, [_pinned_item("ripgrep", "14.0.0")])
    cli.registry_checksum = f"sha256:{'b' * 64}"

    with pytest.raises(SetforgeError, match="inventory changed"):
        validate_reconcile(plan)


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
