from __future__ import annotations

from pathlib import Path

from setforge.config import PackageKind, ReconcilePolicy, load_config, resolve_profile

_DUAL_SHAPE_YAML = """\
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
    cargo_binaries: [rg]
    claude_plugins: [my-plugin]
    plugins_reconcile: prune
    extensions:
      include: [ms-python.python]
      exclude: [GitHub.copilot]
      reconcile: additive
    packages: [rgx, myplug, pyext]
    reconcile:
      plugins:
        policy: prune
      extensions:
        exclude: [GitHub.copilot]
        policy: additive
"""


def test_dual_shape_config_loads(tmp_path: Path) -> None:
    config_path = tmp_path / "setforge.yaml"
    config_path.write_text(_DUAL_SHAPE_YAML)

    config = load_config(config_path)

    assert config.packages["rgx"].type is PackageKind.CARGO
    assert config.packages["myplug"].type is PackageKind.PLUGIN
    assert config.packages["pyext"].type is PackageKind.EXTENSION


def test_dual_shape_config_resolves_old_and_new_together(tmp_path: Path) -> None:
    config_path = tmp_path / "setforge.yaml"
    config_path.write_text(_DUAL_SHAPE_YAML)
    config = load_config(config_path)

    resolved = resolve_profile(config, "base")

    assert resolved.cargo_binaries == ["rg"]
    assert resolved.claude_plugins == ["my-plugin"]
    assert resolved.plugins_reconcile is ReconcilePolicy.PRUNE
    assert resolved.extensions.include == ["ms-python.python"]
    assert resolved.extensions.exclude == ["GitHub.copilot"]
    assert resolved.extensions.reconcile is ReconcilePolicy.ADDITIVE

    assert resolved.packages == ["rgx", "myplug", "pyext"]
    assert resolved.reconcile.plugins.policy is ReconcilePolicy.PRUNE
    assert resolved.reconcile.extensions.exclude == ["GitHub.copilot"]
    assert resolved.reconcile.extensions.policy is ReconcilePolicy.ADDITIVE
