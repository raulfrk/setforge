"""An installed plugin the recovery journal cannot describe must not break install.

``claude plugin list`` can name a plugin whose marketplace ``claude plugin
marketplace list`` does not (or an id that is not ``NAME@MARKETPLACE``). Install
records the plugin inventory so a failed install can put it back; that record
only holds plugins recovery could reinstall. Such a plugin used to stop every
install with a raw ``ValueError``. Now an install that does not touch the plugin
goes ahead and a rollback leaves the plugin installed; one that would change it,
or register its marketplace, stops before changing anything, with an error that
names the plugin.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge import operations
from setforge.cli import app
from setforge.errors import SetforgeError
from tests.conftest import FakeClaude
from tests.shared_helpers import write_setforge_yaml

_YAML = """\
version: 1
tracked_files:
  d:
    src: x
    dst: ~/.setforge-test/y
marketplaces:
  mp1: {source: github, repo: o/mp1}
  mp2: {source: github, repo: o/mp2}
claude_plugins:
  review: {marketplace: mp1}
  extra: {marketplace: mp1}
packages:
  review: {type: plugin, plugin: review}
  extra: {type: plugin, plugin: extra}
profiles:
  p:
    tracked_files: [d]
    packages: [review, extra]
"""
_PRUNE = "    reconcile: {plugins: {policy: prune}}\n"

_ODD_PLUGINS = [
    pytest.param({"id": "stray@ghost", "enabled": True}, id="unregistered-marketplace"),
    pytest.param({"id": "stray", "enabled": True}, id="no-marketplace-part"),
    pytest.param({"id": "stray@", "enabled": True}, id="empty-marketplace"),
    pytest.param({"id": "@ghost", "enabled": True}, id="empty-name"),
    pytest.param({"id": "silent@mp1", "scope": "user"}, id="no-enabled-field"),
]


def _install(tmp_path: Path, *, policy: str = "") -> Result:
    cfg = write_setforge_yaml(tmp_path, _YAML + policy)
    (tmp_path / "tracked").mkdir(exist_ok=True)
    (tmp_path / "tracked" / "x").write_text("x\n", encoding="utf-8")
    return CliRunner().invoke(
        app,
        [
            "install",
            "--profile=p",
            f"--config={cfg}",
            "--no-fetch",
            "--no-secrets-scan",
            "--no-git-check",
            "--yes",
        ],
    )


def _claude(
    fake_claude: Callable[..., FakeClaude], odd: dict[str, object]
) -> FakeClaude:
    return fake_claude(
        marketplaces=[{"name": "mp1", "source": "o/mp1"}],
        plugins=[{"id": "review@mp1", "enabled": True, "scope": "user"}, dict(odd)],
    )


@pytest.mark.parametrize("odd", _ODD_PLUGINS)
def test_install_that_does_not_touch_the_plugin_goes_ahead(
    tmp_path: Path, fake_claude: Callable[..., FakeClaude], odd: dict[str, object]
) -> None:
    claude = _claude(fake_claude, odd)

    result = _install(tmp_path)

    assert result.exit_code == 0, (result.output, result.exception)
    installed = claude.installed_state()
    assert installed["extra@mp1"]["enabled"] is True
    assert installed[str(odd["id"])] == odd
    assert claude.uninstall_args() == []
    assert claude.mp_add_args() == ["o/mp2"]


@pytest.mark.parametrize("odd", _ODD_PLUGINS)
def test_failed_install_rolls_plugin_changes_back_and_keeps_the_plugin(
    tmp_path: Path,
    fake_claude: Callable[..., FakeClaude],
    monkeypatch: pytest.MonkeyPatch,
    odd: dict[str, object],
) -> None:
    claude = _claude(fake_claude, odd)

    def fail_after_plugins(*_args: object, **_kwargs: object) -> Path:
        raise RuntimeError("write failed after the plugins were reconciled")

    monkeypatch.setattr(
        "setforge.cli.install._write_install_transition", fail_after_plugins
    )

    result = _install(tmp_path)

    assert result.exit_code == 1, result.output
    assert claude.install_args() == ["extra@mp1"]
    assert claude.installed_state() == {
        "review@mp1": {"id": "review@mp1", "enabled": True, "scope": "user"},
        str(odd["id"]): odd,
    }
    assert [m["name"] for m in claude.marketplaces_state()] == ["mp1"]
    assert operations.active("p") is None


@pytest.mark.parametrize("odd", _ODD_PLUGINS[:4])
def test_install_that_would_change_the_plugin_stops_before_changing_anything(
    tmp_path: Path, fake_claude: Callable[..., FakeClaude], odd: dict[str, object]
) -> None:
    claude = _claude(fake_claude, odd)
    claude_calls_before = len(claude.calls)

    result = _install(tmp_path, policy=_PRUNE)

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SetforgeError), result.exception
    assert f"plugin {odd['id']!r} is installed" in str(result.exception)
    assert f"claude plugin uninstall {odd['id']}" in str(result.exception)
    assert {tuple(call[1:]) for call in claude.calls[claude_calls_before:]} <= {
        ("plugin", "list", "--json"),
        ("plugin", "marketplace", "list", "--json"),
    }
    assert claude.installed_state()[str(odd["id"])] == odd
    assert operations.active("p") is None
    assert not (Path.home() / ".setforge-test" / "y").exists()


def test_install_that_would_register_the_plugins_marketplace_stops_first(
    tmp_path: Path, fake_claude: Callable[..., FakeClaude]
) -> None:
    claude = _claude(fake_claude, {"id": "stray@ghost", "enabled": True})
    cfg = write_setforge_yaml(
        tmp_path,
        _YAML.replace(
            "claude_plugins:",
            "  ghost: {source: github, repo: o/ghost}\nclaude_plugins:",
        ),
    )
    (tmp_path / "tracked").mkdir()
    (tmp_path / "tracked" / "x").write_text("x\n", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "install",
            "--profile=p",
            f"--config={cfg}",
            "--no-fetch",
            "--no-secrets-scan",
            "--no-git-check",
            "--yes",
        ],
    )

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SetforgeError), result.exception
    assert "'ghost' is not registered" in str(result.exception)
    assert claude.mp_add_args() == []
    assert operations.active("p") is None


def test_baseline_leaves_out_a_plugin_recovery_could_not_reinstall() -> None:
    marketplaces: dict[str, dict[str, object]] = {
        "mp1": {"name": "mp1", "source": "o/mp1"}
    }
    plugins = {
        "ok@mp1": {"id": "ok@mp1", "enabled": True},
        "stray@ghost": {"id": "stray@ghost", "enabled": True},
        "stray": {"id": "stray", "enabled": True},
    }

    baseline = operations.plugin_recovery_baseline(
        plugins, marketplaces, touched=set(), marketplaces_added=()
    )

    assert baseline == {
        "plugins": {"ok@mp1": {"id": "ok@mp1", "enabled": True}},
        "marketplaces": marketplaces,
    }
    operations._validate_plugin_payload(json.loads(json.dumps(baseline)))


def test_baseline_records_a_missing_enabled_state_as_not_enabled() -> None:
    marketplaces: dict[str, dict[str, object]] = {
        "mp1": {"name": "mp1", "source": "o/mp1"}
    }

    baseline = operations.plugin_recovery_baseline(
        {"silent@mp1": {"id": "silent@mp1"}},
        marketplaces,
        touched=set(),
        marketplaces_added=(),
    )

    assert baseline["plugins"] == {"silent@mp1": {"id": "silent@mp1", "enabled": False}}
    operations._validate_plugin_payload(baseline)


def test_baseline_refuses_a_plugin_the_operation_would_change() -> None:
    with pytest.raises(SetforgeError) as raised:
        operations.plugin_recovery_baseline(
            {"stray@ghost": {"id": "stray@ghost", "enabled": True}},
            {"mp1": {"name": "mp1", "source": "o/mp1"}},
            touched={"stray@ghost"},
            marketplaces_added=(),
        )

    message = str(raised.value)
    assert "stray@ghost" in message
    assert "'ghost' is not registered" in message
    assert "claude plugin uninstall stray@ghost" in message


def test_baseline_refuses_a_plugin_whose_marketplace_the_operation_would_register() -> (
    None
):
    with pytest.raises(SetforgeError, match="'ghost' is not registered"):
        operations.plugin_recovery_baseline(
            {"stray@ghost": {"id": "stray@ghost", "enabled": True}},
            {"mp1": {"name": "mp1", "source": "o/mp1"}},
            touched=set(),
            marketplaces_added=("ghost",),
        )


def test_recovery_uninstalls_what_the_install_added_but_spares_such_a_plugin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from setforge import claude_plugins

    marketplaces: dict[str, dict[str, object]] = {
        "mp1": {"source": "github:o/mp1"},
        "mp2": {"source": "github:o/mp2"},
    }
    plugins: dict[str, dict[str, object]] = {
        "kept@mp1": {"enabled": True},
        "added@mp2": {"enabled": True},
        "stray@ghost": {"enabled": True},
        "stray": {"enabled": True},
        "@ghost": {"enabled": True},
    }
    uninstalled: list[str] = []
    monkeypatch.setattr(claude_plugins, "list_marketplaces", lambda: dict(marketplaces))
    monkeypatch.setattr(claude_plugins, "list_installed", lambda: dict(plugins))
    monkeypatch.setattr(claude_plugins, "plugin_uninstall", uninstalled.append)
    monkeypatch.setattr(claude_plugins, "marketplace_remove", marketplaces.pop)
    monkeypatch.setattr(claude_plugins, "plugin_enable", lambda _id: None)
    monkeypatch.setattr(claude_plugins, "plugin_disable", lambda _id: None)

    operations._recover_plugins(
        {
            "plugins": {"kept@mp1": {"enabled": True}},
            "marketplaces": {"mp1": {"source": "github:o/mp1"}},
        }
    )

    assert uninstalled == ["added@mp2"]
    assert list(marketplaces) == ["mp1"]
