"""Install and revert when the plugin tool lists something no rollback can restore.

Before changing Claude plugins, ``install`` and ``revert`` journal the plugin
tool's whole inventory, and a rollback puts back everything that differs from
it. The journal can only hold a plugin recovery could reinstall (an id of the
form ``NAME@MARKETPLACE``, a registered marketplace, a known enabled state) and
a marketplace it could register again (a GitHub repo, a git link or a path).
When the tool listed anything else, both commands used to stop with a raw
``ValueError``.

Now a command that changes no plugin or marketplace goes ahead and journals no
plugin inventory, so a rollback runs no plugin command at all. A command that
would change one stops before changing anything and names each item. Rollback
itself is unchanged: it removes what the command added and nothing else.
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


def _install(tmp_path: Path, *, yaml: str = _YAML, policy: str = "") -> Result:
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


# Marketplace rows as the plugin tool lists them: ``source`` is the kind of
# origin and the origin itself is a sibling key.
_MP1: dict[str, object] = {"name": "mp1", "source": "github", "repo": "o/mp1"}
_MP2: dict[str, object] = {"name": "mp2", "source": "github", "repo": "o/mp2"}
# The same two with the origin in ``source`` itself, which is still read.
_MP1_BARE: dict[str, object] = {"name": "mp1", "source": "o/mp1"}
_MP2_BARE: dict[str, object] = {"name": "mp2", "source": "o/mp2"}
_REVIEW: dict[str, object] = {"id": "review@mp1", "enabled": True, "scope": "user"}
_EXTRA: dict[str, object] = {"id": "extra@mp1", "enabled": True, "scope": "user"}

# What the plugin tool can list that a rollback baseline cannot hold, with the
# text the refusal names it by.
_UNRECORDABLE = [
    pytest.param(
        "plugin",
        {"id": "stray@ghost", "enabled": True},
        "plugin 'stray@ghost': its marketplace 'ghost' is not registered",
        id="unregistered-marketplace",
    ),
    pytest.param(
        "plugin",
        {"id": "stray", "enabled": True},
        "plugin 'stray': its id is not NAME@MARKETPLACE",
        id="no-marketplace-part",
    ),
    pytest.param(
        "plugin",
        {"id": "@ghost", "enabled": True},
        "plugin '@ghost': its id is not NAME@MARKETPLACE",
        id="empty-name",
    ),
    pytest.param(
        "plugin",
        {"id": "silent@mp1", "scope": "user"},
        "plugin 'silent@mp1': the plugin tool does not say whether it is enabled",
        id="no-enabled-field",
    ),
    pytest.param(
        "marketplace",
        {"name": "urlmp", "source": "url", "url": "http://example.test/mp.json"},
        "marketplace 'urlmp': setforge cannot read where it was added from",
        id="url-marketplace",
    ),
]
_STRAY = ("plugin", {"id": "stray@ghost", "enabled": True})


_Rows = tuple[dict[str, object], ...]


def _host(
    fake_claude: Callable[..., FakeClaude],
    host: dict[str, _Rows] | None = None,
    *,
    odd: tuple[str, dict[str, object]] | None = None,
) -> FakeClaude:
    """A host where, unless ``host`` says otherwise, ``_YAML`` changes no plugin."""
    listed: dict[str, _Rows] = {
        "plugins": (_REVIEW, _EXTRA),
        "marketplaces": (_MP1, _MP2),
        **(host or {}),
    }
    plugin_rows = [dict(row) for row in listed["plugins"]]
    marketplace_rows = [dict(row) for row in listed["marketplaces"]]
    if odd is not None:
        (plugin_rows if odd[0] == "plugin" else marketplace_rows).append(dict(odd[1]))
    return fake_claude(marketplaces=marketplace_rows, plugins=plugin_rows)


def _fail_after_plugins(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Fail the install after its plugin phase.

    Returns whether its journal held a plugin inventory at that point.
    """
    journaled: list[bool] = []

    def fail(*_args: object, **_kwargs: object) -> Path:
        journal = operations.active("p")
        assert journal is not None
        journaled.append(
            operations.AdapterKind.PLUGINS in {item.kind for item in journal.adapters}
        )
        raise RuntimeError("write failed after the plugins were reconciled")

    monkeypatch.setattr("setforge.cli.install._write_install_transition", fail)
    return journaled


