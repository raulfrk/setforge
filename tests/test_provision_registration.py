"""The plugin + extension provisioners register and build by type."""

import importlib

from setforge.provision.protocol import Identity, ProvisionItem
from setforge.provision.registry import build


def test_plugin_and_extension_build_by_type() -> None:
    # Importing the reconcile adapters triggers the @register side effect;
    # build() must then resolve both new provisioner types.
    importlib.import_module("setforge.claude_plugins")
    importlib.import_module("setforge.vscode_extensions")

    plugin = build(
        ProvisionItem(type="plugin", identity=Identity(key="a@mk", display="a@mk"))
    )
    extension = build(
        ProvisionItem(type="extension", identity=Identity(key="x.y", display="x.y"))
    )
    assert plugin.type == "plugin"
    assert extension.type == "extension"
