"""Tests for Claude plugin & marketplace reconcile orchestration.

``subprocess.run`` is monkeypatched to a fake driver that records every
call and simulates the ``claude plugin`` CLI surface, so tests can
assert on the exact sequence of install/enable/disable invocations
without requiring a real ``claude`` CLI.

Binary resolution is also monkeypatched via
``setforge.claude_plugins.resolve_binary`` to control when the binary
is "found" vs absent.

The shared :class:`FakeClaude` driver, :class:`FakeGit` git fake, and
``fake_claude`` / ``fake_git`` fixtures live in :mod:`tests.conftest`
(setforge-qo23 split); yaml-editor + marketplace-cache helper tests
moved out to :mod:`tests.test_claude_yaml_editor` and
:mod:`tests.test_claude_marketplace_cache` respectively.
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from setforge import claude_plugins as cp
from setforge.config import (
    ClaudePluginRef,
    MarketplaceSource,
    MarketplaceSourceKind,
    ReconcilePolicy,
)
from setforge.errors import ConfigError, PluginToolMissing
from tests.conftest import (
    _local_clone_yaml,
    _make_config,
    _make_resolved,
    _regular_yaml,
)

# ---------------------------------------------------------------------------
# P3.1 — Wrapper tests
# ---------------------------------------------------------------------------


def test_list_marketplaces_returns_dict_keyed_by_name(fake_claude) -> None:
    from setforge.claude_plugins import list_marketplaces

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
    from setforge.claude_plugins import list_installed

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
    from setforge.claude_plugins import plugin_install

    fake = fake_claude()
    plugin_install("cline", "anthropic")
    # Assert --scope=user is present in the call
    install_calls = [c for c in fake.calls if c[1:3] == ["plugin", "install"]]
    assert len(install_calls) == 1
    assert "--scope=user" in install_calls[0]
    assert "cline@anthropic" in install_calls[0]


def test_plugin_enable_synthesises_correct_command(fake_claude) -> None:
    from setforge.claude_plugins import plugin_enable

    fake = fake_claude(plugins=[{"id": "cline@anthropic", "enabled": False}])
    plugin_enable("cline@anthropic")
    assert fake.enable_args() == ["cline@anthropic"]


def test_plugin_disable_synthesises_correct_command(fake_claude) -> None:
    from setforge.claude_plugins import plugin_disable

    fake = fake_claude(plugins=[{"id": "cline@anthropic", "enabled": True}])
    plugin_disable("cline@anthropic")
    assert fake.disable_args() == ["cline@anthropic"]


def test_plugin_uninstall_argv(fake_claude) -> None:
    """``plugin_uninstall`` issues ``claude plugin uninstall <id>``.

    Mirrors :func:`test_plugin_install_passes_scope_user`'s shape: assert
    the argv FakeClaude received and assert the plugin no longer appears
    in ``installed_state()`` afterwards. This is the inverse primitive
    used by ``setforge revert`` to undo a ``PluginDelta.installed`` row.
    """
    from setforge.claude_plugins import plugin_uninstall

    fake = fake_claude(
        plugins=[
            {"id": "cline@anthropic", "enabled": True, "scope": "user"},
            {"id": "ghost@anthropic", "enabled": False, "scope": "user"},
        ]
    )
    plugin_uninstall("cline@anthropic")
    # argv shape: [<claude>, "plugin", "uninstall", "cline@anthropic"]
    uninstall_calls = [c for c in fake.calls if c[1:3] == ["plugin", "uninstall"]]
    assert len(uninstall_calls) == 1
    assert uninstall_calls[0][3] == "cline@anthropic"
    assert fake.uninstall_args() == ["cline@anthropic"]
    # In-memory analog of installed_plugins.json: cline removed, ghost stays.
    assert fake.installed_state() == {
        "ghost@anthropic": {"id": "ghost@anthropic", "enabled": False, "scope": "user"}
    }


def test_missing_claude_binary_raises_plugin_tool_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from setforge.claude_plugins import (
        list_installed,
        list_marketplaces,
        plugin_install,
    )

    cp._get_claude_bin.cache_clear()
    monkeypatch.setattr("setforge.claude_plugins.resolve_binary", lambda _: None)
    with pytest.raises(PluginToolMissing, match="claude"):
        list_installed()
    with pytest.raises(PluginToolMissing, match="claude"):
        list_marketplaces()
    with pytest.raises(PluginToolMissing, match="claude"):
        plugin_install("cline", "anthropic")


def _wire_stdout(monkeypatch: pytest.MonkeyPatch, stdout: str) -> None:
    """Point claude-CLI subprocess.run at a stub returning ``stdout``.

    resolve_binary is forced to a valid path so the wrapper proceeds to
    parse ``result.stdout`` rather than short-circuiting on a missing
    binary; the cache is cleared so the stubbed resolver actually runs.
    """
    cp._get_claude_bin.cache_clear()
    monkeypatch.setattr(
        "setforge.claude_plugins.resolve_binary",
        lambda name: Path("/usr/local/bin/claude") if name == "claude" else None,
    )

    def stub_run(args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr("setforge.claude_plugins.subprocess.run", stub_run)


@pytest.mark.parametrize(
    "bad_stdout",
    [
        "this is not json at all",
        "",
        "{ broken json",
    ],
)
def test_list_marketplaces_non_json_raises_plugin_tool_missing(
    monkeypatch: pytest.MonkeyPatch, bad_stdout: str
) -> None:
    """Non-JSON stdout surfaces PluginToolMissing, not a raw JSONDecodeError."""
    _wire_stdout(monkeypatch, bad_stdout)
    with pytest.raises(PluginToolMissing) as excinfo:
        cp.list_marketplaces()
    assert not isinstance(excinfo.value, json.JSONDecodeError)
    assert isinstance(excinfo.value.__cause__, json.JSONDecodeError)


@pytest.mark.parametrize(
    "bad_stdout",
    [
        "this is not json at all",
        "",
        "{ broken json",
    ],
)
def test_list_installed_non_json_raises_plugin_tool_missing(
    monkeypatch: pytest.MonkeyPatch, bad_stdout: str
) -> None:
    """Non-JSON stdout surfaces PluginToolMissing, not a raw JSONDecodeError."""
    _wire_stdout(monkeypatch, bad_stdout)
    with pytest.raises(PluginToolMissing) as excinfo:
        cp.list_installed()
    assert not isinstance(excinfo.value, json.JSONDecodeError)
    assert isinstance(excinfo.value.__cause__, json.JSONDecodeError)


@pytest.mark.parametrize(
    "wrong_shape",
    [
        '{"name": "anthropic"}',  # JSON object, not a list
        '"anthropic"',  # JSON string
        "42",  # JSON number
    ],
)
def test_list_marketplaces_wrong_shape_raises_plugin_tool_missing(
    monkeypatch: pytest.MonkeyPatch, wrong_shape: str
) -> None:
    """Valid-JSON-but-wrong-shape stdout raises PluginToolMissing, not TypeError.

    A bare JSONDecodeError guard would let a non-list payload through to
    the ``{e["name"]: ...}`` comprehension, raising TypeError/KeyError; the
    list-shape assertion (or the element guard) must surface the clear
    error instead.
    """
    _wire_stdout(monkeypatch, wrong_shape)
    with pytest.raises(PluginToolMissing):
        cp.list_marketplaces()


@pytest.mark.parametrize(
    "wrong_shape",
    [
        '{"id": "cline@anthropic"}',  # JSON object, not a list
        '"cline"',  # JSON string
        "42",  # JSON number
    ],
)
def test_list_installed_wrong_shape_raises_plugin_tool_missing(
    monkeypatch: pytest.MonkeyPatch, wrong_shape: str
) -> None:
    """Valid-JSON-but-wrong-shape stdout raises PluginToolMissing, not TypeError."""
    _wire_stdout(monkeypatch, wrong_shape)
    with pytest.raises(PluginToolMissing):
        cp.list_installed()


def test_get_claude_bin_consults_resolve_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_get_claude_bin() must delegate to resolve_binary, not shutil.which."""
    calls: list[str] = []

    def recording_resolver(name: str) -> Path | None:
        calls.append(name)
        return Path("/custom/claude")

    # Reset cache BEFORE setting the new resolver so the next call
    # actually hits the resolver (not the cached path).
    cp._get_claude_bin.cache_clear()
    monkeypatch.setattr("setforge.claude_plugins.resolve_binary", recording_resolver)
    path = cp._get_claude_bin()
    assert "claude" in calls
    assert str(path) == "/custom/claude"


