import json
import subprocess
from pathlib import Path

import pytest

from setforge import codex_plugins
from setforge.config import MarketplaceSource, MarketplaceSourceKind, ReconcilePolicy
from setforge.errors import PluginToolMissing, SetforgeError


def _github(repo: str = "owner/repo") -> MarketplaceSource:
    return MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo=repo)


def test_list_installed_parses_stable_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        codex_plugins,
        "_run_json",
        lambda _args: {
            "installed": [
                {
                    "pluginId": "review@official",
                    "name": "review",
                    "marketplaceName": "official",
                    "authPolicy": {"opaque": "ignored"},
                }
            ]
        },
    )
    assert codex_plugins.list_installed() == {
        "review@official": codex_plugins.InstalledPlugin(
            "review@official", "review", "official"
        )
    }


def test_list_installed_rejects_shape_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_plugins, "_run_json", lambda _args: {"installed": {}})
    with pytest.raises(PluginToolMissing, match="installed list"):
        codex_plugins.list_installed()


def test_plan_is_idempotent_when_declared_state_is_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        codex_plugins,
        "list_installed",
        lambda: {
            "review@official": codex_plugins.InstalledPlugin(
                "review@official", "review", "official"
            )
        },
    )
    monkeypatch.setattr(
        codex_plugins,
        "list_marketplaces",
        lambda: {
            "official": codex_plugins.InstalledMarketplace(
                "official", Path("/cache/official")
            )
        },
    )
    plan = codex_plugins.plan_reconcile(
        declared_plugin_ids={"review@official"},
        marketplaces={
            "official": MarketplaceSource(
                source=MarketplaceSourceKind.PATH, path=Path("/cache/official")
            )
        },
        policy=ReconcilePolicy.ADDITIVE,
    )
    assert plan.to_install == ()
    assert plan.to_remove == ()
    assert plan.marketplaces_to_add == ()
    assert plan.marketplaces_to_replace == ()


def test_report_and_dry_run_never_mutate(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = codex_plugins.CodexPluginPlan(
        policy=ReconcilePolicy.REPORT,
        to_install=("review@official",),
        to_remove=(),
        marketplaces_to_add=(("official", _github()),),
        marketplaces_to_replace=(),
        pre_plugin_ids=(),
        pre_marketplaces=(),
    )
    monkeypatch.setattr(
        codex_plugins,
        "marketplace_add",
        lambda _source: pytest.fail("REPORT mutated marketplace state"),
    )
    monkeypatch.setattr(
        codex_plugins,
        "plugin_install",
        lambda _plugin: pytest.fail("REPORT mutated plugin state"),
    )
    report = codex_plugins.apply_plan(plan)
    assert report.dry_run
    assert report.installed == ["review@official"]
    assert report.marketplaces_added == ["official"]


def test_partial_failure_reports_only_successful_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugins: dict[str, codex_plugins.InstalledPlugin] = {}
    marketplaces: dict[str, codex_plugins.InstalledMarketplace] = {}
    monkeypatch.setattr(codex_plugins, "list_installed", lambda: dict(plugins))
    monkeypatch.setattr(codex_plugins, "list_marketplaces", lambda: dict(marketplaces))

    def add_marketplace(_source: MarketplaceSource) -> None:
        marketplaces["official"] = codex_plugins.InstalledMarketplace(
            "official", Path("/cache/official")
        )

    def install(plugin_id: str) -> None:
        if plugin_id.startswith("broken"):
            raise PluginToolMissing("native add failed")
        name, _, marketplace = plugin_id.partition("@")
        plugins[plugin_id] = codex_plugins.InstalledPlugin(plugin_id, name, marketplace)

    monkeypatch.setattr(codex_plugins, "marketplace_add", add_marketplace)
    monkeypatch.setattr(codex_plugins, "plugin_install", install)
    plan = codex_plugins.plan_reconcile(
        declared_plugin_ids={"good@official", "broken@official"},
        marketplaces={"official": _github()},
        policy=ReconcilePolicy.ADDITIVE,
    )
    report = codex_plugins.apply_plan(plan)
    assert report.marketplaces_added == ["official"]
    assert report.installed == ["good@official"]
    assert report.failed == [("broken@official", "native add failed")]


def test_prune_removes_only_undeclared_plugins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        codex_plugins,
        "list_installed",
        lambda: {
            name: codex_plugins.InstalledPlugin(name, name.split("@")[0], "official")
            for name in ("keep@official", "old@official")
        },
    )
    monkeypatch.setattr(codex_plugins, "list_marketplaces", lambda: {})
    plan = codex_plugins.plan_reconcile(
        declared_plugin_ids={"keep@official"},
        marketplaces={"official": _github()},
        policy=ReconcilePolicy.PRUNE,
    )
    assert plan.to_remove == ("old@official",)


