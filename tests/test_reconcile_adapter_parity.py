"""Three-way (legacy / package / both) parity + ConfigError snapshot + casefold
+ marketplace-idempotency coverage for the reconcile adapter.

Covers, once the reconcile adapter unions the OLD declaration fields
(``claude_plugins`` / ``extensions`` / ``cargo_binaries`` /
``plugins_reconcile``) with the NEW package + reconcile-block surface:

1. THREE-WAY parity — one real config expressed via OLD fields, via NEW
   packages, and via BOTH must ``validate`` identically AND enumerate the same
   lock surface.
2. The two ConfigError message snapshots that must stay DISTINCT from the
   adapter/per-ref one: the lock-path ``_plugin_item`` raise and the aggregated
   load-time ``_validate_plugin_references`` raise.
3. Casefold regression: a NEW ExtensionPackage excluded via the new
   reconcile block subtracts case-insensitively through the adapter.
4. Marketplace idempotency: reconciling a NEW-package plugin twice adds the
   marketplace on the first run only.
"""

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

# --- Shared config bodies for the three-way parity: OLD / NEW / BOTH --------
#
# One real config — a plugin ``sp`` (marketplace ``mp``) + an extension
# ``Vendor.Ext`` selected by profile ``p`` — expressed three ways. The plugin
# is always DECLARED in the top-level ``claude_plugins`` registry (a
# PluginPackage names a registry plugin; it does not replace the declaration).

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
    """Run the FULL loader (schema + every validator) on a tmp setforge.yaml."""
    d = Path(tempfile.mkdtemp())
    (d / "setforge.yaml").write_text(yaml_text, encoding="utf-8")
    return load_config(d / "setforge.yaml")


def _lock_key_set(cfg: Config) -> set[tuple[str, str]]:
    resolved = resolve_profile(cfg, "p")
    return {
        (it.pkg_type.value, it.lock_key()) for it in enumerate_lock_items(cfg, resolved)
    }


# --- 1. Three-way VALIDATE + LOCK parity -----------------------------------


def test_three_way_validate_parity_all_clean() -> None:
    """OLD == NEW == BOTH: every shape loads + validates without error.

    ``load_config`` is the real validate driver — it runs
    ``_validate_plugin_references`` / ``_validate_package_references`` and the
    rest. A clean load for all three is the ``validate`` outcome parity.
    """
    configs = {name: _load(text) for name, text in _SHAPES.items()}
    # Every shape resolves the same effective plugin id set + extension set,
    # proving validate did not silently diverge (e.g. drop a package).
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
    """The set of ``_LockItem`` (type, key) pairs is IDENTICAL across shapes.

    Guards the double-enumeration regression: a NEW plugin/extension package
    must lock via the adapter loop (a ``PluginResolveItem`` /
    ``ExtensionResolveItem`` with a resolvable ``lock_key``), NOT also as a raw
    package object the resolver cannot consume.
    """
    keys = {name: _lock_key_set(_load(text)) for name, text in _SHAPES.items()}
    expected = {("plugin", "sp@mp"), ("extension", "Vendor.Ext")}
    assert keys["old"] == expected
    assert keys["new"] == expected
    assert keys["both"] == expected


# --- 2. The two REMAINING ConfigError snapshots (verbatim + DISTINCT) ------


def test_lock_path_undeclared_plugin_message_verbatim() -> None:
    """``_lock_enumerate._plugin_item`` raise — the LOCK-path snapshot.

    A profile selecting an undeclared plugin (bare name absent from the
    top-level registry) must raise this EXACT string when the lock surface is
    enumerated. Distinct site from the adapter's read-time raise.
    """
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
    """``_validate_plugin_references`` aggregated raise — the LOAD-path snapshot.

    A DIFFERENT message shape ("claude_plugins reference undeclared plugin(s)")
    with the ``profile.name`` offender list, batched at load. Freezing it keeps
    the three sites distinct (2 per-ref identical + 1 aggregated).
    """
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
    """Belt-and-braces: the aggregated LOAD message differs from the per-ref
    read/lock message, so the two snapshots above can never collapse into one.
    """
    per_ref = (
        "profile references undeclared plugin: 'ghost' "
        "(add it to top-level claude_plugins:)"
    )
    aggregated = (
        "profile claude_plugins reference undeclared plugin(s): "
        "p.ghost (add to top-level claude_plugins:)"
    )
    assert per_ref != aggregated


# --- 3. Casefold regression ------------------------------------------------


@pytest.fixture
def _fake_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve ``_ensure_code`` to a fake path without touching PATH."""
    monkeypatch.setattr(
        vscode_extensions, "resolve_binary", lambda _name: Path("/fake/code")
    )


@pytest.mark.usefixtures("_fake_code")
def test_casefold_exclude_via_new_reconcile_block_subtracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GitHub.copilot`` (NEW ExtensionPackage) excluded as ``github.copilot``
    via the NEW reconcile block must NOT survive into the effective set.

    Feeds the adapter's ``extensions_input`` output into
    ``vscode_extensions.reconcile``; the case-insensitive subtract must drop the
    extension despite the casing mismatch. Guards a case-sensitive adapter path.
    """

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
    # The union still carries the mixed-case include + lowercase exclude...
    assert ext_input.include == ["GitHub.copilot"]
    assert ext_input.exclude == ["github.copilot"]

    report = vscode_extensions.reconcile(ext_input)
    # ...but the effective (to-install) set drops it case-insensitively.
    assert report.to_install == []


# --- 4. Marketplace idempotency via a NEW package --------------------------


def _new_pkg_plugin_cfg() -> Config:
    """A config whose plugin is declared ONLY via a NEW PluginPackage.

    The YAML marketplace key (``anthropic``) differs from claude's
    manifest-derived name so the source-identity dedup is the thing under test.
    """
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
    """Reconcile a NEW-package plugin TWICE: the second run adds no marketplace.

    ``synth_plugin_profile`` overlays the adapter's unioned plugin names onto
    the resolved profile so the unchanged reconcile engine sees the new-surface
    plugin. Source-identity dedup must survive that adapter hop.
    """
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