def test_marketplace_add_calls_correct_args(fake_claude) -> None:
    from setforge.claude_plugins import marketplace_add

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
    from setforge.claude_plugins import marketplace_remove

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
    from setforge.claude_plugins import marketplace_update

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
    from setforge.claude_plugins import reconcile

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

    Primary acceptance gate for setforge-l37: a freshly-declared plugin
    must be both installed AND enabled in a single reconcile run, even
    though `claude plugin install` alone leaves it disabled.
    `to_enable` in the report keeps clean β2 semantics: only the
    original `declared intersect disabled` set, NOT freshly-installed plugins.
    """
    from setforge.claude_plugins import reconcile

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
    from setforge.claude_plugins import reconcile

    fake = fake_claude()
    real_run = fake.run

    def failing_run(args, **kwargs: Any) -> subprocess.CompletedProcess:
        cmd = list(args[1:])
        if cmd == ["plugin", "install", "bad@m1", "--scope=user"]:
            fake.calls.append(list(args))
            raise subprocess.CalledProcessError(
                1, list(args), output="", stderr="install bombed"
            )
        return real_run(args, **kwargs)

    monkeypatch.setattr("setforge.claude_plugins.subprocess.run", failing_run)

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
    ``declared intersect disabled`` path with no new code.
    """
    from setforge.claude_plugins import reconcile

    fake = fake_claude()
    real_run = fake.run

    def failing_run(args, **kwargs: Any) -> subprocess.CompletedProcess:
        cmd = list(args[1:])
        if cmd == ["plugin", "enable", "a@m1"]:
            fake.calls.append(list(args))
            raise subprocess.CalledProcessError(
                1, list(args), output="", stderr="enable bombed"
            )
        return real_run(args, **kwargs)

    monkeypatch.setattr("setforge.claude_plugins.subprocess.run", failing_run)

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
    from setforge.claude_plugins import reconcile

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
    from setforge.claude_plugins import reconcile

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
    from setforge.claude_plugins import reconcile

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
    """declared={a,b}, enabled={a,c}, disabled={b,d} →
    install=[],enable=[b],disable=[c].
    """
    from setforge.claude_plugins import reconcile

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
    from setforge.claude_plugins import reconcile

    cp._get_claude_bin.cache_clear()
    # Monkeypatch resolve_binary to return a valid path
    monkeypatch.setattr(
        "setforge.claude_plugins.resolve_binary",
        lambda name: Path("/usr/local/bin/claude") if name == "claude" else None,
    )

    list_call_count = 0

    def read_only_run(args, **kwargs: Any) -> subprocess.CompletedProcess:
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

    monkeypatch.setattr("setforge.claude_plugins.subprocess.run", read_only_run)
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
    from setforge.claude_plugins import reconcile

    cp._get_claude_bin.cache_clear()
    monkeypatch.setattr(
        "setforge.claude_plugins.resolve_binary",
        lambda name: Path("/usr/local/bin/claude") if name == "claude" else None,
    )

    def read_only_run(args, **kwargs: Any) -> subprocess.CompletedProcess:
        import json

        cmd = args[1:]
        if cmd == ["plugin", "list", "--json"]:
            return subprocess.CompletedProcess(
                args, 0, json.dumps([{"id": "extra@m1", "enabled": True}]), ""
            )
        if cmd == ["plugin", "marketplace", "list", "--json"]:
            return subprocess.CompletedProcess(args, 0, json.dumps([]), "")
        raise AssertionError(f"dry_run must not invoke write command: {args!r}")

    monkeypatch.setattr("setforge.claude_plugins.subprocess.run", read_only_run)
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
    from setforge.claude_plugins import reconcile

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
    from setforge.claude_plugins import reconcile

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
    from setforge.claude_plugins import reconcile

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
    from setforge.claude_plugins import reconcile

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
    from setforge.claude_plugins import reconcile

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
    from setforge.claude_plugins import reconcile

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
    from setforge.claude_plugins import reconcile

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
# P3.4 — --claude-bin wiring + reconcile dry-run
# ---------------------------------------------------------------------------
#
# YAML editor tests for the ``yaml_add_*`` / ``yaml_remove_*`` verbs
# moved to :mod:`tests.test_claude_yaml_editor` (setforge-qo23). The
# fixture below is retained because the setforge-l37 / setforge-oyv
# ``plugin add`` CLI tests still need a config-file fixture to mutate.