def test_github_marketplace_source_uses_checkout_remote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        codex_plugins.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, "git@github.com:owner/repo.git\n", ""
        ),
    )
    assert codex_plugins._marketplace_present(
        "official",
        _github(),
        {
            "official": codex_plugins.InstalledMarketplace(
                "official", Path("/cache/official")
            )
        },
    )


def test_path_marketplace_git_checkout_uses_declared_path_identity() -> None:
    source = MarketplaceSource(
        source=MarketplaceSourceKind.PATH, path=Path("/work/plugins")
    )
    assert codex_plugins._marketplace_present(
        "team",
        source,
        {
            "team": codex_plugins.InstalledMarketplace(
                "team", Path("/work/plugins"), _github()
            )
        },
    )


def test_codex_transition_inverse_dispatches_only_codex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from setforge.cli._plugin_helpers import _reverse_codex_plugins
    from setforge.transitions import CodexPluginDelta

    removed: list[str] = []
    marketplaces_removed: list[str] = []
    monkeypatch.setattr(codex_plugins, "plugin_remove", removed.append)
    monkeypatch.setattr(codex_plugins, "plugin_install", pytest.fail)
    monkeypatch.setattr(
        codex_plugins,
        "list_marketplaces",
        lambda: {
            "official": codex_plugins.InstalledMarketplace(
                "official", Path("/cache/official")
            )
        },
    )
    monkeypatch.setattr(
        codex_plugins, "marketplace_remove", marketplaces_removed.append
    )
    reverse, failed = _reverse_codex_plugins(
        CodexPluginDelta(
            installed=("review@official",),
            removed=(),
            marketplaces_added=("official",),
            marketplaces_removed=(),
        )
    )
    assert failed == []
    assert removed == ["review@official"]
    assert marketplaces_removed == ["official"]
    assert reverse.removed == ("review@official",)
    assert reverse.marketplaces_removed == (
        ("official", {"source": "path", "path": "/cache/official"}),
    )


def test_successful_marketplace_replacement_records_both_sides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prior = MarketplaceSource(source=MarketplaceSourceKind.PATH, path=Path("/old"))
    desired = MarketplaceSource(source=MarketplaceSourceKind.PATH, path=Path("/new"))
    plan = codex_plugins.CodexPluginPlan(
        policy=ReconcilePolicy.ADDITIVE,
        to_install=(),
        to_remove=(),
        marketplaces_to_add=(),
        marketplaces_to_replace=(("official", prior, desired),),
        pre_plugin_ids=(),
        pre_marketplaces=(),
    )
    monkeypatch.setattr(codex_plugins, "validate_plan", lambda _plan: None)
    marketplaces = {
        "official": codex_plugins.InstalledMarketplace("official", Path("/old"), prior)
    }
    removed: list[str] = []
    added: list[MarketplaceSource] = []

    def remove(name: str) -> None:
        removed.append(name)
        marketplaces.pop(name)

    def add(source: MarketplaceSource) -> None:
        added.append(source)
        marketplaces["official"] = codex_plugins.InstalledMarketplace(
            "official", Path("/new"), source
        )

    monkeypatch.setattr(codex_plugins, "list_marketplaces", lambda: marketplaces)
    monkeypatch.setattr(codex_plugins, "marketplace_remove", remove)
    monkeypatch.setattr(codex_plugins, "marketplace_add", add)
    report = codex_plugins.apply_plan(plan)
    assert removed == ["official"]
    assert added == [desired]
    assert report.marketplaces_added == ["official"]
    assert report.marketplaces_removed == [
        ("official", {"source": "path", "path": "/old"})
    ]


