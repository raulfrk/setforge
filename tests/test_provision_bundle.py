"""Sequential DAG execution of a bundle (spec §6 bundle).

Drives the executor through :class:`InMemoryProvisioner` (via the ``reference``
provisioner type) so HARD / SOFT / OK outcomes are scriptable without a
subprocess. Covers the three phases the executor owns:

- **validate** — cycle (self-edge + back-edge) rejected, diamond accepted,
  dangling ``depends_on`` rejected, unknown ``package:`` ref rejected,
  duplicate id rejected;
- **topo order** — ``depends_on`` honored, declaration-order tiebreak
  deterministic, a diamond leaf applied exactly once;
- **execute** — a non-OK parent (HARD or SOFT) transitively skips ALL
  dependents; SOFT gates dependents but not exit; the revert delta records
  only OK components; an empty bundle is a clean no-op.
"""

from __future__ import annotations

import pytest

from setforge.config import (
    BundleComponent,
    BundleSpec,
    CargoPackage,
    Config,
    Profile,
    TrackedFile,
)
from setforge.errors import ConfigError
from setforge.provision.bundle import execute_bundle, topo_order, validate_bundle
from setforge.provision.driver import exit_code
from setforge.provision.protocol import Identity, Outcome
from setforge.provision.reference import InMemoryProvisioner

_PROFILE = "bundle-test"


def _cfg(**kw: object) -> Config:
    return Config(
        tracked_files={"d": TrackedFile(src=__import__("pathlib").Path("a"), dst="b")},
        profiles={_PROFILE: Profile()},
        **kw,  # type: ignore[arg-type]
    )


def _comp(
    id_: str, *, depends_on: list[str] | None = None, crate: str
) -> BundleComponent:
    """An inline-cargo component keyed on ``crate``."""
    return BundleComponent(
        id=id_, depends_on=depends_on or [], cargo=CargoPackage(crate=crate)
    )


# --------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------


def test_self_edge_rejected() -> None:
    bundle = BundleSpec(components=[_comp("a", depends_on=["a"], crate="ra")])
    with pytest.raises(ConfigError, match="cycle"):
        validate_bundle(bundle, _cfg())


def test_back_edge_cycle_rejected() -> None:
    bundle = BundleSpec(
        components=[
            _comp("a", depends_on=["b"], crate="ra"),
            _comp("b", depends_on=["a"], crate="rb"),
        ]
    )
    with pytest.raises(ConfigError, match="cycle"):
        validate_bundle(bundle, _cfg())


def test_diamond_is_not_a_cycle() -> None:
    # a → b, a → c, b → d, c → d  (dependency edges point at prerequisites)
    bundle = BundleSpec(
        components=[
            _comp("d", crate="rd"),
            _comp("b", depends_on=["d"], crate="rb"),
            _comp("c", depends_on=["d"], crate="rc"),
            _comp("a", depends_on=["b", "c"], crate="ra"),
        ]
    )
    validate_bundle(bundle, _cfg())  # must not raise


def test_dangling_depends_on_rejected() -> None:
    bundle = BundleSpec(components=[_comp("a", depends_on=["ghost"], crate="ra")])
    with pytest.raises(ConfigError, match="ghost"):
        validate_bundle(bundle, _cfg())


def test_unknown_package_ref_rejected() -> None:
    bundle = BundleSpec(components=[BundleComponent(id="a", package="not-declared")])
    with pytest.raises(ConfigError, match="not-declared"):
        validate_bundle(bundle, _cfg())


def test_known_package_ref_accepted() -> None:
    cfg = _cfg(packages={"rg": CargoPackage(crate="ripgrep")})
    bundle = BundleSpec(components=[BundleComponent(id="a", package="rg")])
    validate_bundle(bundle, cfg)  # must not raise


def test_plugin_source_rejected() -> None:
    # plugin has no provisioner (banked future work) → reject at validation,
    # not mid-install at build().
    bundle = BundleSpec(components=[BundleComponent(id="a", plugin="some-plugin")])
    with pytest.raises(ConfigError, match="plugin"):
        validate_bundle(bundle, _cfg())


def test_file_source_rejected() -> None:
    # file has no provisioner (banked future work) → reject at validation.
    bundle = BundleSpec(components=[BundleComponent(id="a", file="/etc/thing")])
    with pytest.raises(ConfigError, match="file"):
        validate_bundle(bundle, _cfg())


def test_duplicate_id_rejected() -> None:
    bundle = BundleSpec(
        components=[
            _comp("a", crate="ra"),
            _comp("a", crate="rb"),
        ]
    )
    with pytest.raises(ConfigError, match="duplicate"):
        validate_bundle(bundle, _cfg())


# --------------------------------------------------------------------------
# topo order
# --------------------------------------------------------------------------


