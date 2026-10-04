"""No test may resolve a setforge path under the developer's real home.

Every root comes from :mod:`setforge.paths` at call time, so redirecting
``$HOME`` (the autouse ``_isolate_home`` fixture) is enough. These tests fail
when that stops being true: an accessor that ignores the redirected home, a
module that keeps its own copy of the ``local.yaml`` accessor (and so escapes
``redirect_local_config_path``), or a module-level constant or default argument
computed from the real home at import time.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from pathlib import Path

import pytest

import setforge
import setforge.cli
from setforge import claude_marketplace_cache, paths, secrets
from tests.conftest import (
    REAL_HOME,
    REAL_LOCAL_CONFIG_PATH,
    _real_local_config_path,
    redirect_local_config_path,
)

_PACKAGE_ROOT = Path(setforge.__file__).resolve().parent

_ACCESSORS = (
    paths.config_root,
    _real_local_config_path,
    paths.state_root,
    paths.cache_root,
    paths.journals_root,
    paths.data_root,
    paths.snapshots_root,
    paths.vscode_user_dir,
    claude_marketplace_cache.marketplace_cache_root,
    secrets.default_allowlist_path,
)

# Import-time values that legitimately name the real home: a denylist of
# generic directories the orphan scan must never treat as managed. It is
# compared against, never read from or written to.
_ALLOWED_IMPORT_TIME = {("setforge.compare", "GENERIC_DST_ROOTS")}


def _setforge_modules() -> list[tuple[str, types.ModuleType]]:
    return [
        (name, module)
        for name, module in sorted(sys.modules.items())
        if module is not None and (name == "setforge" or name.startswith("setforge."))
    ]


def _under_real_home(path: Path) -> bool:
    if not path.is_absolute():
        return False
    return path.is_relative_to(REAL_HOME) and not path.is_relative_to(_PACKAGE_ROOT)


def _paths_in(value: object) -> Iterator[Path]:
    if isinstance(value, Path):
        yield value
    elif isinstance(value, (tuple, list, set, frozenset)):
        for item in value:
            yield from _paths_in(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _paths_in(key)
            yield from _paths_in(item)


def _import_time_values(module: types.ModuleType) -> Iterator[tuple[str, object]]:
    for attr, value in vars(module).items():
        if attr.startswith("__"):
            continue
        yield attr, value
        function = getattr(value, "__func__", value)
        if isinstance(function, types.FunctionType):
            if function.__module__ != module.__name__:
                continue
            yield f"{attr}() defaults", function.__defaults__ or ()
            yield f"{attr}() defaults", tuple((function.__kwdefaults__ or {}).values())


@pytest.mark.parametrize("accessor", _ACCESSORS, ids=lambda a: a.__qualname__)
def test_accessor_resolves_under_the_redirected_home(accessor: object) -> None:
    home = Path.home()
    assert home != REAL_HOME
    resolved = accessor()  # type: ignore[operator]
    assert resolved.is_relative_to(home), resolved
    assert not _under_real_home(resolved)


def test_real_home_is_what_the_accessors_would_otherwise_use() -> None:
    assert REAL_LOCAL_CONFIG_PATH == REAL_HOME / ".config" / "setforge" / "local.yaml"
    assert _under_real_home(REAL_LOCAL_CONFIG_PATH)


def test_state_root_follows_the_home_until_a_test_overrides_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert paths.state_root() == Path.home() / ".local" / "state" / "setforge"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    assert paths.state_root() == tmp_path / "state"


def test_local_config_redirect_reaches_every_module(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    copies = [
        f"{name}.{attr}"
        for name, module in _setforge_modules()
        if module is not paths
        for attr, value in vars(module).items()
        if value is _real_local_config_path
    ]
    assert copies == []
    target = tmp_path / "elsewhere" / "local.yaml"
    redirect_local_config_path(monkeypatch, target)
    assert paths.local_config_path() == target


def test_no_module_keeps_an_import_time_path_under_the_real_home() -> None:
    leaks = sorted(
        {
            f"{name}.{label} = {path}"
            for name, module in _setforge_modules()
            for label, value in _import_time_values(module)
            if (name, label) not in _ALLOWED_IMPORT_TIME
            for path in _paths_in(value)
            if _under_real_home(path)
        }
    )
    assert leaks == []


def test_import_time_scan_sees_a_planted_real_home_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planted = REAL_HOME / ".config" / "setforge" / "local.yaml"

    def _with_default(path: Path = planted) -> Path:
        return path

    _with_default.__module__ = paths.__name__
    monkeypatch.setattr(paths, "_PLANTED", (planted,), raising=False)
    monkeypatch.setattr(paths, "_planted_default", _with_default, raising=False)
    found = {
        label
        for label, value in _import_time_values(paths)
        for path in _paths_in(value)
        if _under_real_home(path)
    }
    assert found == {"_PLANTED", "_planted_default() defaults"}