def _deployed() -> bool:
    return (Path.home() / ".setforge-test" / "y").exists()


# Each host needs exactly one kind of plugin change from ``_YAML``.
_INSTALL_CHANGES = [
    pytest.param({"plugins": (_REVIEW,)}, "", id="installs-a-plugin"),
    pytest.param(
        {"plugins": (_REVIEW, {**_EXTRA, "enabled": False})}, "", id="enables-a-plugin"
    ),
    pytest.param(
        {"plugins": (_REVIEW, _EXTRA, {"id": "other@mp1", "enabled": True})},
        _PRUNE,
        id="disables-a-plugin",
    ),
    pytest.param({"marketplaces": (_MP1,)}, "", id="adds-a-marketplace"),
]


@pytest.mark.parametrize(("kind", "row", "named"), _UNRECORDABLE)
def test_install_that_changes_no_plugin_goes_ahead(
    tmp_path: Path,
    fake_claude: Callable[..., FakeClaude],
    kind: str,
    row: dict[str, object],
    named: str,
) -> None:
    claude = _host(fake_claude, odd=(kind, row))
    before = (claude.installed_state(), claude.marketplaces_state())

    result = _install(tmp_path)

    assert result.exit_code == 0, (result.output, result.exception)
    assert _deployed()
    assert _claude_calls_after(claude, 0) <= _LIST_ONLY
    assert (claude.installed_state(), claude.marketplaces_state()) == before


@pytest.mark.parametrize(("kind", "row", "named"), _UNRECORDABLE)
def test_install_with_a_report_policy_goes_ahead_and_still_reports(
    tmp_path: Path,
    fake_claude: Callable[..., FakeClaude],
    kind: str,
    row: dict[str, object],
    named: str,
) -> None:
    claude = _host(
        fake_claude,
        {"plugins": (_REVIEW,), "marketplaces": (_MP1,)},
        odd=(kind, row),
    )

    result = _install(tmp_path, policy="    reconcile: {plugins: {policy: report}}\n")

    assert result.exit_code == 0, (result.output, result.exception)
    assert _deployed()
    assert "would add marketplace mp2" in result.output
    assert "would add marketplace mp1" not in result.output
    assert "extra@mp1" in result.output
    assert _claude_calls_after(claude, 0) <= _LIST_ONLY


@pytest.mark.parametrize(("kind", "row", "named"), _UNRECORDABLE)
def test_failed_install_that_changed_no_plugin_runs_no_plugin_command(
    tmp_path: Path,
    fake_claude: Callable[..., FakeClaude],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    row: dict[str, object],
    named: str,
) -> None:
    claude = _host(fake_claude, odd=(kind, row))
    before = (claude.installed_state(), claude.marketplaces_state())
    journaled = _fail_after_plugins(monkeypatch)

    result = _install(tmp_path)

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, RuntimeError), result.exception
    assert journaled == [False]
    assert _claude_calls_after(claude, 0) <= _LIST_ONLY
    assert (claude.installed_state(), claude.marketplaces_state()) == before
    assert operations.active("p") is None
    assert not _deployed()