def test_topo_order_honors_depends_on() -> None:
    # b depends on a → a must precede b.
    bundle = BundleSpec(
        components=[
            _comp("b", depends_on=["a"], crate="rb"),
            _comp("a", crate="ra"),
        ]
    )
    order = [c.id for c in topo_order(bundle)]
    assert order.index("a") < order.index("b")


def test_topo_order_declaration_tiebreak_deterministic() -> None:
    # Two roots with no edges → declaration order is the stable tiebreak.
    bundle = BundleSpec(
        components=[
            _comp("x", crate="rx"),
            _comp("y", crate="ry"),
            _comp("z", crate="rz"),
        ]
    )
    assert [c.id for c in topo_order(bundle)] == ["x", "y", "z"]


# --------------------------------------------------------------------------
# execute
# --------------------------------------------------------------------------


def _run(bundle: BundleSpec, cfg: Config, prov: InMemoryProvisioner):
    return execute_bundle(bundle, cfg, provisioner=prov)


def test_install_order_honors_depends_on() -> None:
    # Apply order (OK components) must place a prerequisite before its dependent.
    bundle = BundleSpec(
        components=[
            _comp("b", depends_on=["a"], crate="rb"),
            _comp("a", crate="ra"),
        ]
    )
    result = _run(bundle, _cfg(), InMemoryProvisioner())
    applied = [o.item.identity.key for o in result.outcomes if o.outcome is Outcome.OK]
    assert applied == ["ra", "rb"]


def test_diamond_leaf_applied_once() -> None:
    # d is depended on by both b and c; it must be applied exactly once.
    bundle = BundleSpec(
        components=[
            _comp("d", crate="rd"),
            _comp("b", depends_on=["d"], crate="rb"),
            _comp("c", depends_on=["d"], crate="rc"),
            _comp("a", depends_on=["b", "c"], crate="ra"),
        ]
    )
    result = _run(bundle, _cfg(), InMemoryProvisioner())
    ok_keys = [o.item.identity.key for o in result.outcomes if o.outcome is Outcome.OK]
    assert ok_keys.count("rd") == 1
    assert set(ok_keys) == {"ra", "rb", "rc", "rd"}


def test_dedup_inline_vs_ref_same_identity_applied_once() -> None:
    # A package-ref and an inline source that resolve to the SAME Identity.key
    # are the same leaf; apply it once.
    cfg = _cfg(packages={"rg": CargoPackage(crate="ripgrep")})
    bundle = BundleSpec(
        components=[
            BundleComponent(id="viaref", package="rg"),
            BundleComponent(id="inline", cargo=CargoPackage(crate="ripgrep")),
        ]
    )
    result = _run(bundle, cfg, InMemoryProvisioner())
    ok_keys = [o.item.identity.key for o in result.outcomes if o.outcome is Outcome.OK]
    assert ok_keys.count("ripgrep") == 1


def test_hard_parent_transitively_skips_child_and_grandchild() -> None:
    # a (HARD) → b → c :  b AND c are skipped (grandchild too).
    bundle = BundleSpec(
        components=[
            _comp("a", crate="ra"),
            _comp("b", depends_on=["a"], crate="rb"),
            _comp("c", depends_on=["b"], crate="rc"),
        ]
    )
    prov = InMemoryProvisioner(fail_hard={"ra"})
    result = _run(bundle, _cfg(), prov)
    by_key = {o.item.identity.key: o.outcome for o in result.outcomes}
    assert by_key["ra"] is Outcome.HARD
    assert by_key["rb"] is Outcome.SKIP
    assert by_key["rc"] is Outcome.SKIP
    assert exit_code(result) == 1  # HARD gates exit


def test_soft_parent_skips_dependents_but_exit_zero() -> None:
    bundle = BundleSpec(
        components=[
            _comp("a", crate="ra"),
            _comp("b", depends_on=["a"], crate="rb"),
        ]
    )
    prov = InMemoryProvisioner(soft={"ra"})
    result = _run(bundle, _cfg(), prov)
    by_key = {o.item.identity.key: o.outcome for o in result.outcomes}
    assert by_key["ra"] is Outcome.SOFT
    assert by_key["rb"] is Outcome.SKIP
    assert exit_code(result) == 0  # SOFT does NOT gate exit


def test_revert_delta_records_only_ok_components() -> None:
    # a OK, b HARD, c skipped → delta lists only a's identity.
    bundle = BundleSpec(
        components=[
            _comp("a", crate="ra"),
            _comp("b", depends_on=["a"], crate="rb"),
            _comp("c", depends_on=["b"], crate="rc"),
        ]
    )
    prov = InMemoryProvisioner(fail_hard={"rb"})
    result = _run(bundle, _cfg(), prov)
    assert result.delta.installed == (Identity(key="ra", display="ra"),)


def test_empty_bundle_is_clean_no_op() -> None:
    bundle = BundleSpec(components=[])
    result = _run(bundle, _cfg(), InMemoryProvisioner())
    assert result.outcomes == ()
    assert result.delta.installed == ()
    assert exit_code(result) == 0