@pytest.mark.parametrize("payload", [{}, {"success": 0}, {"success": "false"}])
def test_mutation_rejects_malformed_success_json(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> None:
    monkeypatch.setattr(codex_plugins, "_run_json", lambda _args: payload)
    with pytest.raises(PluginToolMissing, match="unsuccessful mutation"):
        codex_plugins.plugin_install("review@official")


def test_replacement_and_prune_reverse_restores_marketplace_before_plugin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from setforge.cli._plugin_helpers import _reverse_codex_plugins
    from setforge.transitions import CodexPluginDelta

    calls: list[str] = []
    monkeypatch.setattr(
        codex_plugins,
        "list_marketplaces",
        lambda: {
            "official": codex_plugins.InstalledMarketplace(
                "official",
                Path("/new"),
                MarketplaceSource(source=MarketplaceSourceKind.PATH, path=Path("/new")),
            )
        },
    )
    monkeypatch.setattr(
        codex_plugins,
        "plugin_remove",
        lambda plugin_id: calls.append(f"remove-plugin:{plugin_id}"),
    )
    monkeypatch.setattr(
        codex_plugins,
        "marketplace_remove",
        lambda name: calls.append(f"remove-marketplace:{name}"),
    )
    monkeypatch.setattr(
        codex_plugins,
        "marketplace_add",
        lambda source: calls.append(f"add-marketplace:{source.path}"),
    )
    monkeypatch.setattr(
        codex_plugins,
        "plugin_install",
        lambda plugin_id: calls.append(f"install-plugin:{plugin_id}"),
    )
    _reverse_codex_plugins(
        CodexPluginDelta(
            installed=("new@official",),
            removed=("old@official",),
            marketplaces_added=("official",),
            marketplaces_removed=(("official", {"source": "path", "path": "/old"}),),
        )
    )
    assert calls == [
        "remove-plugin:new@official",
        "remove-marketplace:official",
        "add-marketplace:/old",
        "install-plugin:old@official",
    ]


def test_list_boundaries_reject_duplicate_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    duplicate_plugin = {
        "installed": [
            {
                "pluginId": "review@official",
                "name": "review",
                "marketplaceName": "official",
            },
            {
                "pluginId": "review@official",
                "name": "other",
                "marketplaceName": "official",
            },
        ]
    }
    monkeypatch.setattr(codex_plugins, "_run_json", lambda _args: duplicate_plugin)
    with pytest.raises(PluginToolMissing, match="repeats pluginId"):
        codex_plugins.list_installed()

    monkeypatch.setattr(
        codex_plugins,
        "_run_json",
        lambda _args: {
            "marketplaces": [
                {"name": "official", "root": "/one"},
                {"name": "official", "root": "/two"},
            ]
        },
    )
    monkeypatch.setattr(codex_plugins, "_source_from_root", lambda root: _github())
    with pytest.raises(PluginToolMissing, match="repeats name"):
        codex_plugins.list_marketplaces()


@pytest.mark.parametrize("failure_phase", ["remove", "add"])
def test_replacement_failure_records_only_completed_effects(
    monkeypatch: pytest.MonkeyPatch, failure_phase: str
) -> None:
    prior = MarketplaceSource(source=MarketplaceSourceKind.PATH, path=Path("/old"))
    desired = MarketplaceSource(source=MarketplaceSourceKind.PATH, path=Path("/new"))
    plan = codex_plugins.CodexPluginPlan(
        policy=ReconcilePolicy.ADDITIVE,
        to_install=(),
        to_remove=(),
        marketplaces_to_add=(),
        marketplaces_to_replace=(("official", prior, desired),),
        pre_plugin_ids=(),
        pre_marketplaces=(),
    )
    monkeypatch.setattr(codex_plugins, "validate_plan", lambda _plan: None)
    marketplaces = {
        "official": codex_plugins.InstalledMarketplace("official", Path("/old"), prior)
    }
    monkeypatch.setattr(codex_plugins, "list_marketplaces", lambda: marketplaces)

    def remove(name: str) -> None:
        if failure_phase == "remove":
            raise PluginToolMissing("remove failed")
        marketplaces.pop(name)

    def add(source: MarketplaceSource) -> None:
        if failure_phase == "add":
            raise PluginToolMissing("add failed")
        marketplaces["official"] = codex_plugins.InstalledMarketplace(
            "official", Path("/new"), source
        )

    monkeypatch.setattr(codex_plugins, "marketplace_remove", remove)
    monkeypatch.setattr(codex_plugins, "marketplace_add", add)
    report = codex_plugins.apply_plan(plan)

    assert report.marketplaces_added == []
    assert report.marketplaces_removed == (
        []
        if failure_phase == "remove"
        else [("official", {"source": "path", "path": "/old"})]
    )
    assert report.failed
    assert report.failed[0][0] == "marketplace:official"


def test_validate_plan_refuses_changed_frozen_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = codex_plugins.InstalledPlugin("review@official", "review", "official")
    marketplace = codex_plugins.InstalledMarketplace(
        "official", Path("/cache"), _github("owner/repo")
    )
    monkeypatch.setattr(
        codex_plugins, "list_installed", lambda: {plugin.plugin_id: plugin}
    )
    monkeypatch.setattr(
        codex_plugins, "list_marketplaces", lambda: {"official": marketplace}
    )
    monkeypatch.setattr(
        codex_plugins, "_github_source_from_root", lambda _root: _github("owner/repo")
    )
    plan = codex_plugins.plan_reconcile(
        declared_plugin_ids={plugin.plugin_id},
        marketplaces={"official": _github("owner/repo")},
        policy=ReconcilePolicy.ADDITIVE,
    )
    changed = codex_plugins.InstalledMarketplace(
        "official", Path("/cache"), _github("owner/other")
    )
    monkeypatch.setattr(
        codex_plugins, "list_marketplaces", lambda: {"official": changed}
    )
    monkeypatch.setattr(
        codex_plugins, "_github_source_from_root", lambda _root: _github("owner/other")
    )
    with pytest.raises(SetforgeError, match="inventory changed"):
        codex_plugins.validate_plan(plan)


def test_subprocess_oserror_is_capability_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_plugins, "_get_codex_bin", lambda: Path("/bin/codex"))
    monkeypatch.setattr(
        codex_plugins.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("exec race")),
    )
    with pytest.raises(PluginToolMissing, match="required non-interactive command"):
        codex_plugins.list_installed()


