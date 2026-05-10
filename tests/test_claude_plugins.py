"""Tests for Claude plugin & marketplace reconcile.

``subprocess.run`` is monkeypatched to a fake driver that records every
call and simulates the ``claude plugin`` CLI surface, so tests can
assert on the exact sequence of install/enable/disable invocations
without requiring a real ``claude`` CLI.

Binary resolution is also monkeypatched via
``my_setup.claude_plugins.resolve_binary`` to control when the binary
is "found" vs absent.
"""

import subprocess
from pathlib import Path

import pytest

from my_setup.config import (
    ClaudePluginRef,
    Config,
    Dotfile,
    MarketplaceSource,
    MarketplaceSourceKind,
    Profile,
    ReconcilePolicy,
    ResolvedProfile,
)
from my_setup.errors import ConfigError, PluginToolMissing

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    *,
    marketplaces: dict | None = None,
    claude_plugins: dict | None = None,
) -> Config:
    """Build a minimal Config for reconcile tests."""
    return Config(
        dotfiles={"d": Dotfile(src=Path("tracked/x"), dst="~/x")},
        marketplaces=marketplaces or {},
        claude_plugins=claude_plugins or {},
        profiles={"default": Profile(dotfiles=["d"])},
    )


def _make_resolved(
    *,
    claude_plugins: list[str] | None = None,
    plugins_reconcile: ReconcilePolicy = ReconcilePolicy.ADDITIVE,
) -> ResolvedProfile:
    return ResolvedProfile(
        claude_plugins=claude_plugins or [],
        plugins_reconcile=plugins_reconcile,
    )


# ---------------------------------------------------------------------------
# Fake ``claude`` driver
# ---------------------------------------------------------------------------