_YAML_FIXTURE = """\
version: 1

# Top-level comment.
tracked_files:
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
    tracked_files:
      - d
    claude_plugins:
      - existing-plugin
  bare:
    tracked_files:
      - d
"""


def _write_yaml_fixture(tmp_path: Path) -> Path:
    p = tmp_path / "setforge.yaml"
    p.write_text(_YAML_FIXTURE, encoding="utf-8")
    return p


def test_claude_bin_override_flows_through_set_cli_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """--claude-bin flag must call binaries.set_cli_overrides(claude=...)."""
    from typer.testing import CliRunner

    import setforge.binaries as binaries_mod
    from setforge.cli import app

    calls: list[dict] = []
    original_set_cli_overrides = binaries_mod.set_cli_overrides

    def recording_set_cli_overrides(
        *,
        code: str | None = None,
        claude: str | None = None,
        gitleaks: str | None = None,
        patch: str | None = None,
    ) -> None:
        calls.append(
            {"code": code, "claude": claude, "gitleaks": gitleaks, "patch": patch}
        )
        # Reset claude_bin cache after override change
        cp._get_claude_bin.cache_clear()
        original_set_cli_overrides(
            code=code, claude=claude, gitleaks=gitleaks, patch=patch
        )

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
    from setforge.claude_plugins import reconcile

    cp._get_claude_bin.cache_clear()
    monkeypatch.setattr(
        "setforge.claude_plugins.resolve_binary",
        lambda name: Path("/usr/local/bin/claude") if name == "claude" else None,
    )

    def read_only_run(args, **kwargs: Any) -> subprocess.CompletedProcess:
        import json

        cmd = args[1:]
        if cmd in (
            ["plugin", "list", "--json"],
            ["plugin", "marketplace", "list", "--json"],
        ):
            return subprocess.CompletedProcess(args, 0, json.dumps([]), "")
        raise AssertionError(f"REPORT mode must not invoke write command: {args!r}")

    monkeypatch.setattr("setforge.claude_plugins.subprocess.run", read_only_run)
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
# setforge-l37 — `plugin add` strict enable behavior
# ---------------------------------------------------------------------------


def test_plugin_add_calls_enable_after_install(fake_claude, tmp_path: Path) -> None:
    """`plugin add` must run `plugin enable` after a successful install.

    Mirrors the reconcile-path fix at the CLI surface: a freshly added
    plugin should be active in a single command invocation, not two.
    """
    from typer.testing import CliRunner

    from setforge.cli import app

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

    from setforge.cli import app

    p = _write_yaml_fixture(tmp_path)
    fake = fake_claude()
    real_run = fake.run

    def failing_run(args, **kwargs: Any) -> subprocess.CompletedProcess:
        cmd = list(args[1:])
        if cmd == ["plugin", "enable", "newp@existing-mp"]:
            fake.calls.append(list(args))
            raise subprocess.CalledProcessError(
                1, list(args), output="", stderr="enable broke"
            )
        return real_run(args, **kwargs)

    monkeypatch.setattr("setforge.claude_plugins.subprocess.run", failing_run)

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
# setforge-oyv — `plugin add` install subprocess error handling
# ---------------------------------------------------------------------------