@pytest.mark.parametrize(
    "url",
    [
        "https://user:super-secret@github.com/owner/repo",
        "//user:super-secret@github.com/owner/repo",
        "custom+ssh://user:super-secret@github.com/owner/repo",
    ],
)
def test_subprocess_diagnostic_masks_url_credentials(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setattr(codex_plugins, "_get_codex_bin", lambda: Path("/bin/codex"))
    error = subprocess.CalledProcessError(
        1,
        ["codex"],
        stderr=f"unable to access {url}: denied",
    )
    monkeypatch.setattr(
        codex_plugins.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )
    with pytest.raises(PluginToolMissing) as caught:
        codex_plugins.list_installed()
    assert "super-secret" not in str(caught.value)
    assert "***@github.com/owner/repo: denied" in str(caught.value)


def test_github_source_inspection_failure_refuses_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        codex_plugins.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("git", 30)
        ),
    )
    with pytest.raises(PluginToolMissing, match="refusing destructive"):
        codex_plugins._marketplace_present(
            "official",
            _github(),
            {
                "official": codex_plugins.InstalledMarketplace(
                    "official", Path("/cache/official")
                )
            },
        )


def test_marketplace_add_rejects_credential_bearing_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        codex_plugins,
        "_run_mutation",
        lambda _args: pytest.fail("unsafe repo reached subprocess boundary"),
    )
    source = MarketplaceSource(
        source=MarketplaceSourceKind.GITHUB,
        repo="https://token@github.com/owner/repo",
    )
    with pytest.raises(SetforgeError, match="credential-free owner/repo"):
        codex_plugins.marketplace_add(source)


def test_credential_bearing_origin_blocks_replacement_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_plugins, "list_installed", lambda: {})
    monkeypatch.setattr(
        codex_plugins,
        "list_marketplaces",
        lambda: {
            "official": codex_plugins.InstalledMarketplace(
                "official", Path("/cache/official")
            )
        },
    )
    monkeypatch.setattr(
        codex_plugins.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, "https://github.com/owner/repo?access_token=secret\n", ""
        ),
    )
    with pytest.raises(PluginToolMissing, match="no canonical GitHub"):
        codex_plugins.plan_reconcile(
            declared_plugin_ids={"review@official"},
            marketplaces={"official": _github()},
            policy=ReconcilePolicy.ADDITIVE,
        )