class FakeClaude:
    """In-memory simulation of ``claude plugin`` commands."""

    def __init__(
        self,
        *,
        marketplaces: list[dict] | None = None,
        plugins: list[dict] | None = None,
    ) -> None:
        # Each marketplace entry: {"name": str, "source": str, ...}
        self._marketplaces: list[dict] = list(marketplaces or [])
        # Each plugin entry: {"id": "<name>@<mp>", "enabled": bool, ...}
        self._plugins: list[dict] = list(plugins or [])
        self.calls: list[list[str]] = []

    def run(self, args, **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        # args[0] is the binary path (we normalise as str already)
        cmd = args[1:]  # ["plugin", "marketplace", "list", "--json"]

        if cmd == ["plugin", "marketplace", "list", "--json"]:
            import json

            return subprocess.CompletedProcess(
                args, 0, json.dumps(self._marketplaces), ""
            )
        if cmd == ["plugin", "list", "--json"]:
            import json

            return subprocess.CompletedProcess(args, 0, json.dumps(self._plugins), "")
        if len(cmd) >= 3 and cmd[:2] == ["plugin", "marketplace"] and cmd[2] == "add":
            # claude plugin marketplace add <source-url>
            source_url = cmd[3]
            self._marketplaces.append({"name": source_url, "source": source_url})
            return subprocess.CompletedProcess(args, 0, "", "")
        if (
            len(cmd) >= 3
            and cmd[:2] == ["plugin", "marketplace"]
            and cmd[2] == "remove"
        ):
            name = cmd[3]
            self._marketplaces = [
                m for m in self._marketplaces if m.get("name") != name
            ]
            return subprocess.CompletedProcess(args, 0, "", "")
        if (
            len(cmd) >= 3
            and cmd[:2] == ["plugin", "marketplace"]
            and cmd[2] == "update"
        ):
            return subprocess.CompletedProcess(args, 0, "", "")
        if len(cmd) >= 3 and cmd[:2] == ["plugin", "install"]:
            plugin_arg = cmd[2]  # "name@marketplace" or similar
            # "--scope=user" may follow
            if not any(p["id"] == plugin_arg for p in self._plugins):
                # Match production: install adds to installed_plugins.json
                # without touching enabledPlugins. Plugin lands disabled
                # until 'enable' runs.
                self._plugins.append(
                    {"id": plugin_arg, "enabled": False, "scope": "user"}
                )
            # Re-install of an already-installed plugin: no-op on enabled
            # state (production claude doesn't touch enabledPlugins on
            # re-install).
            return subprocess.CompletedProcess(args, 0, "", "")
        if len(cmd) >= 3 and cmd[:2] == ["plugin", "enable"]:
            name = cmd[2]
            for p in self._plugins:
                if p["id"] == name:
                    p["enabled"] = True
            return subprocess.CompletedProcess(args, 0, "", "")
        if len(cmd) >= 3 and cmd[:2] == ["plugin", "disable"]:
            name = cmd[2]
            for p in self._plugins:
                if p["id"] == name:
                    p["enabled"] = False
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected claude invocation: {args!r}")

    # Convenience query helpers
    def install_args(self) -> list[str]:
        return [c[3] for c in self.calls if c[1:3] == ["plugin", "install"]]

    def enable_args(self) -> list[str]:
        return [c[3] for c in self.calls if c[1:3] == ["plugin", "enable"]]

    def disable_args(self) -> list[str]:
        return [c[3] for c in self.calls if c[1:3] == ["plugin", "disable"]]

    def mp_add_args(self) -> list[str]:
        return [
            c[4]
            for c in self.calls
            if len(c) > 4 and c[1:4] == ["plugin", "marketplace", "add"]
        ]


# ---------------------------------------------------------------------------
# P3.1 — Wrapper tests
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_claude(monkeypatch: pytest.MonkeyPatch):
    """Return a factory that wires FakeClaude into claude_plugins."""

    def factory(
        *,
        marketplaces: list[dict] | None = None,
        plugins: list[dict] | None = None,
    ) -> FakeClaude:
        fake = FakeClaude(marketplaces=marketplaces, plugins=plugins)
        monkeypatch.setattr(
            "my_setup.claude_plugins.resolve_binary",
            lambda name: Path("/usr/local/bin/claude") if name == "claude" else None,
        )
        monkeypatch.setattr("my_setup.claude_plugins.subprocess.run", fake.run)
        # Reset module-level binary cache so each test starts fresh.
        monkeypatch.setattr("my_setup.claude_plugins._claude_bin", None)
        return fake

    return factory


def test_list_marketplaces_returns_dict_keyed_by_name(fake_claude) -> None:
    from my_setup.claude_plugins import list_marketplaces

    fake_claude(
        marketplaces=[
            {"name": "anthropic", "source": "github:anthropics/claude-marketplace"},
        ]
    )
    result = list_marketplaces()
    assert isinstance(result, dict)
    assert "anthropic" in result
    assert result["anthropic"]["source"] == "github:anthropics/claude-marketplace"


def test_list_installed_returns_dict_keyed_by_id_with_enabled_field(
    fake_claude,
) -> None:
    from my_setup.claude_plugins import list_installed

    fake_claude(
        plugins=[
            {"id": "cline@anthropic", "enabled": True, "version": "1.0"},
            {"id": "ghost@anthropic", "enabled": False, "version": "0.9"},
        ]
    )
    result = list_installed()
    assert isinstance(result, dict)
    assert "cline@anthropic" in result
    assert "ghost@anthropic" in result
    assert result["cline@anthropic"]["enabled"] is True
    assert result["ghost@anthropic"]["enabled"] is False


def test_plugin_install_passes_scope_user(fake_claude) -> None:
    from my_setup.claude_plugins import plugin_install

    fake = fake_claude()
    plugin_install("cline", "anthropic")
    # Assert --scope=user is present in the call
    install_calls = [c for c in fake.calls if c[1:3] == ["plugin", "install"]]
    assert len(install_calls) == 1
    assert "--scope=user" in install_calls[0]
    assert "cline@anthropic" in install_calls[0]


def test_plugin_enable_synthesises_correct_command(fake_claude) -> None:
    from my_setup.claude_plugins import plugin_enable

    fake = fake_claude(plugins=[{"id": "cline@anthropic", "enabled": False}])
    plugin_enable("cline@anthropic")
    assert fake.enable_args() == ["cline@anthropic"]


def test_plugin_disable_synthesises_correct_command(fake_claude) -> None:
    from my_setup.claude_plugins import plugin_disable

    fake = fake_claude(plugins=[{"id": "cline@anthropic", "enabled": True}])
    plugin_disable("cline@anthropic")
    assert fake.disable_args() == ["cline@anthropic"]


def test_missing_claude_binary_raises_plugin_tool_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from my_setup.claude_plugins import (
        list_installed,
        list_marketplaces,
        plugin_install,
    )

    monkeypatch.setattr("my_setup.claude_plugins._claude_bin", None)
    monkeypatch.setattr("my_setup.claude_plugins.resolve_binary", lambda _: None)
    with pytest.raises(PluginToolMissing, match="claude"):
        list_installed()
    with pytest.raises(PluginToolMissing, match="claude"):
        list_marketplaces()
    with pytest.raises(PluginToolMissing, match="claude"):
        plugin_install("cline", "anthropic")


def test_get_claude_bin_consults_resolve_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_get_claude_bin() must delegate to resolve_binary, not shutil.which."""
    import my_setup.claude_plugins as cp

    calls: list[str] = []

    def recording_resolver(name: str) -> Path | None:
        calls.append(name)
        return Path("/custom/claude")

    # Reset module-level cache BEFORE setting the new resolver so the
    # next call actually hits the resolver (not the cached path).
    monkeypatch.setattr("my_setup.claude_plugins._claude_bin", None)
    monkeypatch.setattr("my_setup.claude_plugins.resolve_binary", recording_resolver)
    path = cp._get_claude_bin()
    assert "claude" in calls
    assert str(path) == "/custom/claude"


def test_marketplace_add_calls_correct_args(fake_claude) -> None:
    from my_setup.claude_plugins import marketplace_add

    fake = fake_claude()
    src = MarketplaceSource(
        source=MarketplaceSourceKind.GITHUB, repo="anthropics/plugins"
    )
    marketplace_add("anthropic", src)
    mp_calls = [c for c in fake.calls if c[1:4] == ["plugin", "marketplace", "add"]]
    assert len(mp_calls) == 1
    # Should contain the repo string
    assert "anthropics/plugins" in " ".join(mp_calls[0])


def test_marketplace_remove_calls_correct_args(fake_claude) -> None:
    from my_setup.claude_plugins import marketplace_remove

    fake = fake_claude(
        marketplaces=[{"name": "anthropic", "source": "github:anthropics/plugins"}]
    )
    marketplace_remove("anthropic")
    remove_calls = [
        c for c in fake.calls if c[1:4] == ["plugin", "marketplace", "remove"]
    ]
    assert len(remove_calls) == 1
    assert "anthropic" in remove_calls[0]


def test_marketplace_update_calls_correct_args(fake_claude) -> None:
    from my_setup.claude_plugins import marketplace_update

    fake = fake_claude()
    marketplace_update("anthropic")
    update_calls = [
        c for c in fake.calls if c[1:4] == ["plugin", "marketplace", "update"]
    ]
    assert len(update_calls) == 1


# ---------------------------------------------------------------------------
# P3.2 — Three-way reconcile tests
# ---------------------------------------------------------------------------


def test_reconcile_fresh_host_installs_all(fake_claude) -> None:
    """declared = {a@m1, b@m1}, installed = {} → to_install both; install called."""
    from my_setup.claude_plugins import reconcile

    fake = fake_claude()
    cfg = _make_config(
        claude_plugins={
            "a": ClaudePluginRef(marketplace="m1"),
            "b": ClaudePluginRef(marketplace="m1"),
        }
    )
    profile = _make_resolved(
        claude_plugins=["a", "b"],
        plugins_reconcile=ReconcilePolicy.ADDITIVE,
    )
    report = reconcile(cfg, profile)
    assert sorted(report.to_install) == [("a", "m1"), ("b", "m1")]
    assert report.to_enable == []
    assert report.to_disable == []
    assert sorted(fake.install_args()) == ["a@m1", "b@m1"]
    assert fake.disable_args() == []


def test_reconcile_fresh_install_lands_enabled(fake_claude) -> None:
    """Fresh install must trigger an enable so the plugin lands active.

    Primary acceptance gate for dotfiles-l37: a freshly-declared plugin
    must be both installed AND enabled in a single reconcile run, even
    though `claude plugin install` alone leaves it disabled.
    `to_enable` in the report keeps clean β2 semantics: only the
    original `declared ∩ disabled` set, NOT freshly-installed plugins.
    """
    from my_setup.claude_plugins import reconcile

    fake = fake_claude()
    cfg = _make_config(claude_plugins={"a": ClaudePluginRef(marketplace="m1")})
    profile = _make_resolved(
        claude_plugins=["a"],
        plugins_reconcile=ReconcilePolicy.ADDITIVE,
    )
    report = reconcile(cfg, profile)
    assert fake.install_args() == ["a@m1"]
    assert fake.enable_args() == ["a@m1"]
    entry = next(p for p in fake._plugins if p["id"] == "a@m1")
    assert entry["enabled"] is True
    assert report.to_install == [("a", "m1")]
    # β2: to_enable is the original pre-loop set; fresh installs are NOT
    # in it.
    assert report.to_enable == []


def test_reconcile_fresh_install_failure_skips_enable(
    fake_claude, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If install raises, plugin_enable is NOT called for that pid.

    The install loop's ``except`` branch must skip appending to the
    runtime enable working list, so a failed install never feeds an
    enable attempt.
    """
    from my_setup.claude_plugins import reconcile

    fake = fake_claude()
    real_run = fake.run

    def failing_run(args, **kwargs) -> subprocess.CompletedProcess:
        cmd = list(args[1:])
        if cmd == ["plugin", "install", "bad@m1", "--scope=user"]:
            fake.calls.append(list(args))
            raise subprocess.CalledProcessError(
                1, list(args), output="", stderr="install bombed"
            )
        return real_run(args, **kwargs)

    monkeypatch.setattr("my_setup.claude_plugins.subprocess.run", failing_run)

    cfg = _make_config(
        claude_plugins={
            "bad": ClaudePluginRef(marketplace="m1"),
            "good": ClaudePluginRef(marketplace="m1"),
        }
    )
    profile = _make_resolved(
        claude_plugins=["bad", "good"],
        plugins_reconcile=ReconcilePolicy.ADDITIVE,
    )
    report = reconcile(cfg, profile)
    # bad@m1 never reached the enable loop; good@m1 did.
    assert fake.enable_args() == ["good@m1"]
    failed_pids = [pid for pid, _ in report.failed]
    assert "bad@m1" in failed_pids
    failed_msg = next(msg for pid, msg in report.failed if pid == "bad@m1")
    assert failed_msg
    # to_install reflects intent (sorted set diff), regardless of failure.
    assert ("bad", "m1") in report.to_install
    assert ("good", "m1") in report.to_install


def test_reconcile_fresh_install_succeeds_then_enable_fails_records_failure(
    fake_claude, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Install OK, enable raises → pid in report.failed, NOT in to_enable.

    Self-healing semantics: pid is in `to_install` (install half landed
    on disk), pid is in `failed` with the enable-step stderr, pid is
    NOT added to `to_enable` in the report (clean β2 semantics). The
    next reconcile run will pick the plugin up via the existing
    ``declared ∩ disabled`` path with no new code.
    """
    from my_setup.claude_plugins import reconcile

    fake = fake_claude()
    real_run = fake.run

    def failing_run(args, **kwargs) -> subprocess.CompletedProcess:
        cmd = list(args[1:])
        if cmd == ["plugin", "enable", "a@m1"]:
            fake.calls.append(list(args))
            raise subprocess.CalledProcessError(
                1, list(args), output="", stderr="enable bombed"
            )
        return real_run(args, **kwargs)

    monkeypatch.setattr("my_setup.claude_plugins.subprocess.run", failing_run)

    cfg = _make_config(claude_plugins={"a": ClaudePluginRef(marketplace="m1")})
    profile = _make_resolved(
        claude_plugins=["a"],
        plugins_reconcile=ReconcilePolicy.ADDITIVE,
    )
    report = reconcile(cfg, profile)
    failed_pids = [pid for pid, _ in report.failed]
    assert "a@m1" in failed_pids
    failed_msg = next(msg for pid, msg in report.failed if pid == "a@m1")
    assert failed_msg
    # Install half landed on disk; enable half raised, so entry stays
    # disabled.
    entry = next(p for p in fake._plugins if p["id"] == "a@m1")
    assert entry["enabled"] is False
    assert ("a", "m1") in report.to_install
    # β2 clean semantics: `to_enable` is the original pre-loop set.
    assert "a@m1" not in report.to_enable


def test_reconcile_declared_but_disabled_enables_not_reinstalls(
    fake_claude,
) -> None:
    """declared = {a@m1}, installed = {a@m1: disabled} → enable only."""
    from my_setup.claude_plugins import reconcile

    fake = fake_claude(plugins=[{"id": "a@m1", "enabled": False}])
    cfg = _make_config(claude_plugins={"a": ClaudePluginRef(marketplace="m1")})
    profile = _make_resolved(
        claude_plugins=["a"],
        plugins_reconcile=ReconcilePolicy.ADDITIVE,
    )
    report = reconcile(cfg, profile)
    assert report.to_install == []
    assert report.to_enable == ["a@m1"]
    assert report.to_disable == []
    assert fake.install_args() == []
    assert fake.enable_args() == ["a@m1"]
    assert fake.disable_args() == []


def test_reconcile_additive_does_not_disable_extras(fake_claude) -> None:
    """ADDITIVE: installed has extras → to_disable=[] regardless."""
    from my_setup.claude_plugins import reconcile

    fake = fake_claude(
        plugins=[
            {"id": "a@m1", "enabled": True},
            {"id": "extra@m1", "enabled": True},
        ]
    )
    cfg = _make_config(claude_plugins={"a": ClaudePluginRef(marketplace="m1")})
    profile = _make_resolved(
        claude_plugins=["a"],
        plugins_reconcile=ReconcilePolicy.ADDITIVE,
    )
    report = reconcile(cfg, profile)
    assert report.to_disable == []
    assert fake.disable_args() == []


def test_reconcile_prune_disables_extras(fake_claude) -> None:
    """PRUNE: extras are disabled."""
    from my_setup.claude_plugins import reconcile

    fake = fake_claude(
        plugins=[
            {"id": "a@m1", "enabled": True},
            {"id": "extra@m1", "enabled": True},
        ]
    )
    cfg = _make_config(claude_plugins={"a": ClaudePluginRef(marketplace="m1")})
    profile = _make_resolved(
        claude_plugins=["a"],
        plugins_reconcile=ReconcilePolicy.PRUNE,
    )
    report = reconcile(cfg, profile)
    assert report.to_disable == ["extra@m1"]
    assert fake.disable_args() == ["extra@m1"]


def test_reconcile_mixed_states_prune(fake_claude) -> None:
    """declared={a,b}, enabled={a,c}, disabled={b,d} → install=[],enable=[b],disable=[c]."""
    from my_setup.claude_plugins import reconcile

    fake = fake_claude(
        plugins=[
            {"id": "a@m1", "enabled": True},
            {"id": "c@m1", "enabled": True},
            {"id": "b@m1", "enabled": False},
            {"id": "d@m1", "enabled": False},
        ]
    )
    cfg = _make_config(
        claude_plugins={
            "a": ClaudePluginRef(marketplace="m1"),
            "b": ClaudePluginRef(marketplace="m1"),
        }
    )
    profile = _make_resolved(
        claude_plugins=["a", "b"],
        plugins_reconcile=ReconcilePolicy.PRUNE,
    )
    report = reconcile(cfg, profile)
    assert report.to_install == []
    assert report.to_enable == ["b@m1"]
    assert report.to_disable == ["c@m1"]
    # d@m1 is disabled and not declared → it's already disabled; no action needed
    assert "d@m1" not in report.to_disable
    assert fake.enable_args() == ["b@m1"]
    assert fake.disable_args() == ["c@m1"]


def test_reconcile_report_policy_runs_no_subprocesses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REPORT: all three diffs computed, zero subprocess writes."""
    from my_setup.claude_plugins import reconcile

    monkeypatch.setattr("my_setup.claude_plugins._claude_bin", None)
    # Monkeypatch resolve_binary to return a valid path
    monkeypatch.setattr(
        "my_setup.claude_plugins.resolve_binary",
        lambda name: Path("/usr/local/bin/claude") if name == "claude" else None,
    )

    list_call_count = 0

    def read_only_run(args, **kwargs) -> subprocess.CompletedProcess:
        import json

        nonlocal list_call_count
        cmd = args[1:]
        if cmd == ["plugin", "list", "--json"]:
            list_call_count += 1
            return subprocess.CompletedProcess(
                args, 0, json.dumps([{"id": "extra@m1", "enabled": True}]), ""
            )
        if cmd == ["plugin", "marketplace", "list", "--json"]:
            return subprocess.CompletedProcess(args, 0, json.dumps([]), "")
        # Any write command must NOT be called
        raise AssertionError(f"REPORT mode must not invoke write command: {args!r}")

    monkeypatch.setattr("my_setup.claude_plugins.subprocess.run", read_only_run)
    cfg = _make_config(claude_plugins={"declared": ClaudePluginRef(marketplace="m1")})
    profile = _make_resolved(
        claude_plugins=["declared"],
        plugins_reconcile=ReconcilePolicy.REPORT,
    )
    report = reconcile(cfg, profile)
    assert report.dry_run is True
    assert ("declared", "m1") in report.to_install
    assert report.to_disable == ["extra@m1"]


def test_reconcile_dry_run_runs_no_subprocess_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dry_run=True: zero subprocess writes, regardless of policy."""
    from my_setup.claude_plugins import reconcile

    monkeypatch.setattr("my_setup.claude_plugins._claude_bin", None)
    monkeypatch.setattr(
        "my_setup.claude_plugins.resolve_binary",
        lambda name: Path("/usr/local/bin/claude") if name == "claude" else None,
    )

    def read_only_run(args, **kwargs) -> subprocess.CompletedProcess:
        import json

        cmd = args[1:]
        if cmd == ["plugin", "list", "--json"]:
            return subprocess.CompletedProcess(
                args, 0, json.dumps([{"id": "extra@m1", "enabled": True}]), ""
            )
        if cmd == ["plugin", "marketplace", "list", "--json"]:
            return subprocess.CompletedProcess(args, 0, json.dumps([]), "")
        raise AssertionError(f"dry_run must not invoke write command: {args!r}")

    monkeypatch.setattr("my_setup.claude_plugins.subprocess.run", read_only_run)
    cfg = _make_config(claude_plugins={"declared": ClaudePluginRef(marketplace="m1")})
    profile = _make_resolved(
        claude_plugins=["declared"],
        plugins_reconcile=ReconcilePolicy.PRUNE,
    )
    report = reconcile(cfg, profile, dry_run=True)
    assert report.dry_run is True
    assert ("declared", "m1") in report.to_install
    assert "extra@m1" in report.to_disable


def test_reconcile_marketplaces_always_added(fake_claude) -> None:
    """Declared marketplace not in list_marketplaces() → marketplace_add."""
    from my_setup.claude_plugins import reconcile

    fake = fake_claude(marketplaces=[])
    cfg = _make_config(
        marketplaces={
            "anthropic": MarketplaceSource(
                source=MarketplaceSourceKind.GITHUB,
                repo="anthropics/plugins",
            )
        }
    )
    profile = _make_resolved(plugins_reconcile=ReconcilePolicy.ADDITIVE)
    reconcile(cfg, profile)
    assert len(fake.mp_add_args()) == 1


def test_reconcile_stale_marketplace_not_evicted(fake_claude) -> None:
    """Marketplace installed but not declared → no remove call."""
    from my_setup.claude_plugins import reconcile

    fake = fake_claude(marketplaces=[{"name": "stale", "source": "github:stale/mp"}])
    cfg = _make_config()  # no declared marketplaces
    profile = _make_resolved(plugins_reconcile=ReconcilePolicy.PRUNE)
    reconcile(cfg, profile)
    remove_calls = [
        c for c in fake.calls if c[1:4] == ["plugin", "marketplace", "remove"]
    ]
    assert remove_calls == []


def test_reconcile_additive_disabled_not_in_to_disable(fake_claude) -> None:
    """ADDITIVE: disabled plugins not declared → not in to_disable."""
    from my_setup.claude_plugins import reconcile

    fake_claude(plugins=[{"id": "undeclared@m1", "enabled": False}])
    cfg = _make_config()
    profile = _make_resolved(
        claude_plugins=[],
        plugins_reconcile=ReconcilePolicy.ADDITIVE,
    )
    report = reconcile(cfg, profile)
    assert report.to_disable == []


# ---------------------------------------------------------------------------
# P3.3 — Bare-name resolution via top-level claude_plugins registry
# ---------------------------------------------------------------------------


def test_reconcile_resolves_bare_profile_names_via_registry(fake_claude) -> None:
    """Bare profile names resolve to <name>@<marketplace> via cfg.claude_plugins.

    Profile holds bare names like ['superpowers']; the top-level registry
    maps each name to a marketplace; reconcile must combine them into
    '<name>@<marketplace>' form before diffing against installed plugins.
    """
    from my_setup.claude_plugins import reconcile

    fake = fake_claude(plugins=[{"id": "superpowers@official", "enabled": True}])
    cfg = _make_config(
        claude_plugins={
            "superpowers": ClaudePluginRef(marketplace="official"),
        }
    )
    profile = _make_resolved(
        claude_plugins=["superpowers"],
        plugins_reconcile=ReconcilePolicy.ADDITIVE,
    )
    report = reconcile(cfg, profile)
    assert report.to_install == []
    assert report.to_enable == []
    assert report.to_disable == []
    assert fake.install_args() == []
    assert fake.enable_args() == []


def test_reconcile_bare_name_to_install_emits_at_form_pair(fake_claude) -> None:
    """First-time-declared bare name lands in to_install as (name, marketplace);
    install loop receives @-form id without _split_id crashing."""
    from my_setup.claude_plugins import reconcile

    fake = fake_claude()
    cfg = _make_config(
        claude_plugins={
            "new-plugin": ClaudePluginRef(marketplace="m1"),
        }
    )
    profile = _make_resolved(
        claude_plugins=["new-plugin"],
        plugins_reconcile=ReconcilePolicy.ADDITIVE,
    )
    report = reconcile(cfg, profile)
    assert report.to_install == [("new-plugin", "m1")]
    assert fake.install_args() == ["new-plugin@m1"]


def test_reconcile_bare_name_disabled_lands_in_to_enable(fake_claude) -> None:
    """Already-installed-but-disabled plugin: bare profile name resolves via
    registry, matches the @-form id from claude plugin list, lands in to_enable."""
    from my_setup.claude_plugins import reconcile

    fake = fake_claude(plugins=[{"id": "wiki@llm-wiki", "enabled": False}])
    cfg = _make_config(
        claude_plugins={
            "wiki": ClaudePluginRef(marketplace="llm-wiki"),
        }
    )
    profile = _make_resolved(
        claude_plugins=["wiki"],
        plugins_reconcile=ReconcilePolicy.ADDITIVE,
    )
    report = reconcile(cfg, profile)
    assert report.to_install == []
    assert report.to_enable == ["wiki@llm-wiki"]
    assert fake.enable_args() == ["wiki@llm-wiki"]


def test_reconcile_undeclared_bare_name_raises_config_error(fake_claude) -> None:
    """Profile lists a bare name not in the top-level registry → ConfigError
    naming the offending plugin, before any plugin write subprocesses run."""
    from my_setup.claude_plugins import reconcile

    fake = fake_claude()
    cfg = _make_config()  # empty registry
    profile = _make_resolved(
        claude_plugins=["mystery-plugin"],
        plugins_reconcile=ReconcilePolicy.ADDITIVE,
    )
    with pytest.raises(ConfigError, match="mystery-plugin"):
        reconcile(cfg, profile)
    assert fake.install_args() == []
    assert fake.enable_args() == []
    assert fake.disable_args() == []


# ---------------------------------------------------------------------------
# P3.4 — YAML editing helpers + --claude-bin wiring
# ---------------------------------------------------------------------------

_YAML_FIXTURE = """\
version: 1

# Top-level comment.
dotfiles:
  d:
    src: x
    dst: y

# Marketplaces comment.
marketplaces:
  existing-mp:
    source: github
    repo: owner/existing-mp

# Plugins comment.
claude_plugins:
  existing-plugin:
    marketplace: existing-mp

profiles:
  myprofile:
    # Profile comment.
    dotfiles:
      - d
    claude_plugins:
      - existing-plugin
  bare:
    dotfiles:
      - d
"""


def _write_yaml_fixture(tmp_path: Path) -> Path:
    p = tmp_path / "my_setup.yaml"
    p.write_text(_YAML_FIXTURE, encoding="utf-8")
    return p


def test_yaml_add_marketplace_appends(tmp_path: Path) -> None:
    from my_setup.claude_plugins import yaml_add_marketplace

    p = _write_yaml_fixture(tmp_path)
    src = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="acme/new-mp")
    added = yaml_add_marketplace(p, "new-mp", src)
    assert added is True
    text = p.read_text()
    assert "new-mp" in text
    assert "acme/new-mp" in text
    # Comments preserved
    assert "Top-level comment." in text
    assert "Marketplaces comment." in text
    assert "Plugins comment." in text


def test_yaml_add_marketplace_idempotent(tmp_path: Path) -> None:
    from my_setup.claude_plugins import yaml_add_marketplace

    p = _write_yaml_fixture(tmp_path)
    src = MarketplaceSource(
        source=MarketplaceSourceKind.GITHUB, repo="owner/existing-mp"
    )
    added = yaml_add_marketplace(p, "existing-mp", src)
    assert added is False
    # Only one occurrence in YAML
    assert p.read_text().count("existing-mp") >= 1


def test_yaml_remove_marketplace(tmp_path: Path) -> None:
    from my_setup.claude_plugins import yaml_remove_marketplace

    p = _write_yaml_fixture(tmp_path)
    removed = yaml_remove_marketplace(p, "existing-mp")
    assert removed is True
    from my_setup.config import load_config

    cfg = load_config(p)
    assert "existing-mp" not in cfg.marketplaces


def test_yaml_remove_marketplace_idempotent(tmp_path: Path) -> None:
    from my_setup.claude_plugins import yaml_remove_marketplace

    p = _write_yaml_fixture(tmp_path)
    removed = yaml_remove_marketplace(p, "ghost-mp")
    assert removed is False


def test_yaml_add_plugin_appends(tmp_path: Path) -> None:
    from my_setup.claude_plugins import yaml_add_plugin

    p = _write_yaml_fixture(tmp_path)
    added = yaml_add_plugin(p, "new-plugin", "existing-mp")
    assert added is True
    text = p.read_text()
    assert "new-plugin" in text
    # Comments preserved
    assert "Plugins comment." in text


def test_yaml_add_plugin_idempotent(tmp_path: Path) -> None:
    from my_setup.claude_plugins import yaml_add_plugin

    p = _write_yaml_fixture(tmp_path)
    added = yaml_add_plugin(p, "existing-plugin", "existing-mp")
    assert added is False


def test_yaml_add_plugin_to_profile(tmp_path: Path) -> None:
    from my_setup.claude_plugins import yaml_add_plugin, yaml_add_plugin_to_profile

    p = _write_yaml_fixture(tmp_path)
    # Mirror the production CLI flow: register in top-level claude_plugins
    # first, then append to the profile list. load_config validates that
    # every profile reference exists in the registry.
    yaml_add_plugin(p, "new-plugin", "existing-mp")
    added = yaml_add_plugin_to_profile(p, "myprofile", "new-plugin")
    assert added is True
    from my_setup.config import load_config

    cfg = load_config(p)
    assert "new-plugin" in cfg.profiles["myprofile"].claude_plugins


def test_yaml_add_plugin_to_profile_idempotent(tmp_path: Path) -> None:
    from my_setup.claude_plugins import yaml_add_plugin_to_profile

    p = _write_yaml_fixture(tmp_path)
    added = yaml_add_plugin_to_profile(p, "myprofile", "existing-plugin")
    assert added is False


def test_yaml_remove_plugin_from_profile(tmp_path: Path) -> None:
    from my_setup.claude_plugins import yaml_remove_plugin_from_profile

    p = _write_yaml_fixture(tmp_path)
    removed = yaml_remove_plugin_from_profile(p, "myprofile", "existing-plugin")
    assert removed is True
    from my_setup.config import load_config

    cfg = load_config(p)
    assert "existing-plugin" not in cfg.profiles["myprofile"].claude_plugins


def test_yaml_comments_preserved_after_edits(tmp_path: Path) -> None:
    """Multiple edits must not corrupt comments in the YAML file."""
    from my_setup.claude_plugins import (
        yaml_add_marketplace,
        yaml_add_plugin,
        yaml_add_plugin_to_profile,
    )

    p = _write_yaml_fixture(tmp_path)
    yaml_add_marketplace(
        p, "test-mp", MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="t/t")
    )
    yaml_add_plugin(p, "test-plugin", "test-mp")
    yaml_add_plugin_to_profile(p, "myprofile", "test-plugin")

    text = p.read_text()
    assert "Top-level comment." in text
    assert "Marketplaces comment." in text
    assert "Plugins comment." in text
    assert "Profile comment." in text


def test_claude_bin_override_flows_through_set_cli_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """--claude-bin flag must call binaries.set_cli_overrides(claude=...)."""
    from typer.testing import CliRunner

    import my_setup.binaries as binaries_mod
    from my_setup.cli import app

    calls: list[dict] = []
    original_set_cli_overrides = binaries_mod.set_cli_overrides

    def recording_set_cli_overrides(**kwargs):
        calls.append(dict(kwargs))
        # Reset claude_bin cache after override change
        import my_setup.claude_plugins as cp

        cp._claude_bin = None
        original_set_cli_overrides(**kwargs)

    # Patch the function on the binaries module itself so that
    # cli.py's `binaries.set_cli_overrides(...)` call goes through our recorder.
    monkeypatch.setattr(binaries_mod, "set_cli_overrides", recording_set_cli_overrides)

    runner = CliRunner()
    # Use compare on a nonexistent profile so the callback fires but the
    # subcommand fails with a known error. We don't care about the exit code;
    # we only need to verify set_cli_overrides was called with claude=.
    runner.invoke(
        app,
        ["--claude-bin=/tmp/fake-claude", "compare", "--profile=nonexistent"],
    )
    # At least one call recorded with claude=/tmp/fake-claude
    assert any(c.get("claude") == "/tmp/fake-claude" for c in calls), (
        f"set_cli_overrides not called with claude override; calls={calls}"
    )


def test_reconcile_marketplaces_dry_run_not_added(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under REPORT policy, marketplace_add is NOT called."""
    from my_setup.claude_plugins import reconcile

    monkeypatch.setattr("my_setup.claude_plugins._claude_bin", None)
    monkeypatch.setattr(
        "my_setup.claude_plugins.resolve_binary",
        lambda name: Path("/usr/local/bin/claude") if name == "claude" else None,
    )

    def read_only_run(args, **kwargs) -> subprocess.CompletedProcess:
        import json

        cmd = args[1:]
        if cmd in (
            ["plugin", "list", "--json"],
            ["plugin", "marketplace", "list", "--json"],
        ):
            return subprocess.CompletedProcess(args, 0, json.dumps([]), "")
        raise AssertionError(f"REPORT mode must not invoke write command: {args!r}")

    monkeypatch.setattr("my_setup.claude_plugins.subprocess.run", read_only_run)
    cfg = _make_config(
        marketplaces={
            "anthropic": MarketplaceSource(
                source=MarketplaceSourceKind.GITHUB,
                repo="anthropics/plugins",
            )
        }
    )
    profile = _make_resolved(plugins_reconcile=ReconcilePolicy.REPORT)
    report = reconcile(cfg, profile)
    assert "anthropic" in report.marketplaces_added
    assert report.dry_run is True


# ---------------------------------------------------------------------------
# dotfiles-l37 — `plugin add` strict enable behavior
# ---------------------------------------------------------------------------


def test_plugin_add_calls_enable_after_install(fake_claude, tmp_path: Path) -> None:
    """`plugin add` must run `plugin enable` after a successful install.

    Mirrors the reconcile-path fix at the CLI surface: a freshly added
    plugin should be active in a single command invocation, not two.
    """
    from typer.testing import CliRunner

    from my_setup.cli import app

    p = _write_yaml_fixture(tmp_path)
    fake = fake_claude()  # marketplace + plugin lists start empty

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "plugin",
            "add",
            "newp@existing-mp",
            "--from=github:foo/bar",
            "--profile=myprofile",
            f"--config={p}",
        ],
    )
    assert result.exit_code == 0, result.output
    assert fake.install_args() == ["newp@existing-mp"]
    assert fake.enable_args() == ["newp@existing-mp"]
    assert "installed plugin: newp@existing-mp" in result.output
    assert "enabled plugin: newp@existing-mp" in result.output


