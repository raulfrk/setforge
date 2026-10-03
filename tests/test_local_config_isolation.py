"""No setforge module may keep the real host ``local.yaml`` path during a test."""

from __future__ import annotations

import sys

from tests.conftest import _LOCAL_CONFIG_ATTRS, _REAL_LOCAL_CONFIG_PATH


def test_no_module_binds_the_real_local_config_path() -> None:
    leaks = [
        f"{name}.{attr}"
        for name, module in list(sys.modules.items())
        if module is not None and (name == "setforge" or name.startswith("setforge."))
        for attr in _LOCAL_CONFIG_ATTRS
        if getattr(module, attr, None) == _REAL_LOCAL_CONFIG_PATH
    ]
    assert leaks == []