@pytest.mark.parametrize("action", ["install", "remove"])
def test_plugin_side_effect_then_error_is_recorded(
    monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    plugin_id = "review@official"
    plugins = (
        {plugin_id: codex_plugins.InstalledPlugin(plugin_id, "review", "official")}
        if action == "remove"
        else {}
    )
    monkeypatch.setattr(codex_plugins, "list_installed", lambda: dict(plugins))
    monkeypatch.setattr(codex_plugins, "list_marketplaces", lambda: {})
    plan = codex_plugins.CodexPluginPlan(
        policy=ReconcilePolicy.PRUNE,
        to_install=(plugin_id,) if action == "install" else (),
        to_remove=(plugin_id,) if action == "remove" else (),
        marketplaces_to_add=(),
        marketplaces_to_replace=(),
        pre_plugin_ids=tuple(plugins),
        pre_marketplaces=(),
    )

    def mutate(value: str) -> None:
        if action == "install":
            plugins[value] = codex_plugins.InstalledPlugin(value, "review", "official")
        else:
            plugins.pop(value)
        raise PluginToolMissing("reported failure after mutation")

    monkeypatch.setattr(codex_plugins, f"plugin_{action}", mutate)
    report = codex_plugins.apply_plan(plan)
    assert (report.installed if action == "install" else report.removed) == [plugin_id]
    assert report.failed == [(plugin_id, "reported failure after mutation")]


def test_marketplace_replacement_side_effect_then_error_records_both_sides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prior = MarketplaceSource(source=MarketplaceSourceKind.PATH, path=Path("/old"))
    desired = MarketplaceSource(source=MarketplaceSourceKind.PATH, path=Path("/new"))
    marketplaces = {
        "official": codex_plugins.InstalledMarketplace("official", Path("/old"), prior)
    }
    monkeypatch.setattr(codex_plugins, "validate_plan", lambda _plan: None)
    monkeypatch.setattr(codex_plugins, "list_marketplaces", lambda: marketplaces)
    monkeypatch.setattr(
        codex_plugins, "marketplace_remove", lambda name: marketplaces.pop(name)
    )

    def add_then_error(source: MarketplaceSource) -> None:
        marketplaces["official"] = codex_plugins.InstalledMarketplace(
            "official", Path("/new"), source
        )
        raise PluginToolMissing("reported failure after mutation")

    monkeypatch.setattr(codex_plugins, "marketplace_add", add_then_error)
    report = codex_plugins.apply_plan(
        codex_plugins.CodexPluginPlan(
            policy=ReconcilePolicy.ADDITIVE,
            to_install=(),
            to_remove=(),
            marketplaces_to_add=(),
            marketplaces_to_replace=(("official", prior, desired),),
            pre_plugin_ids=(),
            pre_marketplaces=(),
        )
    )
    assert report.marketplaces_added == ["official"]
    assert report.marketplaces_removed == [
        ("official", {"source": "path", "path": "/old"})
    ]


def test_reverse_codex_preflight_failure_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from setforge.cli._plugin_helpers import _reverse_codex_plugins
    from setforge.transitions import CodexPluginDelta

    monkeypatch.setattr(
        codex_plugins,
        "list_installed",
        lambda: (_ for _ in ()).throw(PluginToolMissing("codex disappeared")),
    )
    reverse, failed = _reverse_codex_plugins(
        CodexPluginDelta(("review@official",), (), (), ())
    )
    assert reverse.is_empty()
    assert failed == [("codex", "codex disappeared")]


def test_reverse_codex_stale_path_failure_does_not_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from setforge.cli._plugin_helpers import _reverse_codex_plugins
    from setforge.transitions import CodexPluginDelta

    monkeypatch.setattr(codex_plugins, "list_installed", lambda: {})
    monkeypatch.setattr(codex_plugins, "list_marketplaces", lambda: {})
    monkeypatch.setattr(
        codex_plugins,
        "marketplace_add",
        lambda _source: (_ for _ in ()).throw(SetforgeError("path is missing")),
    )
    reverse, failed = _reverse_codex_plugins(
        CodexPluginDelta(
            (), (), (), (("local", {"source": "path", "path": "/missing"}),)
        )
    )
    assert reverse.is_empty()
    assert failed == [("local", "path is missing")]


def test_reverse_transition_aborts_when_codex_inverse_is_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge.cli import _plugin_helpers
    from setforge.errors import ReconcileAborted
    from setforge.transitions import CodexPluginDelta, TransitionDir

    transition = tmp_path / "transition"
    transition.mkdir()
    (transition / "codex_plugins.json").write_text(
        json.dumps(
            {
                "installed": ["review@official"],
                "removed": [],
                "marketplaces_added": [],
                "marketplaces_removed": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        _plugin_helpers,
        "_reverse_codex_plugins",
        lambda _delta: (
            CodexPluginDelta((), (), (), ()),
            [("codex", "inventory unavailable")],
        ),
    )
    with pytest.raises(ReconcileAborted, match="inventory unavailable"):
        _plugin_helpers._write_reverse_transition(
            TransitionDir(transition), "team", (), {}, filesystem_deltas=()
        )
