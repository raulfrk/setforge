"""Regression: marketplace reconcile must match by SOURCE, not YAML key.

``claude plugin marketplace add`` registers a marketplace under a name it
derives from the repo's manifest, which may differ from the YAML key the
user chose. The pre-fix reconcile computed ``mps_to_add`` as
``set(cfg.marketplaces) - set(list_marketplaces())`` — comparing YAML keys
against claude's reported NAMES. When the key (``anthropic``) differed from
claude's name (``anthropics``), the marketplace was never found, so
``marketplace_add`` ran on EVERY reconcile (non-idempotent; spurious
``marketplaces_added`` each run).

The fix matches each declared marketplace's source (``owner/repo`` slug or
filesystem path) against the ``source`` field of each registered entry.
"""

from setforge.claude_plugins import reconcile
from setforge.config import (
    MarketplaceSource,
    MarketplaceSourceKind,
    ReconcilePolicy,
)
from tests.conftest import _make_config, _make_resolved


def _anthropic_cfg():
    """Config whose YAML key (``anthropic``) differs from claude's name."""
    return _make_config(
        marketplaces={
            "anthropic": MarketplaceSource(
                source=MarketplaceSourceKind.GITHUB,
                repo="anthropics/plugins",
            )
        }
    )


def test_reconcile_skips_add_when_source_already_registered(fake_claude) -> None:
    """Key != claude name, but same source already registered → no add.

    This is the core bug: the marketplace is already registered (claude
    derived the name ``anthropics`` from the manifest), yet the YAML key is
    ``anthropic``. A key-based diff re-adds it; a source-based diff does not.
    """
    fake = fake_claude(
        marketplaces=[{"name": "anthropics", "source": "github:anthropics/plugins"}]
    )
    cfg = _anthropic_cfg()
    profile = _make_resolved(plugins_reconcile=ReconcilePolicy.ADDITIVE)

    report = reconcile(cfg, profile)

    assert fake.mp_add_args() == []
    assert report.marketplaces_added == []


def test_reconcile_marketplace_add_is_idempotent_across_runs(fake_claude) -> None:
    """Two reconciles in a row: the second adds nothing (idempotent).

    Starts from an empty marketplace state so the first run adds the
    marketplace; FakeClaude then records it under its manifest-derived name
    (the repo basename, ``plugins``) — which differs from the YAML key
    ``anthropic``. The second run must still recognize it as registered.
    """
    fake = fake_claude(marketplaces=[])
    cfg = _anthropic_cfg()
    profile = _make_resolved(plugins_reconcile=ReconcilePolicy.ADDITIVE)

    first = reconcile(cfg, profile)
    assert first.marketplaces_added == ["anthropic"]
    assert len(fake.mp_add_args()) == 1

    second = reconcile(cfg, profile)
    assert second.marketplaces_added == []
    # No second add call recorded — total adds stays at one.
    assert len(fake.mp_add_args()) == 1


def test_reconcile_path_marketplace_matched_by_path(tmp_path, fake_claude) -> None:
    """A PATH-source marketplace already registered by path → no re-add."""
    mp_path = tmp_path / "local-mp"
    mp_path.mkdir()
    fake = fake_claude(marketplaces=[{"name": "whatever-name", "source": str(mp_path)}])
    cfg = _make_config(
        marketplaces={
            "local": MarketplaceSource(
                source=MarketplaceSourceKind.PATH,
                path=mp_path,
            )
        }
    )
    profile = _make_resolved(plugins_reconcile=ReconcilePolicy.ADDITIVE)

    report = reconcile(cfg, profile)

    assert fake.mp_add_args() == []
    assert report.marketplaces_added == []


def test_reconcile_unregistered_marketplace_still_added(fake_claude) -> None:
    """Guardrail: a genuinely-absent declared marketplace is still added."""
    fake = fake_claude(marketplaces=[{"name": "other", "source": "github:other/repo"}])
    cfg = _anthropic_cfg()
    profile = _make_resolved(plugins_reconcile=ReconcilePolicy.ADDITIVE)

    report = reconcile(cfg, profile)

    assert report.marketplaces_added == ["anthropic"]
    assert fake.mp_add_args() == ["anthropics/plugins"]
