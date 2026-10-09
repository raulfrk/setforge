"""A second ``revert`` redoes the plugin and marketplace changes the first undid.

``install`` changes Claude plugins and marketplaces through the plugin tool
and records what it changed. ``revert`` undoes those changes and records what
*it* changed, so that the next ``revert`` puts them back. The record of a
revert used to list its own changes under the names of the changes it undid
(a plugin it uninstalled was listed as installed), so the next revert repeated
the undo instead of redoing the install.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge import codex_plugins, transitions
from setforge.cli import app
from setforge.config import MarketplaceSource, MarketplaceSourceKind
from setforge.errors import InvalidTransitionRecord
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
# The same config with ``mp2`` added from a git link, from a link straight to
# a ``marketplace.json``, and from a directory.
_GIT_URL = "https://example.com/team/mp2.git"
_GIT_YAML = _YAML.replace("repo: o/mp2", f"repo: {_GIT_URL}")
_JSON_URL = "https://example.com/team/mp2/marketplace.json"
_JSON_YAML = _YAML.replace("repo: o/mp2", f"repo: {_JSON_URL}")
_MP2_NAMES = {_GIT_URL: "mp2", _JSON_URL: "mp2"}
_PATH_YAML = _YAML.replace(
    "mp2: {source: github, repo: o/mp2}", "mp2: {source: path, path: /srv/mp2}"
)

_MP1: dict[str, object] = {"name": "mp1", "source": "github", "repo": "o/mp1"}
_MP2: dict[str, object] = {"name": "mp2", "source": "github", "repo": "o/mp2"}
_REVIEW: dict[str, object] = {"id": "review@mp1", "enabled": True, "scope": "user"}
_EXTRA: dict[str, object] = {"id": "extra@mp1", "enabled": True, "scope": "user"}
_OTHER: dict[str, object] = {"id": "other@mp1", "enabled": True, "scope": "user"}

_State = tuple[dict[str, dict], dict[str, dict]]


def _install(tmp_path: Path, policy: str = "", yaml: str = _YAML) -> Result:
    cfg = write_setforge_yaml(tmp_path, yaml + policy)
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


def _revert(tmp_path: Path) -> Result:
    return CliRunner().invoke(
        app,
        ["revert", "--profile=p", f"--config={tmp_path / 'setforge.yaml'}", "--yes"],
    )


def _state(claude: FakeClaude) -> _State:
    """The plugin tool's plugins and marketplaces, each keyed by name."""
    return (
        claude.installed_state(),
        {str(row["name"]): row for row in claude.marketplaces_state()},
    )


def _newest_record() -> Path:
    return Path(transitions.list_transitions(["p"], reverse=True)[0].directory)


# The host each install starts from, the reconcile policy, and the one kind of
# plugin change that install then makes.
_HOSTS = [
    pytest.param([], [], "", _YAML, id="adds-marketplaces-and-installs-plugins"),
    pytest.param([_MP1, _MP2], [_REVIEW], "", _YAML, id="installs-a-plugin"),
    pytest.param(
        [_MP1, _MP2],
        [_REVIEW, {**_EXTRA, "enabled": False}],
        "",
        _YAML,
        id="enables-a-plugin",
    ),
    pytest.param(
        [_MP1, _MP2],
        [_REVIEW, _EXTRA, _OTHER],
        _PRUNE,
        _YAML,
        id="disables-a-plugin",
    ),
    pytest.param([_MP1], [], "", _YAML, id="adds-one-marketplace"),
    pytest.param([_MP1], [], "", _GIT_YAML, id="adds-a-marketplace-from-a-git-link"),
    pytest.param([_MP1], [], "", _JSON_YAML, id="adds-a-marketplace-from-a-json-link"),
    pytest.param([_MP1], [], "", _PATH_YAML, id="adds-a-marketplace-from-a-directory"),
]


@pytest.mark.parametrize(("marketplaces", "plugins", "policy", "yaml"), _HOSTS)
def test_second_revert_redoes_the_install_and_third_undoes_it_again(
    tmp_path: Path,
    fake_claude: Callable[..., FakeClaude],
    marketplaces: list[dict[str, object]],
    plugins: list[dict[str, object]],
    policy: str,
    yaml: str,
) -> None:
    claude = fake_claude(
        marketplaces=[dict(row) for row in marketplaces],
        plugins=[dict(row) for row in plugins],
        native_rows=True,
        marketplace_names=_MP2_NAMES,
    )
    before_install = _state(claude)
    assert _install(tmp_path, policy, yaml).exit_code == 0
    after_install = _state(claude)
    assert after_install != before_install
    assert set(after_install[1]) == {"mp1", "mp2"}

    first = _revert(tmp_path)
    assert first.exit_code == 0, (first.output, first.exception)
    assert _state(claude) == before_install

    redo = _revert(tmp_path)
    assert redo.exit_code == 0, (redo.output, redo.exception)
    assert _state(claude) == after_install

    third = _revert(tmp_path)
    assert third.exit_code == 0, (third.output, third.exception)
    assert _state(claude) == before_install


