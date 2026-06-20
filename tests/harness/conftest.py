"""Harness fixtures: fs isolation + subprocess-boundary mock (RFC 0001 §6).

Two fixtures the invariant tests build on:

- :func:`isolated_state_home` — a ``tmp_path``-rooted ``$HOME`` +
  ``SETFORGE_STATE_DIR`` so every reconcile transition lands in a
  throwaway tree, never the dev-host home or the real state dir. The
  project-wide ``tests/conftest.py`` already isolates ``$HOME`` per test;
  this fixture ADDS the explicit state-dir redirect and hands the test a
  typed handle to both roots so an invariant can read the on-disk store.
- :func:`mock_cli_subprocess` — the subprocess boundary mocked via
  pytest-subprocess (``fake_process``). The reconcile engine shells out
  to ``claude`` / ``git`` / ``code``; intercepting at the
  ``subprocess.run`` syscall boundary tests the real call shape rather
  than an in-Python fake, and a real-binary call that was never
  registered RAISES (``ProcessNotRegisteredError``) instead of escaping
  the sandbox.

EXTENSION POINT (E2 / E6): the integration tier wires these two fixtures
together — drive a verb against ``isolated_state_home`` with
``mock_cli_subprocess`` registering the ``claude``/``git`` responses each
reconcile path expects. Comment-handling fidelity (ruamel round-trip,
JSONC preservation) is asserted by reading the store bodies back through
the same fixtures end-to-end.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from pytest_subprocess import FakeProcess


@dataclass(frozen=True, slots=True)
class IsolatedStateHome:
    """Typed handle to the isolated roots a reconcile test runs against."""

    root: Path
    home: Path
    state_dir: Path


@pytest.fixture
def isolated_state_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> IsolatedStateHome:
    """Redirect ``$HOME`` + ``Path.home`` + ``SETFORGE_STATE_DIR`` to tmp_path.

    Belt-and-suspenders on top of the project-wide autouse ``_isolate_home``:
    this fixture pins a KNOWN home/state pair under the test's own
    ``tmp_path`` and exposes both so an invariant can ``cat`` the store. The
    state dir is redirected via the ``SETFORGE_STATE_DIR`` env var that
    :func:`setforge.transitions.state_root` honors, so any production code
    path resolving the state dir lands inside the sandbox.
    """
    home = tmp_path / "home"
    state_dir = tmp_path / "state"
    home.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state_dir))
    monkeypatch.setattr(Path, "home", lambda: Path(os.environ["HOME"]))
    return IsolatedStateHome(root=tmp_path, home=home, state_dir=state_dir)


@pytest.fixture
def mock_cli_subprocess() -> Iterator[FakeProcess]:
    """Mock the ``subprocess`` boundary via pytest-subprocess.

    Yields a :class:`pytest_subprocess.FakeProcess`. Register the exact
    argv each reconcile path is expected to shell out to::

        mock_cli_subprocess.register(
            ["claude", "plugin", "list", "--json"], stdout="[]"
        )

    Any real-binary invocation that was NOT registered raises
    ``ProcessNotRegisteredError`` — so an unexpected shell-out fails the
    test loudly instead of hitting the host. ``allow_unregistered`` stays
    off by design: the whole point of the seam is that every subprocess
    call is accounted for.
    """
    with FakeProcess() as process:
        yield process