def test_plugin_add_exits_nonzero_when_install_fails_with_called_process_error(
    fake_claude, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`plugin add` must exit 1 with a red ERROR when install raises CalledProcessError.

    The enable step must NOT be called after a failed install.
    """
    from typer.testing import CliRunner

    from setforge.cli import app

    p = _write_yaml_fixture(tmp_path)
    fake = fake_claude()
    real_run = fake.run

    def failing_run(args, **kwargs: Any) -> subprocess.CompletedProcess:
        cmd = list(args[1:])
        if cmd[:2] == ["plugin", "install"]:
            fake.calls.append(list(args))
            raise subprocess.CalledProcessError(
                1, list(args), output="", stderr="install exploded"
            )
        return real_run(args, **kwargs)

    monkeypatch.setattr("setforge.claude_plugins.subprocess.run", failing_run)

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

    from setforge.cli import app

    p = _write_yaml_fixture(tmp_path)
    fake = fake_claude()
    real_run = fake.run

    def timing_out_run(args, **kwargs: Any) -> subprocess.CompletedProcess:
        cmd = list(args[1:])
        if cmd[:2] == ["plugin", "install"]:
            fake.calls.append(list(args))
            raise subprocess.TimeoutExpired(list(args), timeout=30)
        return real_run(args, **kwargs)

    monkeypatch.setattr("setforge.claude_plugins.subprocess.run", timing_out_run)

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

    from setforge.cli import app
    from setforge.errors import PluginToolMissing

    p = _write_yaml_fixture(tmp_path)

    # Directly raise PluginToolMissing from plugin_install to test the handler
    # in cli.py's plugin_add in isolation, independent of binary-resolution internals.
    def fake_install(name: str, marketplace: str) -> None:
        raise PluginToolMissing("fake message")

    monkeypatch.setattr("setforge.claude_plugins.plugin_install", fake_install)

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


# ---------------------------------------------------------------------------
# setforge-nen.13 — PluginDelta in transition records + revert inverse
# ---------------------------------------------------------------------------
#
# These tests exercise the install → transition-record → revert round-trip
# end-to-end via the CliRunner, asserting that:
#
# 1. ``install`` writes a ``plugins.json`` sidecar capturing the successful
#    reconcile actions (installed / enabled / disabled + marketplace
#    add/remove).
# 2. ``revert`` reads that sidecar and applies the inverse via
#    ``plugin_uninstall`` / ``plugin_disable`` / ``plugin_enable`` /
#    ``marketplace_remove`` / ``marketplace_add``.
# 3. Round-trip is total: FakeClaude's ``installed_state()`` after revert
#    matches the pre-install state.
#
# Each test uses ``SETFORGE_STATE_DIR`` to redirect the transition state
# into ``tmp_path`` (per ``test_cli_e2e.py:77``) so the host's real
# state dir is untouched.

# E2E fixture mirrors the layout used by tests/test_cli_e2e.py — a copy
# of the full fixture YAML + tracked tree under tmp_path so tracked_file
# srcs resolve.
_E2E_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "e2e"
_E2E_FIXTURE_YAML = _E2E_FIXTURE_DIR / "setforge.test.yaml"
_E2E_FIXTURE_TRACKED = _E2E_FIXTURE_DIR / "tracked"


def _copy_e2e_fixture(tmp_path: Path) -> Path:
    """Materialize the e2e fixture inside ``tmp_path`` and return the
    yaml path. Mirror of ``test_cli_e2e.fixture_repo``."""
    target = tmp_path / "repo"
    target.mkdir()
    shutil.copy2(_E2E_FIXTURE_YAML, target / "setforge.test.yaml")
    shutil.copytree(_E2E_FIXTURE_TRACKED, target / "tracked")
    return target / "setforge.test.yaml"


def _sandbox_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """Redirect ``$HOME`` and ``$SETFORGE_STATE_DIR`` for an install/revert
    round-trip. Returns ``(home_dir, state_dir)``."""
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    return home, state


def _latest_transition(state_dir: Path) -> Path:
    """Return the only transition directory under ``state_dir/transitions``."""
    transitions_root = state_dir / "transitions"
    candidates = sorted(
        d
        for d in transitions_root.iterdir()
        if d.is_dir() and not d.name.startswith(".pending-")
    )
    assert candidates, f"expected at least one transition in {transitions_root}"
    return candidates[-1]


def test_install_records_plugin_delta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_claude,
) -> None:
    """``install`` writes ``plugins.json`` capturing the reconcile actions.

    Fixture: ``test-comprehensive`` profile declares
    ``superpowers@claude-plugins-official``; FakeClaude starts empty so
    reconcile installs + enables the plugin AND registers the
    marketplace. The resulting ``plugins.json`` payload should reflect
    one install + one marketplace add (enable is the install-loop's
    post-step, not a standalone enable transition).
    """
    from typer.testing import CliRunner

    from setforge.cli import app

    fixture_yaml = _copy_e2e_fixture(tmp_path)
    _, state_dir = _sandbox_state_dir(tmp_path, monkeypatch)
    fake_claude()  # empty marketplaces + empty installed_plugins

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["install", "--profile=test-comprehensive", f"--config={fixture_yaml}"],
    )
    assert result.exit_code == 0, result.output

    transition_dir = _latest_transition(state_dir)
    plugins_file = transition_dir / "plugins.json"
    assert plugins_file.exists(), (
        f"expected plugins.json sidecar at {plugins_file}; transition dir "
        f"contains: {sorted(p.name for p in transition_dir.iterdir())}"
    )
    payload = json.loads(plugins_file.read_text(encoding="utf-8"))
    # Schema check: all five PluginDelta fields present as JSON arrays.
    assert set(payload.keys()) == {
        "installed",
        "enabled",
        "disabled",
        "marketplaces_added",
        "marketplaces_removed",
    }
    assert payload["installed"] == ["superpowers@claude-plugins-official"]
    # The fresh-install enable is the install loop's post-step — it does
    # NOT count as a standalone ``enabled`` transition. β2 semantics.
    assert payload["enabled"] == []
    assert payload["disabled"] == []
    assert payload["marketplaces_added"] == ["claude-plugins-official"]
    assert payload["marketplaces_removed"] == []


def test_install_records_plugin_delta_with_enable_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_claude,
) -> None:
    """When a declared plugin is already installed-but-disabled,
    reconcile flips it to enabled and ``plugins.json`` records the
    enable transition (NOT install — the plugin was already installed).
    """
    from typer.testing import CliRunner

    from setforge.cli import app

    fixture_yaml = _copy_e2e_fixture(tmp_path)
    _, state_dir = _sandbox_state_dir(tmp_path, monkeypatch)
    # Pre-seed FakeClaude: marketplace already registered + plugin
    # already installed but disabled.
    fake_claude(
        marketplaces=[
            {
                "name": "claude-plugins-official",
                "source": "anthropics/claude-plugins-official",
            }
        ],
        plugins=[
            {
                "id": "superpowers@claude-plugins-official",
                "enabled": False,
                "scope": "user",
            }
        ],
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["install", "--profile=test-comprehensive", f"--config={fixture_yaml}"],
    )
    assert result.exit_code == 0, result.output

    payload = json.loads(
        (_latest_transition(state_dir) / "plugins.json").read_text(encoding="utf-8")
    )
    assert payload["installed"] == []
    assert payload["enabled"] == ["superpowers@claude-plugins-official"]
    assert payload["disabled"] == []
    # Marketplace already present → no add.
    assert payload["marketplaces_added"] == []


def test_install_records_plugin_delta_with_enable_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_claude,
) -> None:
    """Install-succeeds-then-enable-fails MUST still record the pid in
    PluginDelta.installed (disk state is ground truth) and revert MUST
    uninstall it.

    Regression test for the Phase 5 specifics-review finding: the old
    ``failed_plugin_ids`` filter collapsed install-failures and enable-
    failures into one set and excluded both — leaving a plugin on disk
    but missing from ``PluginDelta.installed``, so revert orphaned it.
    The disk-state pre/post snapshot approach captures the plugin
    correctly because fake-claude's ``installed_state()`` reflects that
    ``plugin_install`` succeeded (the entry is present, just disabled).
    """
    from typer.testing import CliRunner

    from setforge.cli import app

    fixture_yaml = _copy_e2e_fixture(tmp_path)
    _, state_dir = _sandbox_state_dir(tmp_path, monkeypatch)
    fc = fake_claude()
    real_run = fc.run

    # Fail ``plugin enable`` for the test-comprehensive plugin, letting
    # install + marketplace add proceed normally. Mirrors
    # test_reconcile_fresh_install_succeeds_then_enable_fails_records_failure.
    def failing_run(args, **kwargs: Any) -> subprocess.CompletedProcess:
        cmd = list(args[1:])
        if cmd == ["plugin", "enable", "superpowers@claude-plugins-official"]:
            fc.calls.append(list(args))
            raise subprocess.CalledProcessError(
                1, list(args), output="", stderr="enable bombed"
            )
        return real_run(args, **kwargs)

    monkeypatch.setattr("setforge.claude_plugins.subprocess.run", failing_run)

    runner = CliRunner()
    # --yes short-circuits the per-item failure prompt added in
    # setforge-k0uj to its default (SKIP), preserving the historic
    # warn-and-continue behavior this regression test exercises.
    installed = runner.invoke(
        app,
        [
            "install",
            "--profile=test-comprehensive",
            f"--config={fixture_yaml}",
            "--yes",
        ],
    )
    # Install command exits 0 even when a reconcile step fails (warn-
    # and-continue with default SKIP), so the transition still lands.
    assert installed.exit_code == 0, installed.output

    # Plugin landed on disk per fake-claude (install succeeded).
    assert "superpowers@claude-plugins-official" in fc.installed_state()
    # But it stayed disabled because the enable step raised.
    entry = fc.installed_state()["superpowers@claude-plugins-official"]
    assert entry["enabled"] is False

    payload = json.loads(
        (_latest_transition(state_dir) / "plugins.json").read_text(encoding="utf-8")
    )
    # Disk-state ground truth: the pid IS recorded as installed even
    # though its enable step failed. This is the invariant the fix
    # establishes.
    assert payload["installed"] == ["superpowers@claude-plugins-official"]
    # Enable did not flip the bit (failed), so ``enabled`` is empty.
    assert payload["enabled"] == []
    assert payload["disabled"] == []
    # Marketplace add succeeded so it appears in the delta.
    assert payload["marketplaces_added"] == ["claude-plugins-official"]
    assert payload["marketplaces_removed"] == []

    # Round-trip: revert MUST uninstall the plugin (no orphan). Clear
    # the failing-run override first so revert's uninstall succeeds.
    monkeypatch.setattr("setforge.claude_plugins.subprocess.run", real_run)
    reverted = runner.invoke(
        app,
        ["revert", "--profile=test-comprehensive", f"--config={fixture_yaml}", "--yes"],
    )
    assert reverted.exit_code == 0, reverted.output
    assert fc.uninstall_args() == ["superpowers@claude-plugins-official"]
    # Plugin no longer on disk — orphan averted.
    assert "superpowers@claude-plugins-official" not in fc.installed_state()


def test_revert_restores_plugin_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_claude,
) -> None:
    """install → revert: FakeClaude's installed_state matches pre-install
    bytes (empty), and the marketplace registration is reversed.
    """
    from typer.testing import CliRunner

    from setforge.cli import app

    fixture_yaml = _copy_e2e_fixture(tmp_path)
    _, _state_dir = _sandbox_state_dir(tmp_path, monkeypatch)
    fc = fake_claude()  # pre: empty marketplaces + empty plugins
    pre_state = fc.installed_state()
    pre_marketplaces = fc.marketplaces_state()

    runner = CliRunner()
    installed = runner.invoke(
        app,
        ["install", "--profile=test-comprehensive", f"--config={fixture_yaml}"],
    )
    assert installed.exit_code == 0, installed.output
    # Sanity: install actually mutated state.
    assert "superpowers@claude-plugins-official" in fc.installed_state()

    reverted = runner.invoke(
        app,
        ["revert", "--profile=test-comprehensive", f"--config={fixture_yaml}", "--yes"],
    )
    assert reverted.exit_code == 0, reverted.output
    # plugin uninstalled + marketplace removed.
    assert fc.uninstall_args() == ["superpowers@claude-plugins-official"]
    assert fc.installed_state() == pre_state
    assert fc.marketplaces_state() == pre_marketplaces


def test_revert_noop_when_no_plugin_delta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``plugins.json`` is absent (claude was warn-skipped on install),
    revert is a no-op for plugins — file state still reverses.

    Makes the claude binary unresolvable so install's plugin reconcile
    leg is warn-and-skipped and no ``plugins.json`` sidecar lands.
    Revert sees only ``changes.patch`` and reverses the file state
    successfully. (Inline equivalent of ``test_cli_e2e``'s
    ``no_claude_bin`` fixture, which isn't auto-discovered from this
    module.)
    """
    from typer.testing import CliRunner

    from setforge.cli import app

    cp._get_claude_bin.cache_clear()
    monkeypatch.setattr(
        "setforge.claude_plugins.resolve_binary",
        lambda _name: None,
    )
    fixture_yaml = _copy_e2e_fixture(tmp_path)
    home, state_dir = _sandbox_state_dir(tmp_path, monkeypatch)

    runner = CliRunner()
    installed = runner.invoke(
        app,
        ["install", "--profile=test-minimal", f"--config={fixture_yaml}"],
    )
    assert installed.exit_code == 0, installed.output

    transition_dir = _latest_transition(state_dir)
    assert not (transition_dir / "plugins.json").exists()
    live = home / ".setforge_e2e" / "minimal" / "text.txt"
    assert live.exists()

    reverted = runner.invoke(
        app,
        ["revert", "--profile=test-minimal", f"--config={fixture_yaml}", "--yes"],
    )
    assert reverted.exit_code == 0, reverted.output
    # File state reversed (file was created on install, so revert deletes it).
    assert not live.exists()
    # No plugins.json in the reverse transition either (no plugins touched).
    new_transition = _latest_transition(state_dir)
    assert not (new_transition / "plugins.json").exists()


def test_revert_partial_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_claude,
) -> None:
    """When one inverse op fails (e.g. uninstall errors), the remaining
    inverses still apply and the reverse_plugin_delta reflects only the
    successes.

    Strategy: install (lands plugin + marketplace), then monkeypatch
    ``subprocess.run`` to fail on ``plugin uninstall`` only. Revert
    should still succeed at ``marketplace remove`` and write a
    ``plugins.json`` whose ``installed`` field is empty (uninstall
    failed) but ``marketplaces_added`` contains the removed marketplace.
    """
    from typer.testing import CliRunner

    from setforge.cli import app

    fixture_yaml = _copy_e2e_fixture(tmp_path)
    _, state_dir = _sandbox_state_dir(tmp_path, monkeypatch)
    fc = fake_claude()
    real_run = fc.run

    runner = CliRunner()
    installed = runner.invoke(
        app,
        ["install", "--profile=test-comprehensive", f"--config={fixture_yaml}"],
    )
    assert installed.exit_code == 0, installed.output

    # Now install ``failing_run`` that blows up on uninstall but lets
    # everything else (marketplace remove, list, etc.) pass through.
    def failing_run(args, **kwargs: Any) -> subprocess.CompletedProcess:
        cmd = list(args[1:])
        if cmd[:2] == ["plugin", "uninstall"]:
            fc.calls.append(list(args))
            raise subprocess.CalledProcessError(
                1, list(args), output="", stderr="uninstall blew up"
            )
        return real_run(args, **kwargs)

    monkeypatch.setattr("setforge.claude_plugins.subprocess.run", failing_run)

    reverted = runner.invoke(
        app,
        ["revert", "--profile=test-comprehensive", f"--config={fixture_yaml}", "--yes"],
    )
    # Revert still exits 0 — partial failure is warn-and-continue.
    assert reverted.exit_code == 0, reverted.output

    # The reverse transition records only what succeeded: marketplace
    # was removed (the inverse of ``marketplaces_added``), but plugin
    # uninstall failed so ``installed`` is empty in the reverse delta.
    reverse_transition = _latest_transition(state_dir)
    reverse_payload = json.loads(
        (reverse_transition / "plugins.json").read_text(encoding="utf-8")
    )
    # Reverse delta semantics: ``installed`` lists plugins the reverse
    # uninstalled. Empty here because the uninstall raised.
    assert reverse_payload["installed"] == []
    # ``marketplaces_added`` lists marketplaces the reverse removed
    # (the inverse name for the forward direction). Marketplace remove
    # succeeded.
    assert reverse_payload["marketplaces_added"] == ["claude-plugins-official"]