@pytest.mark.parametrize(
    "fresh_install_enabled", [False, True], ids=["lands-disabled", "lands-enabled"]
)
def test_redo_leaves_a_plugin_that_was_installed_disabled_disabled(
    tmp_path: Path,
    fake_claude: Callable[..., FakeClaude],
    fresh_install_enabled: bool,
) -> None:
    claude = fake_claude(
        marketplaces=[dict(_MP1), dict(_MP2)],
        native_rows=True,
        fresh_install_enabled=fresh_install_enabled,
    )
    assert _install(tmp_path).exit_code == 0
    claude.run(["claude", "plugin", "disable", "extra@mp1"])
    after_install = _state(claude)

    assert _revert(tmp_path).exit_code == 0
    assert _state(claude)[0] == {}
    assert _revert(tmp_path).exit_code == 0

    assert _state(claude) == after_install


def test_redo_does_not_reinstall_a_plugin_removed_by_hand_before_the_revert(
    tmp_path: Path, fake_claude: Callable[..., FakeClaude]
) -> None:
    claude = fake_claude(marketplaces=[dict(_MP1), dict(_MP2)], native_rows=True)
    assert _install(tmp_path).exit_code == 0
    claude.run(["claude", "plugin", "uninstall", "extra@mp1"])
    installs_before = len(claude.install_args())

    assert _revert(tmp_path).exit_code == 0
    assert _state(claude)[0] == {}
    assert _revert(tmp_path).exit_code == 0

    assert claude.install_args()[installs_before:] == ["review@mp1"]
    assert set(_state(claude)[0]) == {"review@mp1"}