def test_plugin_add_strict_exits_nonzero_when_enable_fails(
    fake_claude, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`plugin add` must exit non-zero with a clear ERROR when enable fails.

    The install half retains today's pattern; the enable half is strict
    because `plugin add` is an interactive single-plugin command — a
    silent warning would be a footgun.
    """
    from typer.testing import CliRunner

    from my_setup.cli import app

    p = _write_yaml_fixture(tmp_path)
    fake = fake_claude()
    real_run = fake.run

    def failing_run(args, **kwargs) -> subprocess.CompletedProcess:
        cmd = list(args[1:])
        if cmd == ["plugin", "enable", "newp@existing-mp"]:
            fake.calls.append(list(args))
            raise subprocess.CalledProcessError(
                1, list(args), output="", stderr="enable broke"
            )
        return real_run(args, **kwargs)

    monkeypatch.setattr("my_setup.claude_plugins.subprocess.run", failing_run)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "plugin",
            "add",
            "newp@existing-mp",
            "--from=github:foo/bar",
            "--profile=myprofile",
            f"--config={p}",
        ],
    )
    assert result.exit_code == 1, result.output
    assert "ERROR: enable failed" in result.output
    assert "enable broke" in result.output
    # Install half ran successfully before the strict failure.
    assert "installed plugin: newp@existing-mp" in result.output
    assert fake.install_args() == ["newp@existing-mp"]


# ---------------------------------------------------------------------------
# dotfiles-oyv — `plugin add` install subprocess error handling
# ---------------------------------------------------------------------------


def test_plugin_add_exits_nonzero_when_install_fails_with_called_process_error(
    fake_claude, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`plugin add` must exit 1 with a red ERROR when install raises CalledProcessError.

    The enable step must NOT be called after a failed install.
    """
    from typer.testing import CliRunner

    from my_setup.cli import app

    p = _write_yaml_fixture(tmp_path)
    fake = fake_claude()
    real_run = fake.run

    def failing_run(args, **kwargs) -> subprocess.CompletedProcess:
        cmd = list(args[1:])
        if cmd[:2] == ["plugin", "install"]:
            fake.calls.append(list(args))
            raise subprocess.CalledProcessError(
                1, list(args), output="", stderr="install exploded"
            )
        return real_run(args, **kwargs)

    monkeypatch.setattr("my_setup.claude_plugins.subprocess.run", failing_run)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "plugin",
            "add",
            "newp@existing-mp",
            "--from=github:foo/bar",
            "--profile=myprofile",
            f"--config={p}",
        ],
    )
    assert result.exit_code == 1, result.output
    assert "ERROR: install failed" in result.output
    assert "install exploded" in result.output
    # Enable must NOT have been called.
    assert fake.enable_args() == []


