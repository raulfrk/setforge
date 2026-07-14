from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from setforge import reconcile_adapter as adapter
from setforge import vscode_extensions
from setforge.claude_plugins import reconcile as plugin_reconcile
from setforge.cli._lock_enumerate import enumerate_lock_items
from setforge.config import (
    Config,
    load_config,
    resolve_profile,
)
from setforge.errors import ConfigError

_HEAD = (
    "version: 1\n"
    "tracked_files:\n"
    "  t: {src: t, dst: ~/t}\n"
    "marketplaces:\n"
    "  mp: {source: github, repo: o/r}\n"
    "claude_plugins:\n"
    "  sp: {marketplace: mp}\n"
)

_OLD_YAML = _HEAD + (
    "profiles:\n"
    "  p:\n"
    "    tracked_files: [t]\n"
    "    claude_plugins: [sp]\n"
    "    extensions: {include: [Vendor.Ext]}\n"
)

_NEW_YAML = _HEAD + (
    "packages:\n"
    "  sp-pkg: {type: plugin, plugin: sp}\n"
    "  ext-pkg: {type: extension, extension: Vendor.Ext}\n"
    "profiles:\n"
    "  p:\n"
    "    tracked_files: [t]\n"
    "    packages: [sp-pkg, ext-pkg]\n"
)

_BOTH_YAML = _HEAD + (
    "packages:\n"
    "  sp-pkg: {type: plugin, plugin: sp}\n"
    "  ext-pkg: {type: extension, extension: Vendor.Ext}\n"
    "profiles:\n"
    "  p:\n"
    "    tracked_files: [t]\n"
    "    claude_plugins: [sp]\n"
    "    extensions: {include: [Vendor.Ext]}\n"
    "    packages: [sp-pkg, ext-pkg]\n"
)

_SHAPES = {"old": _OLD_YAML, "new": _NEW_YAML, "both": _BOTH_YAML}


def _load(yaml_text: str) -> Config:
    d = Path(tempfile.mkdtemp())
    (d / "setforge.yaml").write_text(yaml_text, encoding="utf-8")
    return load_config(d / "setforge.yaml")


def _lock_key_set(cfg: Config) -> set[tuple[str, str]]:
    resolved = resolve_profile(cfg, "p")
    return {
        (it.pkg_type.value, it.lock_key()) for it in enumerate_lock_items(cfg, resolved)
    }


def test_three_way_validate_parity_all_clean() -> None:
    configs = {name: _load(text) for name, text in _SHAPES.items()}
    ids = {
        name: adapter.plugin_ids(cfg, resolve_profile(cfg, "p"))
        for name, cfg in configs.items()
    }
    assert ids["old"] == ids["new"] == ids["both"] == {"sp@mp"}
    exts = {
        name: adapter.extensions_input(cfg, resolve_profile(cfg, "p")).include
        for name, cfg in configs.items()
    }
    assert exts["old"] == exts["new"] == exts["both"] == ["Vendor.Ext"]


def test_three_way_lock_enumeration_parity() -> None:
    keys = {name: _lock_key_set(_load(text)) for name, text in _SHAPES.items()}
    expected = {("plugin", "sp@mp"), ("extension", "Vendor.Ext")}
    assert keys["old"] == expected
    assert keys["new"] == expected
    assert keys["both"] == expected


def test_lock_path_undeclared_plugin_message_verbatim() -> None:
    cfg = Config.model_validate(
        {
            "tracked_files": {"t": {"src": "t", "dst": "~/t"}},
            "marketplaces": {"mp": {"source": "github", "repo": "o/r"}},
            "profiles": {"p": {"claude_plugins": ["ghost"]}},
        }
    )
    resolved = resolve_profile(cfg, "p")
    with pytest.raises(ConfigError) as exc:
        enumerate_lock_items(cfg, resolved)
    assert str(exc.value) == (
        "profile references undeclared plugin: 'ghost' "
        "(add it to top-level claude_plugins:)"
    )


def test_load_time_aggregated_undeclared_message_verbatim() -> None:
    yaml_text = (
        "version: 1\n"
        "tracked_files:\n"
        "  t: {src: t, dst: ~/t}\n"
        "profiles:\n"
        "  p:\n"
        "    tracked_files: [t]\n"
        "    claude_plugins: [ghost]\n"
    )
    with pytest.raises(ConfigError) as exc:
        _load(yaml_text)
    assert str(exc.value) == (
        "profile claude_plugins reference undeclared plugin(s): "
        "p.ghost (add to top-level claude_plugins:)"
    )


def test_the_three_undeclared_sites_stay_distinct() -> None:
    per_ref = (
        "profile references undeclared plugin: 'ghost' "
        "(add it to top-level claude_plugins:)"
    )
    aggregated = (
        "profile claude_plugins reference undeclared plugin(s): "
        "p.ghost (add to top-level claude_plugins:)"
    )
    assert per_ref != aggregated


@pytest.fixture
def _fake_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        vscode_extensions, "resolve_binary", lambda _name: Path("/fake/code")
    )


@pytest.mark.usefixtures("_fake_code")
def test_casefold_exclude_via_new_reconcile_block_subtracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _run(argv: list[str], **_kw: object) -> Any:
        import subprocess

        if "--list-extensions" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="")
        return subprocess.CompletedProcess(argv, 0, stdout="")

    monkeypatch.setattr(vscode_extensions.subprocess, "run", _run)

    cfg = Config.model_validate(
        {
            "tracked_files": {"t": {"src": "t", "dst": "~/t"}},
            "packages": {
                "cop": {"type": "extension", "extension": "GitHub.copilot"},
            },
            "profiles": {
                "p": {
                    "packages": ["cop"],
                    "reconcile": {"extensions": {"exclude": ["github.copilot"]}},
                }
            },
        }
    )
    resolved = resolve_profile(cfg, "p")
    ext_input = adapter.extensions_input(cfg, resolved)
    assert ext_input.include == ["GitHub.copilot"]
    assert ext_input.exclude == ["github.copilot"]

    report = vscode_extensions.reconcile(ext_input)
    assert report.to_install == []


def _new_pkg_plugin_cfg() -> Config:
    return Config.model_validate(
        {
            "tracked_files": {"t": {"src": "t", "dst": "~/t"}},
            "marketplaces": {
                "anthropic": {"source": "github", "repo": "anthropics/plugins"}
            },
            "claude_plugins": {"sp": {"marketplace": "anthropic"}},
            "packages": {"sp-pkg": {"type": "plugin", "plugin": "sp"}},
            "profiles": {"p": {"packages": ["sp-pkg"]}},
        }
    )


def test_marketplace_add_idempotent_for_new_package_plugin(fake_claude) -> None:
    fake = fake_claude(marketplaces=[])
    cfg = _new_pkg_plugin_cfg()
    resolved = resolve_profile(cfg, "p")
    synth = adapter.synth_plugin_profile(cfg, resolved)

    first = plugin_reconcile(cfg, synth)
    assert first.marketplaces_added == ["anthropic"]
    assert len(fake.mp_add_args()) == 1

    second = plugin_reconcile(cfg, synth)
    assert second.marketplaces_added == []
    assert len(fake.mp_add_args()) == 1