def test_install_journals_the_plugin_inventory_when_it_can_record_all_of_it(
    tmp_path: Path,
    fake_claude: Callable[..., FakeClaude],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host(fake_claude)
    journaled = _fail_after_plugins(monkeypatch)

    assert _install(tmp_path).exit_code == 1

    assert journaled == [True]


@pytest.mark.parametrize(("kind", "row", "named"), _UNRECORDABLE)
@pytest.mark.parametrize(("host", "policy"), _INSTALL_CHANGES)
def test_install_that_would_change_plugins_stops_before_changing_anything(
    tmp_path: Path,
    fake_claude: Callable[..., FakeClaude],
    host: dict[str, _Rows],
    policy: str,
    kind: str,
    row: dict[str, object],
    named: str,
) -> None:
    claude = _host(fake_claude, host, odd=(kind, row))
    before = (claude.installed_state(), claude.marketplaces_state())

    result = _install(tmp_path, policy=policy)

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SetforgeError), result.exception
    message = str(result.exception)
    assert message.startswith("install would change Claude plugins or marketplaces")
    assert named in message
    assert "Nothing was changed." in message
    assert _claude_calls_after(claude, 0) <= _LIST_ONLY
    assert (claude.installed_state(), claude.marketplaces_state()) == before
    assert operations.active("p") is None
    assert not _deployed()


def test_refusal_names_every_item_and_what_to_do_about_it(
    tmp_path: Path, fake_claude: Callable[..., FakeClaude]
) -> None:
    fake_claude(
        marketplaces=[
            _MP1,
            {"name": "urlmp", "source": "url", "url": "http://example.test/mp.json"},
        ],
        plugins=[
            _REVIEW,
            {"id": "stray@ghost", "enabled": True},
            {"id": "stray", "enabled": True},
            {"id": "silent@mp1"},
        ],
    )

    result = _install(tmp_path)

    assert isinstance(result.exception, SetforgeError), result.exception
    assert str(result.exception).splitlines()[1:-1] == [
        "  - marketplace 'urlmp': setforge cannot read where it was added from "
        "(the plugin tool reports source 'url'). Remove it with "
        "`claude plugin marketplace remove urlmp`.",
        "  - plugin 'silent@mp1': the plugin tool does not say whether it is "
        "enabled. Run `claude plugin enable silent@mp1` or "
        "`claude plugin disable silent@mp1`.",
        "  - plugin 'stray': its id is not NAME@MARKETPLACE. Uninstall it with "
        "`claude plugin uninstall stray`.",
        "  - plugin 'stray@ghost': its marketplace 'ghost' is not registered. "
        "Register that marketplace, or uninstall the plugin with "
        "`claude plugin uninstall stray@ghost`.",
    ]


