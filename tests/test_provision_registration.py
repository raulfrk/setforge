"""The plugin + extension provisioners register and build by type."""

import importlib

from setforge.provision.protocol import Identity, ProvisionItem
from setforge.provision.registry import build


def test_plugin_and_extension_build_by_type() -> None:
    # Importing the provisioner modules standalone (no adapter first) must
    # register them AND not trip an import cycle; build() then resolves both.
    importlib.import_module("setforge.provision.plugin")
    importlib.import_module("setforge.provision.extension")

    plugin = build(
        ProvisionItem(type="plugin", identity=Identity(key="a@mk", display="a@mk"))
    )
    extension = build(
        ProvisionItem(type="extension", identity=Identity(key="x.y", display="x.y"))
    )
    assert plugin.type == "plugin"
    assert extension.type == "extension"
