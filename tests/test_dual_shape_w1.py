from __future__ import annotations

from pathlib import Path

from setforge import reconcile_adapter
from setforge.config import PackageKind, ReconcilePolicy, load_config, resolve_profile

_PACKAGES_YAML = """\
version: 1
tracked_files:
  d:
    src: x
    dst: y
marketplaces:
  mp:
    source: github
    repo: o/r
claude_plugins:
  my-plugin:
    marketplace: mp
packages:
  rgx:
    type: cargo
    crate: rg
  myplug:
    type: plugin
    plugin: my-plugin
  pyext:
    type: extension
    extension: ms-python.python
profiles:
  base:
    tracked_files: [d]
    packages: [rgx, myplug, pyext]
    reconcile:
      plugins:
        policy: prune
      extensions:
        exclude: [GitHub.copilot]
        policy: additive
"""


def test_packages_config_loads(tmp_path: Path) -> None:
    config_path = tmp_path / "setforge.yaml"
    config_path.write_text(_PACKAGES_YAML)

    config = load_config(config_path)

    assert config.packages["rgx"].type is PackageKind.CARGO
    assert config.packages["myplug"].type is PackageKind.PLUGIN
    assert config.packages["pyext"].type is PackageKind.EXTENSION


def test_packages_config_resolves_provision_surface(tmp_path: Path) -> None:
    config_path = tmp_path / "setforge.yaml"
    config_path.write_text(_PACKAGES_YAML)
    config = load_config(config_path)

    resolved = resolve_profile(config, "base")

    assert resolved.packages == ["rgx", "myplug", "pyext"]
    assert resolved.reconcile.plugins.policy is ReconcilePolicy.PRUNE
    assert resolved.reconcile.extensions.exclude == ["GitHub.copilot"]
    assert resolved.reconcile.extensions.policy is ReconcilePolicy.ADDITIVE

    # The adapter projects the package refs back into the provisioning lists
    # the plugin / extension / cargo engines consume.
    assert reconcile_adapter.cargo_crates(config, resolved) == ["rg"]
    assert reconcile_adapter.plugin_bare_names(config, resolved) == ["my-plugin"]
    assert reconcile_adapter.plugin_policy(resolved) is ReconcilePolicy.PRUNE

    ext = reconcile_adapter.extensions_input(config, resolved)
    assert ext.include == ["ms-python.python"]
    assert ext.exclude == ["GitHub.copilot"]
    assert ext.reconcile is ReconcilePolicy.ADDITIVE