def test_install_never_registers_the_marketplace_of_a_plugin_it_cannot_record(
    tmp_path: Path,
    fake_claude: Callable[..., FakeClaude],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The config key (``foo``) differs from the name the tool gives (``ghost``)."""
    claude = _host(fake_claude, odd=_STRAY)
    _fail_after_plugins(monkeypatch)
    yaml = _YAML.replace(
        "claude_plugins:", "  foo: {source: github, repo: o/ghost}\nclaude_plugins:"
    )

    result = _install(tmp_path, yaml=yaml)

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SetforgeError), result.exception
    assert claude.mp_add_args() == []
    assert claude.uninstall_args() == []
    assert claude.installed_state()["stray@ghost"] == _STRAY[1]


_GIT_URL = "https://github.com/o/mp2.git"
_GIT_YAML = _YAML.replace("repo: o/mp2", f"repo: {_GIT_URL}").replace(
    "extra: {marketplace: mp1}", "extra: {marketplace: mp2}"
)


def test_failed_install_removes_a_marketplace_it_added_that_is_listed_as_a_link(
    tmp_path: Path,
    fake_claude: Callable[..., FakeClaude],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude = fake_claude(
        marketplaces=[_MP1],
        plugins=[_REVIEW],
        native_rows=True,
        marketplace_names={_GIT_URL: "mp2"},
    )
    _fail_after_plugins(monkeypatch)

    result = _install(tmp_path, yaml=_GIT_YAML)

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, RuntimeError), result.exception
    assert claude.mp_add_args() == [_GIT_URL]
    assert claude.install_args() == ["extra@mp2"]
    assert claude.installed_state() == {"review@mp1": _REVIEW}
    assert claude.marketplaces_state() == [_MP1]
    assert operations.active("p") is None


@pytest.mark.parametrize("yaml", [_YAML, _GIT_YAML], ids=["github", "git-link"])
def test_second_install_on_a_host_the_first_set_up_runs_no_plugin_command(
    tmp_path: Path, fake_claude: Callable[..., FakeClaude], yaml: str
) -> None:
    claude = fake_claude(marketplace_names={_GIT_URL: "mp2"})
    first = _install(tmp_path, yaml=yaml)
    assert first.exit_code == 0, (first.output, first.exception)
    assert len(claude.mp_add_args()) == 2
    start = len(claude.calls)
    before = (claude.installed_state(), claude.marketplaces_state())

    second = _install(tmp_path, yaml=yaml)

    assert second.exit_code == 0, (second.output, second.exception)
    assert _claude_calls_after(claude, start) <= _LIST_ONLY
    assert (claude.installed_state(), claude.marketplaces_state()) == before


_PATH_MARKETPLACES = [
    pytest.param(
        {"name": "localmp", "source": "directory", "path": "/srv/localmp"},
        id="directory",
    ),
    pytest.param(
        {"name": "localmp", "source": "file", "path": "/srv/localmp/marketplace.json"},
        id="file",
    ),
]
_TOOL = {"id": "tool@localmp", "enabled": True, "scope": "user"}


@pytest.mark.parametrize("marketplace", _PATH_MARKETPLACES)
def test_failed_install_rolls_back_beside_a_marketplace_added_from_a_path(
    tmp_path: Path,
    fake_claude: Callable[..., FakeClaude],
    monkeypatch: pytest.MonkeyPatch,
    marketplace: dict[str, object],
) -> None:
    claude = fake_claude(marketplaces=[_MP1, marketplace], plugins=[_REVIEW, _TOOL])
    journaled = _fail_after_plugins(monkeypatch)

    result = _install(tmp_path)

    assert result.exit_code == 1, result.output
    assert journaled == [True]
    assert claude.install_args() == ["extra@mp1"]
    assert claude.uninstall_args() == ["extra@mp1"]
    assert claude.installed_state() == {"review@mp1": _REVIEW, "tool@localmp": _TOOL}
    assert claude.marketplaces_state() == [_MP1, marketplace]
    assert operations.active("p") is None


_GIT_MP: dict[str, object] = {
    "name": "gitmp",
    "source": "git",
    "url": "https://example.test/o/gitmp.git",
}
_GIT_TOOL: dict[str, object] = {"id": "tool@gitmp", "enabled": True, "scope": "user"}
_GIT_MP_NAMES = {"https://example.test/o/gitmp.git": "gitmp"}


def test_failed_install_rolls_back_beside_a_marketplace_added_from_a_git_link(
    tmp_path: Path,
    fake_claude: Callable[..., FakeClaude],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude = fake_claude(marketplaces=[_MP1, _GIT_MP], plugins=[_REVIEW, _GIT_TOOL])
    journaled = _fail_after_plugins(monkeypatch)

    result = _install(tmp_path)

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, RuntimeError), result.exception
    assert journaled == [True]
    assert claude.install_args() == ["extra@mp1"]
    assert claude.uninstall_args() == ["extra@mp1"]
    assert claude.installed_state() == {"review@mp1": _REVIEW, "tool@gitmp": _GIT_TOOL}
    assert claude.marketplaces_state() == [_MP1, _GIT_MP]
    assert operations.active("p") is None


def _plugin_journal(payload: dict[str, object]) -> operations.OperationJournal:
    return operations.OperationJournal(
        operation_id="op",
        command="install",
        profile="p",
        config_dir=None,
        state_dir=transitions.state_root().resolve(),
        resources_lock=True,
        phase=operations.OperationPhase.APPLYING,
        created_at="2026-01-01T00:00:00+00:00",
        paths=(),
        state_snapshots=(),
        adapters=(
            operations.AdapterSnapshot(
                operations.AdapterKind.PLUGINS, json.dumps(payload, sort_keys=True)
            ),
        ),
        checkpoints=(
            operations.OperationCheckpoint(
                "plugins-and-marketplaces",
                operations.CheckpointKind.COMPENSATABLE,
                "restore plugins",
                adapters=(operations.AdapterKind.PLUGINS,),
            ),
        ),
    )


@pytest.mark.parametrize("marketplace", _PATH_MARKETPLACES)
def test_recovery_registers_a_removed_path_marketplace_again_from_its_path(
    fake_claude: Callable[..., FakeClaude], marketplace: dict[str, object]
) -> None:
    claude = fake_claude(marketplaces=[_MP1], plugins=[_REVIEW])

    operations.recover_adapters(
        _plugin_journal(
            {
                "plugins": {"review@mp1": _REVIEW, "tool@localmp": _TOOL},
                "marketplaces": {"mp1": _MP1, "localmp": marketplace},
            }
        )
    )

    assert claude.mp_add_args() == [marketplace["path"]]
    assert claude.install_args() == ["tool@localmp"]
    assert claude.enable_args() == ["tool@localmp"]


def test_recovery_registers_a_removed_git_link_marketplace_again_from_its_link(
    fake_claude: Callable[..., FakeClaude],
) -> None:
    claude = fake_claude(
        marketplaces=[_MP1], plugins=[_REVIEW], marketplace_names=_GIT_MP_NAMES
    )

    operations.recover_adapters(
        _plugin_journal(
            {
                "plugins": {"review@mp1": _REVIEW, "tool@gitmp": _GIT_TOOL},
                "marketplaces": {"mp1": _MP1, "gitmp": _GIT_MP},
            }
        )
    )

    assert claude.mp_add_args() == [_GIT_MP["url"]]
    assert claude.install_args() == ["tool@gitmp"]
    assert claude.enable_args() == ["tool@gitmp"]
    assert claude.marketplaces_state() == [_MP1, _GIT_MP]


def test_recovery_puts_back_a_git_link_marketplace_now_listed_from_another_link(
    fake_claude: Callable[..., FakeClaude],
) -> None:
    moved = {**_GIT_MP, "url": "https://example.test/fork/gitmp.git"}
    claude = fake_claude(
        marketplaces=[_MP1, moved],
        plugins=[_REVIEW, _GIT_TOOL],
        marketplace_names=_GIT_MP_NAMES,
    )

    operations.recover_adapters(
        _plugin_journal(
            {
                "plugins": {"review@mp1": _REVIEW, "tool@gitmp": _GIT_TOOL},
                "marketplaces": {"mp1": _MP1, "gitmp": _GIT_MP},
            }
        )
    )

    assert [
        tuple(call[1:]) for call in claude.calls if tuple(call[1:]) not in _LIST_ONLY
    ] == [
        ("plugin", "uninstall", "tool@gitmp"),
        ("plugin", "marketplace", "remove", "gitmp"),
        ("plugin", "marketplace", "add", "--", _GIT_MP["url"]),
        ("plugin", "install", "tool@gitmp", "--scope=user"),
        ("plugin", "enable", "tool@gitmp"),
    ]
    assert claude.marketplaces_state() == [_MP1, _GIT_MP]


def test_a_journal_listing_a_git_link_marketplace_without_its_link_is_refused() -> None:
    with pytest.raises(ValueError, match="no recoverable source identity"):
        operations._validate_plugin_payload(
            {"plugins": {}, "marketplaces": {"gitmp": {"source": "git"}}}
        )


# A journal exactly as 1.3.8 to 1.5.0 wrote it (the tool's two listings, as
# listed), the host an interrupted command left behind, and the commands those
# releases ran to recover it.
_OLD_JOURNALS = [
    pytest.param(
        {"plugins": {"review@mp1": _REVIEW}, "marketplaces": {"mp1": _MP1_BARE}},
        {"plugins": [_REVIEW, _EXTRA], "marketplaces": [_MP1_BARE, _MP2_BARE]},
        [
            ("plugin", "uninstall", "extra@mp1"),
            ("plugin", "marketplace", "remove", "mp2"),
        ],
        id="removes-what-was-added",
    ),
    pytest.param(
        {
            "plugins": {"review@mp1": _REVIEW, "extra@mp2": {"enabled": False}},
            "marketplaces": {
                "mp1": _MP1_BARE,
                "mp2": {"source": "github", "repo": "o/mp2"},
            },
        },
        {"plugins": [{**_REVIEW, "enabled": False}], "marketplaces": [_MP1_BARE]},
        [
            ("plugin", "marketplace", "add", "--", "o/mp2"),
            ("plugin", "install", "extra@mp2", "--scope=user"),
            ("plugin", "enable", "review@mp1"),
        ],
        id="puts-back-what-was-removed",
    ),
    pytest.param(
        {
            "plugins": {"review@mp1": _REVIEW},
            "marketplaces": {"mp1": {"source": "github:o/mp1"}},
        },
        {
            "plugins": [_REVIEW, {"id": "stray@ghost", "enabled": True}],
            "marketplaces": [
                {"name": "mp1", "source": "o/other"},
                {"name": "urlmp", "source": "url", "url": "http://example.test/mp"},
            ],
        },
        [
            ("plugin", "uninstall", "review@mp1"),
            ("plugin", "uninstall", "stray@ghost"),
            ("plugin", "marketplace", "remove", "mp1"),
            ("plugin", "marketplace", "remove", "urlmp"),
            ("plugin", "marketplace", "add", "--", "o/mp1"),
            ("plugin", "install", "review@mp1", "--scope=user"),
            ("plugin", "enable", "review@mp1"),
        ],
        id="restores-the-whole-inventory",
    ),
    pytest.param(
        {"plugins": {"review@mp1": _REVIEW}, "marketplaces": {"mp1": _MP1_BARE}},
        {"plugins": [_REVIEW], "marketplaces": [_MP1_BARE]},
        [],
        id="nothing-to-do",
    ),
]


def test_an_earlier_journal_recovers_over_a_same_named_git_link_marketplace(
    fake_claude: Callable[..., FakeClaude],
) -> None:
    """Earlier releases stopped here: they could not read the listed row."""
    payload: dict[str, object] = {
        "plugins": {"review@mp1": _REVIEW},
        "marketplaces": {"mp1": _MP1_BARE},
    }
    claude = fake_claude(
        marketplaces=[{**_GIT_MP, "name": "mp1"}],
        plugins=[_REVIEW],
        marketplace_names={"o/mp1": "mp1"},
    )

    operations.recover_adapters(_plugin_journal(payload))

    assert [
        tuple(call[1:]) for call in claude.calls if tuple(call[1:]) not in _LIST_ONLY
    ] == [
        ("plugin", "uninstall", "review@mp1"),
        ("plugin", "marketplace", "remove", "mp1"),
        ("plugin", "marketplace", "add", "--", "o/mp1"),
        ("plugin", "install", "review@mp1", "--scope=user"),
        ("plugin", "enable", "review@mp1"),
    ]


@pytest.mark.parametrize(("payload", "host", "commands"), _OLD_JOURNALS)
def test_a_journal_from_an_earlier_release_recovers_as_that_release_did(
    fake_claude: Callable[..., FakeClaude],
    payload: dict[str, object],
    host: dict[str, list[dict[str, object]]],
    commands: list[tuple[str, ...]],
) -> None:
    claude = fake_claude(
        marketplaces=[dict(row) for row in host["marketplaces"]],
        plugins=[dict(row) for row in host["plugins"]],
    )
    operations._validate_plugin_payload(payload)

    operations.recover_adapters(_plugin_journal(payload))

    assert [
        tuple(call[1:]) for call in claude.calls if tuple(call[1:]) not in _LIST_ONLY
    ] == commands


def test_the_journaled_inventory_is_written_as_earlier_releases_wrote_it() -> None:
    plugins = {"review@mp1": dict(_REVIEW)}
    marketplaces: dict[str, dict[str, object]] = {
        "mp1": dict(_MP1_BARE),
        "mp2": {"source": "github", "repo": "o/mp2"},
    }

    snapshot = operations.plugin_recovery_snapshot(
        plugins, marketplaces, changes_plugins=True, operation="install"
    )

    assert snapshot == operations.AdapterSnapshot(
        operations.AdapterKind.PLUGINS,
        json.dumps({"plugins": plugins, "marketplaces": marketplaces}, sort_keys=True),
    )


# Each host makes ``_YAML`` record exactly one kind of plugin change.
_REVERT_CHANGES = [
    pytest.param({"plugins": (_REVIEW,)}, "", "installed", id="uninstalls-a-plugin"),
    pytest.param(
        {"plugins": (_REVIEW, {**_EXTRA, "enabled": False})},
        "",
        "enabled",
        id="disables-a-plugin",
    ),
    pytest.param(
        {"plugins": (_REVIEW, _EXTRA, {"id": "other@mp1", "enabled": True})},
        _PRUNE,
        "disabled",
        id="enables-a-plugin",
    ),
    pytest.param(
        {"marketplaces": (_MP1,)},
        "",
        "marketplaces_added",
        id="removes-a-marketplace",
    ),
]


def _recorded_plugin_changes() -> set[str]:
    (listing,) = transitions.list_transitions(["p"])
    delta = transitions.load_plugin_delta(listing.directory)
    assert delta is not None
    return {
        name
        for name in (
            "installed",
            "enabled",
            "disabled",
            "marketplaces_added",
            "marketplaces_removed",
        )
        if getattr(delta, name)
    }


@pytest.mark.parametrize(("host", "policy", "change"), _REVERT_CHANGES)
def test_revert_that_would_change_plugins_stops_before_changing_anything(
    tmp_path: Path,
    fake_claude: Callable[..., FakeClaude],
    host: dict[str, _Rows],
    policy: str,
    change: str,
) -> None:
    claude = _host(fake_claude, host)
    assert _install(tmp_path, policy=policy).exit_code == 0
    assert _recorded_plugin_changes() == {change}
    claude.run(["claude", "plugin", "install", "stray@ghost"])
    before = (claude.installed_state(), claude.marketplaces_state())
    calls_before = len(claude.calls)

    result = _revert(tmp_path)

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SetforgeError), result.exception
    message = str(result.exception)
    assert message.startswith("revert would change Claude plugins or marketplaces")
    assert "plugin 'stray@ghost': its marketplace 'ghost' is not registered" in message
    assert "then run revert again" in message
    assert _claude_calls_after(claude, calls_before) <= _LIST_ONLY
    assert (claude.installed_state(), claude.marketplaces_state()) == before
    assert operations.active("p") is None
    assert _deployed()


def test_revert_stops_when_a_plugin_it_would_uninstall_has_no_enabled_state(
    tmp_path: Path, fake_claude: Callable[..., FakeClaude]
) -> None:
    claude = _host(fake_claude, {"plugins": (_REVIEW,)})
    assert _install(tmp_path).exit_code == 0
    claude.drop_plugin_field("extra@mp1", "enabled")
    before = claude.installed_state()
    calls_before = len(claude.calls)

    result = _revert(tmp_path)

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SetforgeError), result.exception
    assert (
        "plugin 'extra@mp1': the plugin tool does not say whether it is enabled"
        in str(result.exception)
    )
    assert _claude_calls_after(claude, calls_before) <= _LIST_ONLY
    assert claude.installed_state() == before
    assert _deployed()


@pytest.mark.parametrize(("kind", "row", "named"), _UNRECORDABLE)
def test_revert_that_changes_no_plugin_goes_ahead(
    tmp_path: Path,
    fake_claude: Callable[..., FakeClaude],
    kind: str,
    row: dict[str, object],
    named: str,
) -> None:
    claude = _host(fake_claude, odd=(kind, row))
    assert _install(tmp_path).exit_code == 0
    before = (claude.installed_state(), claude.marketplaces_state())

    result = _revert(tmp_path)

    assert result.exit_code == 0, (result.output, result.exception)
    assert not _deployed()
    assert _claude_calls_after(claude, 0) <= _LIST_ONLY
    assert (claude.installed_state(), claude.marketplaces_state()) == before