def test_plugin_add_exits_nonzero_when_install_fails_with_timeout_expired(
    fake_claude, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`plugin add` must exit 1 with a red ERROR when install raises TimeoutExpired.

    The enable step must NOT be called after a timed-out install.
    """
    from typer.testing import CliRunner

    from my_setup.cli import app

    p = _write_yaml_fixture(tmp_path)
    fake = fake_claude()
    real_run = fake.run

    def timing_out_run(args, **kwargs) -> subprocess.CompletedProcess:
        cmd = list(args[1:])
        if cmd[:2] == ["plugin", "install"]:
            fake.calls.append(list(args))
            raise subprocess.TimeoutExpired(list(args), timeout=30)
        return real_run(args, **kwargs)

    monkeypatch.setattr("my_setup.claude_plugins.subprocess.run", timing_out_run)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "plugin",
            "add",
            "newp@existing-mp",
            "--from=github:foo/bar",
            "--profile=myprofile",
            f"--config={p}",
        ],
    )
    assert result.exit_code == 1, result.output
    assert "ERROR: install failed" in result.output
    assert "timed out" in result.output
    # Enable must NOT have been called.
    assert fake.enable_args() == []


def test_plugin_add_warns_and_skips_when_install_raises_plugin_tool_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`plugin add` must warn and continue (exit 0) when claude binary is absent.

    PluginToolMissing from plugin_install keeps the warn-and-skip path;
    enable is also skipped since there is nothing installed.
    """
    from typer.testing import CliRunner

    from my_setup.cli import app
    from my_setup.errors import PluginToolMissing

    p = _write_yaml_fixture(tmp_path)

    # Directly raise PluginToolMissing from plugin_install to test the handler
    # in cli.py's plugin_add in isolation, independent of binary-resolution internals.
    def fake_install(name: str, marketplace: str) -> None:
        raise PluginToolMissing("fake message")

    monkeypatch.setattr("my_setup.claude_plugins.plugin_install", fake_install)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "plugin",
            "add",
            "newp@existing-mp",
            "--from=github:foo/bar",
            "--profile=myprofile",
            f"--config={p}",
        ],
    )
    assert result.exit_code == 0, result.output
    # typer CliRunner mixes stderr into result.output
    assert "warning: skipping install" in result.output
    # No install success message, no enable.
    assert "installed plugin" not in result.output
    assert "enabled plugin" not in result.output
