"""Smoke tests for :mod:`setforge.cli._install_helpers`.

The heavy lifting is covered by ``tests/test_install.py`` plus the
Docker e2e suite. These tests exist so a future structural rename of
the helper surface fails fast (import-error class) and so the
no-drift short-circuit on :func:`_check_unexpected_drift` is anchored
explicitly.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from setforge.cli import _install_helpers
from setforge.cli._helpers import ProfileContext
from setforge.compare import CompareReport


def test_install_helpers_module_imports() -> None:
    """The three public-to-install helpers are exported and callable."""
    assert callable(_install_helpers._check_unexpected_drift)
    assert callable(_install_helpers._deploy_all_tracked_files)
    assert callable(_install_helpers._write_install_transition)


def test_check_unexpected_drift_no_entries_is_noop() -> None:
    """Empty :class:`CompareReport` → short-circuit, no side effect, no Exit.

    The helper returns ``None`` unconditionally; the assertion is that the
    no-drift call doesn't raise / Exit. ``ProfileContext`` is unreachable
    on this short-circuit path so the test passes ``None`` deliberately —
    the cast keeps mypy honest about the deliberate violation that the
    short-circuit contract permits.
    """
    empty = CompareReport(entries=[], has_unexpected_drift=False)
    _install_helpers._check_unexpected_drift(
        empty,
        cast(ProfileContext, None),
        Path("/tmp/setforge.yaml"),
        auto_accept_tracked=False,
        auto_accept_live=False,
    )
