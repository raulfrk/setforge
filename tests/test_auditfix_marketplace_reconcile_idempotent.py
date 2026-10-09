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

from pathlib import Path

import pytest

from setforge import reconcile_adapter
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

    report = reconcile(
        cfg,
        declared_plugin_ids=reconcile_adapter.plugin_ids(cfg, profile),
        policy=reconcile_adapter.plugin_policy(profile),
    )

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

    first = reconcile(
        cfg,
        declared_plugin_ids=reconcile_adapter.plugin_ids(cfg, profile),
        policy=reconcile_adapter.plugin_policy(profile),
    )
    assert first.marketplaces_added == ["anthropic"]
    assert len(fake.mp_add_args()) == 1

    second = reconcile(
        cfg,
        declared_plugin_ids=reconcile_adapter.plugin_ids(cfg, profile),
        policy=reconcile_adapter.plugin_policy(profile),
    )
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

    report = reconcile(
        cfg,
        declared_plugin_ids=reconcile_adapter.plugin_ids(cfg, profile),
        policy=reconcile_adapter.plugin_policy(profile),
    )

    assert fake.mp_add_args() == []
    assert report.marketplaces_added == []


_GIT_LINK = "https://example.test/o/mp.git"


@pytest.mark.parametrize(
    ("declared", "listed"),
    [
        pytest.param(
            MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="o/mp"),
            {"source": "github", "repo": "o/mp"},
            id="github",
        ),
        pytest.param(
            MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo=_GIT_LINK),
            {"source": "git", "url": _GIT_LINK},
            id="git-link",
        ),
        pytest.param(
            MarketplaceSource(source=MarketplaceSourceKind.PATH, path=Path("/srv/mp")),
            {"source": "directory", "path": "/srv/mp", "installLocation": "/srv/mp"},
            id="directory",
        ),
        pytest.param(
            MarketplaceSource(
                source=MarketplaceSourceKind.PATH,
                path=Path("/srv/mp/.claude-plugin/marketplace.json"),
            ),
            {
                "source": "file",
                "path": "/srv/mp/.claude-plugin/marketplace.json",
                "installLocation": "/srv/mp",
            },
            id="file",
        ),
    ],
)
def test_reconcile_skips_add_for_a_marketplace_listed_as_the_tool_lists_it(
    fake_claude, declared: MarketplaceSource, listed: dict[str, str]
) -> None:
    """The tool lists ``source`` as a kind word and the origin beside it."""
    fake = fake_claude(marketplaces=[{"name": "tool-name", **listed}])
    cfg = _make_config(marketplaces={"mine": declared})
    profile = _make_resolved(plugins_reconcile=ReconcilePolicy.ADDITIVE)

    report = reconcile(
        cfg,
        declared_plugin_ids=reconcile_adapter.plugin_ids(cfg, profile),
        policy=reconcile_adapter.plugin_policy(profile),
    )

    assert fake.mp_add_args() == []
    assert report.marketplaces_added == []


def test_reconcile_adds_a_directory_declared_beside_its_marketplace_file(
    fake_claude,
) -> None:
    """A marketplace registered from its file is not the directory holding it."""
    fake = fake_claude(
        marketplaces=[
            {
                "name": "mp",
                "source": "file",
                "path": "/srv/mp/.claude-plugin/marketplace.json",
                "installLocation": "/srv/mp",
            }
        ]
    )
    cfg = _make_config(
        marketplaces={
            "mine": MarketplaceSource(
                source=MarketplaceSourceKind.PATH, path=Path("/srv/mp")
            )
        }
    )
    profile = _make_resolved(plugins_reconcile=ReconcilePolicy.ADDITIVE)

    reconcile(
        cfg,
        declared_plugin_ids=reconcile_adapter.plugin_ids(cfg, profile),
        policy=reconcile_adapter.plugin_policy(profile),
    )

    assert fake.mp_add_args() == ["/srv/mp"]


def test_reconcile_unregistered_marketplace_still_added(fake_claude) -> None:
    """Guardrail: a genuinely-absent declared marketplace is still added."""
    fake = fake_claude(marketplaces=[{"name": "other", "source": "github:other/repo"}])
    cfg = _anthropic_cfg()
    profile = _make_resolved(plugins_reconcile=ReconcilePolicy.ADDITIVE)

    report = reconcile(
        cfg,
        declared_plugin_ids=reconcile_adapter.plugin_ids(cfg, profile),
        policy=reconcile_adapter.plugin_policy(profile),
    )

    assert report.marketplaces_added == ["anthropic"]
    assert fake.mp_add_args() == ["anthropics/plugins"]
