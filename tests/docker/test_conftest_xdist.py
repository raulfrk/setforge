"""Unit tests for the ``pytest_configure`` hook in tests/docker/conftest.py.

The hook auto-activates pytest-xdist with ``-n auto`` whenever the
markexpr contains ``e2e_docker`` and the user has not set ``-n`` on the
CLI. These tests drive the hook with a minimal fake ``pytest.Config``
so we can assert the resulting ``config.option.numprocesses`` without
spinning up a real pytest session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tests.docker.conftest import pytest_configure


@dataclass(slots=True)
class _FakeOption:
    """Stand-in for ``pytest.Config.option`` — only ``numprocesses`` matters here."""

    numprocesses: Any = None


@dataclass(slots=True)
class _FakeConfig:
    """Minimal fake of ``pytest.Config`` covering ``getoption`` + ``option``.

    ``getoption(name, default=...)`` looks up ``name`` in ``_values``
    falling back to ``default``; the real ``pytest.Config.getoption`` has
    the same contract for the keys the hook touches.
    """

    _values: dict[str, Any] = field(default_factory=dict)
    option: _FakeOption = field(default_factory=_FakeOption)

    def getoption(self, name: str, default: Any = None) -> Any:
        return self._values.get(name, default)


def test_pytest_configure_activates_on_e2e_marker() -> None:
    """Bare ``-m e2e_docker`` and no explicit ``-n`` → ``numprocesses='auto'``."""
    config = _FakeConfig(_values={"markexpr": "e2e_docker", "numprocesses": None})

    pytest_configure(config)  # type: ignore[arg-type]

    assert config.option.numprocesses == "auto"


def test_pytest_configure_respects_explicit_n() -> None:
    """User-supplied ``-n 0`` (or any explicit value) must NOT be overridden."""
    config = _FakeConfig(
        _values={"markexpr": "e2e_docker", "numprocesses": "0"},
        option=_FakeOption(numprocesses="0"),
    )

    pytest_configure(config)  # type: ignore[arg-type]

    # Hook saw the explicit value via getoption and returned early —
    # option.numprocesses stays at the user-supplied "0".
    assert config.option.numprocesses == "0"


def test_pytest_configure_no_op_for_non_e2e() -> None:
    """Markexprs without ``e2e_docker`` (or empty) must not flip xdist on."""
    config = _FakeConfig(_values={"markexpr": "", "numprocesses": None})

    pytest_configure(config)  # type: ignore[arg-type]

    assert config.option.numprocesses is None

    config2 = _FakeConfig(_values={"markexpr": "unit", "numprocesses": None})

    pytest_configure(config2)  # type: ignore[arg-type]

    assert config2.option.numprocesses is None


def test_pytest_configure_compound_markexpr() -> None:
    """Compound expressions like ``e2e_docker and not slow`` still activate xdist."""
    config = _FakeConfig(
        _values={"markexpr": "e2e_docker and not slow", "numprocesses": None},
    )

    pytest_configure(config)  # type: ignore[arg-type]

    assert config.option.numprocesses == "auto"
