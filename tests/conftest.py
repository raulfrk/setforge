"""Shared pytest fixtures for the setforge test suite.

Two autouse fixtures here form a defense-in-depth around the
``~/.config/setforge/local.yaml`` stub-creation race that surfaces
under ``-n auto`` (setforge-hpd4):

- :func:`_isolated_local_config` redirects the ``LOCAL_CONFIG_PATH``
  module constants in ``setforge.binaries`` and ``setforge.source`` to
  a per-test ``tmp_path`` directory.
- :func:`_isolate_home` monkeypatches ``$HOME`` and ``pathlib.Path.home``
  to a per-test tmp directory. Catches any production code path that
  resolves ``Path.home()`` lazily (completion, snapshots, transitions,
  migrations) — without this, parallel workers would still race on the
  dev-host home for those code paths.

The xdist auto-activation hook lives in the **project-root**
``conftest.py``; it can't live here because ``pytest_configure`` at a
subdir conftest fires too late for xdist (see the root conftest's
module docstring for the timing analysis).
"""

import os
from collections.abc import Sequence
from pathlib import Path

import pytest

# Re-export the ``fake_claude`` fixture defined in tests/test_claude_plugins.py
# so test_cli_e2e.py (setforge-181) can request it via parameter without
# tripping ruff F811 (which fires on direct imports of a fixture name that
# matches a test-parameter name). Placing the import here makes the fixture
# discoverable via pytest's normal conftest mechanism instead of a same-file
# rebinding. ``__all__`` silences ruff F401 without per-site noqa.
from tests.test_claude_plugins import fake_claude, fake_git

__all__ = ["fake_claude", "fake_git"]


@pytest.fixture(autouse=True)
def _isolated_local_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Redirect ``LOCAL_CONFIG_PATH`` constants to a tmp path for every test.

    Two modules carry the constant — ``binaries`` for the ``binaries:``
    block and ``source`` for the ``source:`` block — and both must be
    redirected so neither leaks to ``~/.config/setforge/local.yaml`` on
    the dev host. Also resets ``source._cli_source`` to None so a test
    that sets it directly via ``set_cli_source`` (without going through
    a ``CliRunner`` callback) doesn't leak the value to later tests.
    """
    monkeypatch.setattr(
        "setforge.binaries.LOCAL_CONFIG_PATH",
        tmp_path / "local.yaml",
    )
    monkeypatch.setattr(
        "setforge.source.LOCAL_CONFIG_PATH",
        tmp_path / "local.yaml",
    )
    # setforge.compare imports LOCAL_CONFIG_PATH for orphan_ignore reads
    # (setforge-o3h8); redirect that re-export too so tests don't read
    # the dev host's local.yaml mid-compare.
    monkeypatch.setattr(
        "setforge.compare.LOCAL_CONFIG_PATH",
        tmp_path / "local.yaml",
    )
    # setforge.cli.orphans imports LOCAL_CONFIG_PATH for orphan_ignore
    # writes (setforge-o3h8); redirect that re-export too.
    monkeypatch.setattr(
        "setforge.cli.orphans.LOCAL_CONFIG_PATH",
        tmp_path / "local.yaml",
    )
    monkeypatch.setattr("setforge.source._cli_source", None)


@pytest.fixture(autouse=True)
def _isolate_home(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> Path | None:
    """Redirect ``$HOME`` + ``Path.home()`` to a per-test tmp directory.

    Belt-and-suspenders against parallel-worker races on the shared
    ``~/.config/setforge/local.yaml`` stub that the Typer root callback
    writes via :func:`setforge.binaries.ensure_local_config_stub`.
    :func:`_isolated_local_config` already redirects the
    ``LOCAL_CONFIG_PATH`` module constants — but any other production
    code path that resolves ``Path.home()`` lazily (completion,
    snapshots, transitions, migrations) would still race on the real
    dev-host home. Monkeypatching at the ``Path.home`` level catches
    every reachable site.

    Skip on tests carrying the ``no_home_isolation`` marker — used by
    tests that legitimately need the live ``$HOME``. The marker is a
    forward escape hatch; no test ships with it today.

    The home dir lives under a per-test ``tmp_path_factory.mktemp``
    directory, NOT under the test's ``tmp_path``. This matters because
    some tests pass their ``tmp_path`` to production code and then
    assert it is empty (e.g.
    ``test_claude_plugins.test_resolve_marketplace_source_regular_returns_input``
    on its ``cache_root=tmp_path`` argument). If the autouse fixture
    seeded a subdir into ``tmp_path``, those assertions would
    false-fail. ``tmp_path_factory`` gives a separate per-test dir
    that doesn't collide with anything the test author wrote.

    The fixture returns the redirected home so a test can request it
    via parameter and inspect the contents of the sandboxed
    ``~/.config/setforge/`` directly.
    """
    if request.node.get_closest_marker("no_home_isolation") is not None:
        return None
    home = tmp_path_factory.mktemp("_autoisolated_home")
    monkeypatch.setenv("HOME", str(home))
    # ``Path.home`` is monkeypatched to read ``$HOME`` dynamically so a
    # downstream fixture that does ``monkeypatch.setenv("HOME", ...)``
    # still propagates to ``Path.home()`` calls. A captured-value lambda
    # (``lambda: home``) would ignore later env changes and silently break
    # tests that re-isolate HOME for their own purposes (e.g.
    # ``tests/test_completion.py:home``).
    monkeypatch.setattr(Path, "home", lambda: Path(os.environ["HOME"]))
    return home


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: Sequence[pytest.Item],
) -> None:
    """Register the ``no_home_isolation`` marker for ``--strict-markers``.

    Registration via ``config.addinivalue_line`` keeps
    ``pytest --strict-markers`` happy without forcing every test author
    to remember the marker name in pyproject.toml. The collection hook
    fires once per session, so the registration cost is negligible.
    """
    del items  # collection hook accepts items; we don't filter here.
    config.addinivalue_line(
        "markers",
        "no_home_isolation: opt this test out of the _isolate_home autouse fixture.",
    )