def test_roundtrip_file_and_plugin_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_claude,
) -> None:
    """Integration: after install→revert, file state AND FakeClaude state
    both match the pre-install bytes.

    This is the load-bearing acceptance: revert must converge full
    external state, not just file content (the gap setforge-nen.13
    closes).
    """
    from typer.testing import CliRunner

    from setforge.cli import app

    fixture_yaml = _copy_e2e_fixture(tmp_path)
    home, _state_dir = _sandbox_state_dir(tmp_path, monkeypatch)
    fc = fake_claude()

    # Pre-install snapshot: file state + plugin state.
    live_root = home / ".setforge_e2e" / "comprehensive"
    assert not live_root.exists()
    pre_plugin_state = fc.installed_state()
    pre_marketplaces = fc.marketplaces_state()

    runner = CliRunner()
    installed = runner.invoke(
        app,
        ["install", "--profile=test-comprehensive", f"--config={fixture_yaml}"],
    )
    assert installed.exit_code == 0, installed.output

    # Sanity: install mutated both axes.
    assert live_root.exists()
    assert fc.installed_state() != pre_plugin_state

    reverted = runner.invoke(
        app,
        ["revert", "--profile=test-comprehensive", f"--config={fixture_yaml}", "--yes"],
    )
    assert reverted.exit_code == 0, reverted.output

    # File state reversed — the comprehensive dir was created from
    # absence on install, so revert deletes its contents. (``revert``
    # only reverses files it touched on install; bootstrap stubs may
    # survive but the tracked_file content does not.)
    notes = live_root / "notes.md"
    assert not notes.exists() or notes.read_text() == ""
    # Plugin state reversed.
    assert fc.installed_state() == pre_plugin_state
    assert fc.marketplaces_state() == pre_marketplaces


