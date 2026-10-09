"""An installed plugin the recovery journal cannot describe must not break install.

``claude plugin list`` can name a plugin whose marketplace ``claude plugin
marketplace list`` does not (or an id that is not ``NAME@MARKETPLACE``). Install
records the plugin inventory so a failed install can put it back; that record
only holds plugins recovery could reinstall. Such a plugin used to stop every
install with a raw ``ValueError``. Now an install that does not touch the plugin
goes ahead and a rollback leaves the plugin installed; one that would change it,
or register its marketplace, stops before changing anything, with an error that
names the plugin.

``revert`` records the same inventory before it changes anything, so it follows
the same rules: a revert whose recorded transition does not involve the plugin
goes ahead and leaves the plugin alone, and one that would have to change it
stops first.

The tool reports a marketplace added from a path as a ``directory`` or ``file``
source and one added from a link as a ``url`` source. Recovery reads the path
shapes. A ``url`` source it cannot read follows the same rules as the plugin
above: install and revert go ahead and leave it registered, and a revert that
would have to remove it stops first.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge import operations, transitions
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


def _revert(tmp_path: Path) -> Result:
    return CliRunner().invoke(
        app,
        [
            "revert",
            "--profile=p",
            f"--config={tmp_path / 'setforge.yaml'}",
            "--yes",
        ],
    )


def _claude_calls_after(claude: FakeClaude, start: int) -> set[tuple[str, ...]]:
    return {tuple(call[1:]) for call in claude.calls[start:]}


_LIST_ONLY = {
    ("plugin", "list", "--json"),
    ("plugin", "marketplace", "list", "--json"),
}


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


@pytest.mark.parametrize("odd", _ODD_PLUGINS)
def test_revert_that_does_not_involve_the_plugin_leaves_it_alone(
    tmp_path: Path, fake_claude: Callable[..., FakeClaude], odd: dict[str, object]
) -> None:
    claude = _claude(fake_claude, odd)
    assert _install(tmp_path).exit_code == 0

    result = _revert(tmp_path)

    assert result.exit_code == 0, (result.output, result.exception)
    assert claude.uninstall_args() == ["extra@mp1"]
    assert claude.install_args() == ["extra@mp1"]
    assert claude.installed_state()[str(odd["id"])] == odd
    assert not (Path.home() / ".setforge-test" / "y").exists()


@pytest.mark.parametrize("odd", _ODD_PLUGINS)
def test_revert_of_the_revert_leaves_the_plugin_alone_too(
    tmp_path: Path, fake_claude: Callable[..., FakeClaude], odd: dict[str, object]
) -> None:
    claude = _claude(fake_claude, odd)
    assert _install(tmp_path).exit_code == 0
    assert _revert(tmp_path).exit_code == 0

    result = _revert(tmp_path)

    assert result.exit_code == 0, (result.output, result.exception)
    assert claude.installed_state()[str(odd["id"])] == odd
    assert str(odd["id"]) not in claude.uninstall_args()


@pytest.mark.parametrize("odd", _ODD_PLUGINS)
def test_failed_revert_puts_its_plugin_changes_back_and_keeps_the_plugin(
    tmp_path: Path,
    fake_claude: Callable[..., FakeClaude],
    monkeypatch: pytest.MonkeyPatch,
    odd: dict[str, object],
) -> None:
    from setforge.cli import revert as revert_cli

    claude = _claude(fake_claude, odd)
    assert _install(tmp_path).exit_code == 0
    reverse_and_record = revert_cli._write_reverse_transition

    def fail_after_plugins(*args: object, **kwargs: object) -> Path:
        reverse_and_record(*args, **kwargs)  # type: ignore[arg-type]
        raise RuntimeError("write failed after the plugins were reversed")

    monkeypatch.setattr(revert_cli, "_write_reverse_transition", fail_after_plugins)

    result = _revert(tmp_path)

    assert result.exit_code == 1, result.output
    assert claude.uninstall_args() == ["extra@mp1"]
    installed = claude.installed_state()
    assert installed["extra@mp1"]["id"] == "extra@mp1"
    assert installed[str(odd["id"])] == odd
    assert [m["name"] for m in claude.marketplaces_state()] == ["mp1", "mp2"]
    assert operations.active("p") is None


def test_revert_that_would_change_the_plugin_stops_before_changing_anything(
    tmp_path: Path, fake_claude: Callable[..., FakeClaude]
) -> None:
    claude = fake_claude(
        marketplaces=[{"name": "mp1", "source": "o/mp1"}],
        plugins=[{"id": "review@mp1", "enabled": True, "scope": "user"}],
    )
    assert _install(tmp_path).exit_code == 0
    # The user drops the marketplace outside setforge: the plugin the install
    # added stays installed, but recovery could no longer reinstall it.
    claude.run(["claude", "plugin", "marketplace", "remove", "mp1"])
    calls_before = len(claude.calls)

    result = _revert(tmp_path)

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SetforgeError), result.exception
    assert "plugin 'extra@mp1' is installed" in str(result.exception)
    assert "'mp1' is not registered" in str(result.exception)
    assert "then run revert again" in str(result.exception)
    assert _claude_calls_after(claude, calls_before) <= _LIST_ONLY
    assert "extra@mp1" in claude.installed_state()
    assert operations.active("p") is None
    assert (Path.home() / ".setforge-test" / "y").exists()


def test_revert_that_would_register_the_plugins_marketplace_stops_first(
    tmp_path: Path, fake_claude: Callable[..., FakeClaude]
) -> None:
    claude = fake_claude(
        marketplaces=[{"name": "mp1", "source": "o/mp1"}],
        plugins=[{"id": "stray@mp2", "enabled": True}],
    )
    write_setforge_yaml(tmp_path, _YAML)
    # A record of a marketplace removal: reverting it registers mp2 again.
    transitions.write_transition(
        transitions.make_meta(transitions.TransitionCommand.INSTALL, "p"),
        {},
        {},
        None,
        plugin_delta=transitions.PluginDelta(
            installed=(),
            enabled=(),
            disabled=(),
            marketplaces_added=(),
            marketplaces_removed=(("mp2", {"source": "github", "repo": "o/mp2"}),),
        ),
    )
    calls_before = len(claude.calls)

    result = _revert(tmp_path)

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SetforgeError), result.exception
    assert "plugin 'stray@mp2' is installed" in str(result.exception)
    assert "'mp2' is not registered" in str(result.exception)
    assert _claude_calls_after(claude, calls_before) <= _LIST_ONLY
    assert [m["name"] for m in claude.marketplaces_state()] == ["mp1"]
    assert operations.active("p") is None


_REPORTED_MARKETPLACES = [
    pytest.param(
        {"name": "localmp", "source": "directory", "path": "/srv/localmp"},
        id="directory",
    ),
    pytest.param(
        {"name": "localmp", "source": "file", "path": "/srv/localmp/marketplace.json"},
        id="file",
    ),
    pytest.param(
        {"name": "localmp", "source": "url", "url": "http://example.test/mp.json"},
        id="url",
    ),
]


def _claude_with_marketplace(
    fake_claude: Callable[..., FakeClaude], marketplace: dict[str, object]
) -> FakeClaude:
    return fake_claude(
        marketplaces=[{"name": "mp1", "source": "o/mp1"}, dict(marketplace)],
        plugins=[
            {"id": "review@mp1", "enabled": True, "scope": "user"},
            {"id": "tool@localmp", "enabled": True, "scope": "user"},
        ],
    )


@pytest.mark.parametrize("marketplace", _REPORTED_MARKETPLACES)
def test_install_goes_ahead_with_a_directory_file_or_url_marketplace(
    tmp_path: Path,
    fake_claude: Callable[..., FakeClaude],
    marketplace: dict[str, object],
) -> None:
    claude = _claude_with_marketplace(fake_claude, marketplace)

    result = _install(tmp_path)

    assert result.exit_code == 0, (result.output, result.exception)
    assert claude.uninstall_args() == []
    assert claude.mp_add_args() == ["o/mp2"]
    assert marketplace in claude.marketplaces_state()


@pytest.mark.parametrize("marketplace", _REPORTED_MARKETPLACES)
def test_failed_install_keeps_a_directory_file_or_url_marketplace(
    tmp_path: Path,
    fake_claude: Callable[..., FakeClaude],
    monkeypatch: pytest.MonkeyPatch,
    marketplace: dict[str, object],
) -> None:
    claude = _claude_with_marketplace(fake_claude, marketplace)

    def fail_after_plugins(*_args: object, **_kwargs: object) -> Path:
        raise RuntimeError("write failed after the plugins were reconciled")

    monkeypatch.setattr(
        "setforge.cli.install._write_install_transition", fail_after_plugins
    )

    result = _install(tmp_path)

    assert result.exit_code == 1, result.output
    assert claude.install_args() == ["extra@mp1"]
    assert claude.uninstall_args() == ["extra@mp1"]
    assert claude.marketplaces_state() == [
        {"name": "mp1", "source": "o/mp1"},
        marketplace,
    ]
    assert "tool@localmp" in claude.installed_state()
    assert operations.active("p") is None


@pytest.mark.parametrize("marketplace", _REPORTED_MARKETPLACES)
def test_revert_that_does_not_involve_the_marketplace_leaves_it_alone(
    tmp_path: Path,
    fake_claude: Callable[..., FakeClaude],
    marketplace: dict[str, object],
) -> None:
    claude = _claude_with_marketplace(fake_claude, marketplace)
    assert _install(tmp_path).exit_code == 0

    result = _revert(tmp_path)

    assert result.exit_code == 0, (result.output, result.exception)
    assert claude.uninstall_args() == ["extra@mp1"]
    assert claude.marketplaces_state() == [
        {"name": "mp1", "source": "o/mp1"},
        marketplace,
    ]
    assert "tool@localmp" in claude.installed_state()


def _reregister_mp2(claude: FakeClaude, source: dict[str, object]) -> None:
    # The install added mp2; outside setforge it is now registered another way.
    claude._marketplaces[:] = [
        {"name": "mp1", "source": "o/mp1"},
        {"name": "mp2", **source},
    ]


def _install_with_mp2_added(
    tmp_path: Path, fake_claude: Callable[..., FakeClaude]
) -> FakeClaude:
    claude = fake_claude(
        marketplaces=[{"name": "mp1", "source": "o/mp1"}],
        plugins=[{"id": "review@mp1", "enabled": True, "scope": "user"}],
    )
    assert _install(tmp_path).exit_code == 0
    return claude


def test_revert_that_would_remove_the_marketplace_stops_before_changing_anything(
    tmp_path: Path, fake_claude: Callable[..., FakeClaude]
) -> None:
    claude = _install_with_mp2_added(tmp_path, fake_claude)
    _reregister_mp2(claude, {"source": "url", "url": "http://example.test/mp.json"})
    calls_before = len(claude.calls)

    result = _revert(tmp_path)

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SetforgeError), result.exception
    assert "marketplace 'mp2' is registered" in str(result.exception)
    assert "claude plugin marketplace remove mp2" in str(result.exception)
    assert "then run revert again" in str(result.exception)
    assert _claude_calls_after(claude, calls_before) <= _LIST_ONLY
    assert "extra@mp1" in claude.installed_state()
    assert operations.active("p") is None
    assert (Path.home() / ".setforge-test" / "y").exists()


def test_revert_removes_a_marketplace_listed_with_a_path_source(
    tmp_path: Path, fake_claude: Callable[..., FakeClaude]
) -> None:
    claude = _install_with_mp2_added(tmp_path, fake_claude)
    _reregister_mp2(claude, {"source": "directory", "path": "/srv/mp2"})

    result = _revert(tmp_path)

    assert result.exit_code == 0, (result.output, result.exception)
    assert [m["name"] for m in claude.marketplaces_state()] == ["mp1"]


def test_baseline_leaves_out_a_marketplace_recovery_cannot_read_and_its_plugins() -> (
    None
):
    url: dict[str, object] = {
        "name": "urlmp",
        "source": "url",
        "url": "http://example.test/mp.json",
    }
    path: dict[str, object] = {
        "name": "dirmp",
        "source": "directory",
        "path": "/srv/dirmp",
    }

    baseline = operations.plugin_recovery_baseline(
        {
            "tool@urlmp": {"id": "tool@urlmp", "enabled": True},
            "tool@dirmp": {"id": "tool@dirmp", "enabled": True},
        },
        {"urlmp": url, "dirmp": path},
        touched=set(),
        marketplaces_added=(),
    )

    assert baseline == {
        "plugins": {"tool@dirmp": {"id": "tool@dirmp", "enabled": True}},
        "marketplaces": {"dirmp": path},
    }
    operations._validate_plugin_payload(json.loads(json.dumps(baseline)))


def test_baseline_refuses_a_plugin_of_an_unreadable_marketplace_it_would_change() -> (
    None
):
    with pytest.raises(SetforgeError) as raised:
        operations.plugin_recovery_baseline(
            {"tool@urlmp": {"id": "tool@urlmp", "enabled": True}},
            {"urlmp": {"name": "urlmp", "source": "url", "url": "http://x.test/m"}},
            touched={"tool@urlmp"},
            marketplaces_added=(),
            operation="revert",
        )

    message = str(raised.value)
    assert "plugin 'tool@urlmp' is installed" in message
    assert "the source of its marketplace 'urlmp' cannot be read" in message
    assert "claude plugin uninstall tool@urlmp" in message
    assert "register that marketplace" not in message
    assert "then run revert again" in message


def test_recovery_removes_a_path_marketplace_the_install_added_not_a_url_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from setforge import claude_plugins

    marketplaces: dict[str, dict[str, object]] = {
        "mp1": {"source": "github:o/mp1"},
        "urlmp": {"source": "url", "url": "http://example.test/mp.json"},
        "added": {"source": "directory", "path": "/srv/added"},
    }
    plugins: dict[str, dict[str, object]] = {
        "kept@mp1": {"enabled": True},
        "tool@urlmp": {"enabled": True},
        "new@added": {"enabled": True},
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

    assert uninstalled == ["new@added"]
    assert list(marketplaces) == ["mp1", "urlmp"]