def test_plugin_that_fails_to_reinstall_on_redo_is_not_recorded_as_installed(
    tmp_path: Path,
    fake_claude: Callable[..., FakeClaude],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude = fake_claude(marketplaces=[dict(_MP1), dict(_MP2)], native_rows=True)
    assert _install(tmp_path).exit_code == 0
    assert _revert(tmp_path).exit_code == 0

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        if args[1:4] == ["plugin", "install", "extra@mp1"]:
            claude.calls.append(list(args))
            raise subprocess.CalledProcessError(1, args, "", "install failed")
        return claude.run(args, **kwargs)

    monkeypatch.setattr("setforge.claude_plugins.subprocess.run", run)
    redo = _revert(tmp_path)
    monkeypatch.setattr("setforge.claude_plugins.subprocess.run", claude.run)

    assert "FAILED plugin install extra@mp1" in redo.output
    assert set(_state(claude)[0]) == {"review@mp1"}
    redo_record = json.loads(
        (_newest_record() / "plugins.json").read_text(encoding="utf-8")
    )
    assert redo_record["installed"] == ["review@mp1"]
    uninstalls_before = len(claude.uninstall_args())

    third = _revert(tmp_path)

    assert third.exit_code == 0, (third.output, third.exception)
    assert claude.uninstall_args()[uninstalls_before:] == ["review@mp1"]
    assert _state(claude)[0] == {}


def test_revert_record_lists_what_the_revert_itself_changed(
    tmp_path: Path, fake_claude: Callable[..., FakeClaude]
) -> None:
    fake_claude(native_rows=True)
    assert _install(tmp_path).exit_code == 0
    install_record = json.loads(
        (_newest_record() / "plugins.json").read_text(encoding="utf-8")
    )

    assert _revert(tmp_path).exit_code == 0
    revert_record = json.loads(
        (_newest_record() / "plugins.json").read_text(encoding="utf-8")
    )

    # An install record is written exactly as released versions wrote it.
    assert install_record == {
        "installed": ["extra@mp1", "review@mp1"],
        "enabled": [],
        "disabled": [],
        "marketplaces_added": ["mp1", "mp2"],
        "marketplaces_removed": [],
    }
    assert revert_record == {
        "installed": [],
        "enabled": [],
        "disabled": [],
        "marketplaces_added": [],
        "marketplaces_removed": [
            ["mp1", {"source": "github", "repo": "o/mp1"}],
            ["mp2", {"source": "github", "repo": "o/mp2"}],
        ],
        "uninstalled": [["extra@mp1", True], ["review@mp1", True]],
    }

    shown = CliRunner().invoke(app, ["transitions", "show", _newest_record().name])

    assert shown.exit_code == 0, shown.output
    assert "- extra@mp1  (uninstalled)" in shown.output
    assert "- marketplace:mp1" in shown.output
    # Both records count two plugins and two marketplaces.
    assert [row.plugin_count for row in transitions.list_transitions(["p"])] == [4, 4]


@pytest.mark.parametrize(
    ("yaml", "link"),
    [(_GIT_YAML, _GIT_URL), (_JSON_YAML, _JSON_URL)],
    ids=["git-link", "json-link"],
)
def test_revert_records_a_link_marketplace_in_the_form_releases_read(
    tmp_path: Path, fake_claude: Callable[..., FakeClaude], yaml: str, link: str
) -> None:
    """The record holds only the two source kinds every release accepts."""
    claude = fake_claude(
        marketplaces=[dict(_MP1)], native_rows=True, marketplace_names=_MP2_NAMES
    )
    assert _install(tmp_path, yaml=yaml).exit_code == 0

    assert _revert(tmp_path).exit_code == 0

    record = json.loads((_newest_record() / "plugins.json").read_text(encoding="utf-8"))
    assert record["marketplaces_removed"] == [
        ["mp2", {"source": "github", "repo": link}]
    ]
    # Every release registers it again with the link itself as the argument.
    assert _revert(tmp_path).exit_code == 0
    assert claude.mp_add_args() == [link, link]


@pytest.mark.parametrize(
    "uninstalled",
    ["extra@mp1", ["extra@mp1"], [["extra@mp1"]], [["extra@mp1", "yes"]], [[1, True]]],
)
def test_record_with_a_malformed_uninstalled_list_is_refused(
    uninstalled: object,
) -> None:
    with pytest.raises(InvalidTransitionRecord, match="uninstalled"):
        transitions.plugin_delta_from_json({"uninstalled": uninstalled})


def test_revert_record_written_by_a_released_version_reverts_as_it_did(
    tmp_path: Path, fake_claude: Callable[..., FakeClaude]
) -> None:
    """Released versions wrote a revert's record under the undone change's names.

    Reverting such a record keeps doing what those versions did: it runs the
    same undo commands again and installs or registers nothing.
    """
    claude = fake_claude(native_rows=True)
    assert _install(tmp_path).exit_code == 0
    assert _revert(tmp_path).exit_code == 0
    after_first_revert = _state(claude)
    (_newest_record() / "plugins.json").write_text(
        json.dumps(
            {
                "installed": ["extra@mp1", "review@mp1"],
                "enabled": [],
                "disabled": [],
                "marketplaces_added": ["mp1", "mp2"],
                "marketplaces_removed": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    calls_before = len(claude.calls)

    result = _revert(tmp_path)

    assert result.exit_code == 0, (result.output, result.exception)
    changing = [
        call[1:]
        for call in claude.calls[calls_before:]
        if call[1:] != ["plugin", "list", "--json"]
        and call[1:] != ["plugin", "marketplace", "list", "--json"]
    ]
    assert changing == [
        ["plugin", "uninstall", "extra@mp1"],
        ["plugin", "uninstall", "review@mp1"],
        ["plugin", "marketplace", "remove", "mp1"],
        ["plugin", "marketplace", "remove", "mp2"],
    ]
    assert _state(claude) == after_first_revert


_CODEX_YAML = """\
version: 1
schema_version: '6.4'
minimum_version: '6.4'
tracked_files: {}
codex:
  marketplaces:
    official: {source: github, repo: owner/repo}
  plugins:
    review: {marketplace: official}
profiles:
  p:
    codex:
      plugins: [review]
"""


def test_second_revert_redoes_a_codex_install_and_third_undoes_it_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="owner/repo")
    plugins: dict[str, codex_plugins.InstalledPlugin] = {}
    marketplaces: dict[str, codex_plugins.InstalledMarketplace] = {}

    def add_marketplace(added: MarketplaceSource) -> None:
        assert added == source
        marketplaces["official"] = codex_plugins.InstalledMarketplace(
            "official", tmp_path / "official", source
        )

    def install_plugin(plugin_id: str) -> None:
        name, _, marketplace = plugin_id.rpartition("@")
        assert marketplace in marketplaces
        plugins[plugin_id] = codex_plugins.InstalledPlugin(plugin_id, name, marketplace)

    monkeypatch.setattr(codex_plugins, "list_installed", lambda: dict(plugins))
    monkeypatch.setattr(codex_plugins, "list_marketplaces", lambda: dict(marketplaces))
    monkeypatch.setattr(codex_plugins, "marketplace_add", add_marketplace)
    monkeypatch.setattr(codex_plugins, "marketplace_remove", marketplaces.pop)
    monkeypatch.setattr(codex_plugins, "plugin_install", install_plugin)
    monkeypatch.setattr(codex_plugins, "plugin_remove", plugins.pop)
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(_CODEX_YAML, encoding="utf-8")

    def state() -> tuple[set[str], set[str]]:
        return set(plugins), set(marketplaces)

    installed = CliRunner().invoke(
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
    assert installed.exit_code == 0, (installed.output, installed.exception)
    assert state() == ({"review@official"}, {"official"})

    for expected in (
        (set(), set()),
        ({"review@official"}, {"official"}),
        (set(), set()),
    ):
        result = _revert(tmp_path)
        assert result.exit_code == 0, (result.output, result.exception)
        assert state() == expected