# ---------------------------------------------------------------------------
# nen.15 — local-clone install mode integration (reconcile dispatch)
# ---------------------------------------------------------------------------
#
# Pure marketplace-cache helper tests and sync-cache tests moved to
# :mod:`tests.test_claude_marketplace_cache` (setforge-qo23). The
# integration cases below stay here because they exercise
# ``reconcile()`` end-to-end (claude_plugins orchestration crossing into
# the marketplace-cache module via :func:`resolve_marketplace_source`).


def test_reconcile_local_clone_swaps_source_before_marketplace_add(
    fake_claude, fake_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LOCAL_CLONE mode: reconcile calls marketplace_add with a PATH source."""
    from setforge.claude_plugins import reconcile

    _local_clone_yaml(tmp_path, monkeypatch)
    fc = fake_claude()
    fake_git(known_repos={"anthropic/plug"})
    cfg = _make_config(
        marketplaces={
            "anthropic": MarketplaceSource(
                source=MarketplaceSourceKind.GITHUB, repo="anthropic/plug"
            )
        },
        claude_plugins={"a": ClaudePluginRef(marketplace="anthropic")},
    )
    profile = _make_resolved(claude_plugins=["a"])
    reconcile(cfg, profile)
    # marketplace add was called with the local cache path, not the repo
    mp_add = fc.mp_add_args()
    assert len(mp_add) == 1
    assert mp_add[0].endswith("/plug")
    assert "anthropic/plug" not in mp_add[0]  # not the github short form


def test_reconcile_regular_mode_install_mode_unchanged(
    fake_claude, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """REGULAR mode (default): reconcile preserves today's owner/repo argv."""
    from setforge.claude_plugins import reconcile

    _regular_yaml(tmp_path, monkeypatch)
    fc = fake_claude()
    cfg = _make_config(
        marketplaces={
            "anthropic": MarketplaceSource(
                source=MarketplaceSourceKind.GITHUB, repo="anthropic/plug"
            )
        },
        claude_plugins={"a": ClaudePluginRef(marketplace="anthropic")},
    )
    profile = _make_resolved(claude_plugins=["a"])
    reconcile(cfg, profile)
    # Source argv = the github short form, no swap occurred
    mp_add = fc.mp_add_args()
    assert mp_add == ["anthropic/plug"]


def test_local_clone_repeat_install_is_offline(
    fake_claude, fake_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Marker test (spec acceptance #7): second install runs zero git calls.

    With LOCAL_CLONE mode and a pre-populated cache, reconcile must not
    invoke git on the second install — the cache hit short-circuits the
    network. Asserts ``fake_git.clone_count() == 0`` after the second
    reconcile, AND that no git argv whatsoever was issued (no `fetch`,
    no `reset` — sync-cache is the only refresh surface).
    """
    from setforge.claude_plugins import reconcile

    _local_clone_yaml(tmp_path, monkeypatch)
    fake_claude()
    fake = fake_git(known_repos={"anthropic/plug"})
    cfg = _make_config(
        marketplaces={
            "anthropic": MarketplaceSource(
                source=MarketplaceSourceKind.GITHUB, repo="anthropic/plug"
            )
        },
        claude_plugins={"a": ClaudePluginRef(marketplace="anthropic")},
    )
    profile = _make_resolved(claude_plugins=["a"])
    # First reconcile: clones the marketplace.
    reconcile(cfg, profile)
    assert fake.clone_count() == 1
    # Drop git calls so the second reconcile's assertion is precise.
    fake.calls.clear()
    # Reset claude state so reconcile sees the marketplace as already added.
    # FakeClaude already records that — second reconcile recomputes the diff.
    # Recompute: marketplace `plug` is now in fc.marketplaces_state()
    # (FakeClaude derives the name from the URL basename; we used `/plug`).
    # Skip the assertion on FakeClaude state — we only care that NO git
    # invocation runs.
    # We need to ensure the marketplace add path is short-circuited; the
    # test relies on FakeClaude reporting the marketplace as already
    # installed. Since FakeClaude records the marketplace under the URL
    # basename (here: `plug`), but `cfg.marketplaces` uses the YAML key
    # `anthropic`, the diff will still consider `anthropic` not-present
    # and call `marketplace_add` again. That's harmless for the git
    # assertion — marketplace_add itself doesn't talk to git; the swap
    # site uses the existing cache without re-cloning.
    reconcile(cfg, profile)
    assert fake.clone_count() == 0, (
        f"expected zero git clones on repeat install, got "
        f"{[c for c in fake.calls if c[1:2] == ['clone']]}"
    )


# ---------------------------------------------------------------------------
# Post-probe TOCTOU: claude binary vanishes between pre- and post-probe
# ---------------------------------------------------------------------------


def test_reconcile_plugins_post_probe_tool_missing_keeps_landed_delta(
    fake_claude, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Binary disappearing after reconcile must not propagate, nor drop the delta.

    reconcile() + _emit_plugin_report() already mutated disk before the
    post-probe runs. If the post-probe raises PluginToolMissing (TOCTOU:
    binary removed / PATH changed mid-run), _reconcile_plugins must NOT
    re-raise and must NOT return the pre-probe abort shape (None, ()) —
    that would silently discard the just-landed install. The returned
    delta must reflect what reconcile executed.
    """
    import setforge.cli._plugin_helpers as ph

    fake_claude()
    real_installed = cp.list_installed
    real_marketplaces = cp.list_marketplaces
    real_emit = ph._emit_plugin_report
    # ``reconcile`` calls list_installed / list_marketplaces internally
    # (before _emit_plugin_report) and the pre-probe runs before that, so a
    # naive call counter can't isolate the post-probe. Gate the failure on a
    # flag flipped once the report has been emitted — i.e. once reconcile has
    # already mutated disk — so ONLY the two post-probe calls raise.
    state = {"after_reconcile": False}

    def gated_emit(report: Any) -> None:
        real_emit(report)
        state["after_reconcile"] = True

    def probing_list_installed() -> dict[str, dict]:
        if state["after_reconcile"]:
            raise PluginToolMissing("claude vanished mid-run")
        return real_installed()

    def probing_list_marketplaces() -> dict[str, dict]:
        if state["after_reconcile"]:
            raise PluginToolMissing("claude vanished mid-run")
        return real_marketplaces()

    monkeypatch.setattr(ph, "_emit_plugin_report", gated_emit)
    monkeypatch.setattr(cp, "list_installed", probing_list_installed)
    monkeypatch.setattr(cp, "list_marketplaces", probing_list_marketplaces)

    cfg = _make_config(claude_plugins={"a": ClaudePluginRef(marketplace="m1")})
    profile = _make_resolved(
        claude_plugins=["a"], plugins_reconcile=ReconcilePolicy.ADDITIVE
    )

    delta, outcomes = ph._reconcile_plugins(cfg, profile)

    # No unhandled exception reached the caller; the post-probe raise on
    # BOTH list_installed and list_marketplaces was absorbed.
    # The landed install is preserved (not the pre-probe (None, ()) abort).
    assert delta is not None, "post-probe failure must not discard the landed delta"
    assert "a@m1" in delta.installed
    # Return tuple shape matches the contract: (PluginDelta | None, tuple[...]).
    assert isinstance(outcomes, tuple)
    landed_ids = {o.item_id for o in outcomes}
    assert "a@m1" in landed_ids
