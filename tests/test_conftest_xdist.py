"""Unit tests for ``pytest_configure`` in the project-root ``conftest.py``.

The hook auto-activates pytest-xdist with ``-n 4`` whenever the
``markexpr`` contains ``e2e_docker`` and the user has not passed ``-n``
or ``--numprocesses`` on the CLI. See the root conftest's module
docstring for the timing analysis (why ``pytest_configure(tryfirst=True)``
at the project ROOT works but a subdir conftest's same hook does not).

These tests drive the hook with a minimal fake ``pytest.Config`` so we
can assert the resulting ``option.numprocesses`` / ``option.dist`` /
``option.tx`` triple without spinning up a real pytest session.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

_ROOT_CONFTEST: Path = Path(__file__).resolve().parents[1] / "conftest.py"


def _load_root_conftest_pytest_configure():  # type: ignore[no-untyped-def]
    """Import the project-root ``conftest.py`` and return its ``pytest_configure``.

    The file is loaded by absolute path so the test isn't fragile to
    pytest's conftest collection order (which would normally hide the
    module under a private attribute).
    """
    spec = importlib.util.spec_from_file_location("_root_conftest", _ROOT_CONFTEST)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.pytest_configure


pytest_configure = _load_root_conftest_pytest_configure()


@dataclass(slots=True)
class _FakeOption:
    """Stand-in for ``pytest.Config.option`` — only the xdist knobs matter."""

    numprocesses: Any = None
    dist: str = "no"
    tx: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _FakeConfig:
    """Minimal fake of ``pytest.Config`` covering ``getoption`` + ``option``.

    ``getoption(name, default=...)`` looks up ``name`` in ``_values``,
    falling back to ``default``; the real ``pytest.Config.getoption``
    has the same contract for the keys the hook touches.

    ``hasattr(config, "workerinput")`` is True only when the config
    represents an xdist worker subprocess — the dataclass leaves the
    attribute absent for the main process and tests opt-in to the
    worker case via ``_FakeWorkerConfig`` below.
    """

    _values: dict[str, Any] = field(default_factory=dict)
    option: _FakeOption = field(default_factory=_FakeOption)

    def getoption(self, name: str, default: Any = None) -> Any:
        return self._values.get(name, default)


@dataclass(slots=True)
class _FakeWorkerConfig(_FakeConfig):
    """Worker-process variant: carries ``workerinput`` to exercise the bail."""

    workerinput: dict[str, Any] = field(default_factory=dict)


def test_activates_on_bare_e2e_marker() -> None:
    """Bare ``-m e2e_docker`` and no explicit ``-n`` → set all three knobs."""
    config = _FakeConfig(_values={"markexpr": "e2e_docker", "numprocesses": None})

    pytest_configure(cast(pytest.Config, config))

    assert config.option.numprocesses == 4
    assert config.option.dist == "load"
    assert config.option.tx == ["popen", "popen", "popen", "popen"]


def test_respects_explicit_n_flag() -> None:
    """User-supplied ``-n 0`` must NOT be overridden — no triple set."""
    config = _FakeConfig(
        _values={"markexpr": "e2e_docker", "numprocesses": "0"},
        option=_FakeOption(numprocesses="0"),
    )

    pytest_configure(cast(pytest.Config, config))

    assert config.option.numprocesses == "0"
    assert config.option.dist == "no"
    assert config.option.tx == []


def test_no_op_for_non_e2e_marker() -> None:
    """Markexprs without ``e2e_docker`` must not flip xdist on."""
    config = _FakeConfig(_values={"markexpr": "unit", "numprocesses": None})

    pytest_configure(cast(pytest.Config, config))

    assert config.option.numprocesses is None
    assert config.option.dist == "no"
    assert config.option.tx == []


def test_no_op_for_empty_marker() -> None:
    """Empty markexpr (no ``-m``) must not flip xdist on."""
    config = _FakeConfig(_values={"markexpr": "", "numprocesses": None})

    pytest_configure(cast(pytest.Config, config))

    assert config.option.numprocesses is None
    assert config.option.tx == []


def test_activates_on_compound_markexpr() -> None:
    """``-m "e2e_docker and not slow"`` (substring match) → activate."""
    config = _FakeConfig(
        _values={"markexpr": "e2e_docker and not slow", "numprocesses": None},
    )

    pytest_configure(cast(pytest.Config, config))

    assert config.option.numprocesses == 4
    assert config.option.tx == ["popen", "popen", "popen", "popen"]


def test_no_op_in_worker_subprocess() -> None:
    """xdist worker subprocesses carry ``workerinput`` — must NOT recurse."""
    config = _FakeWorkerConfig(
        _values={"markexpr": "e2e_docker", "numprocesses": None},
        workerinput={"workerid": "gw0"},
    )

    pytest_configure(cast(pytest.Config, config))

    assert config.option.numprocesses is None
    assert config.option.tx == []


def test_no_op_for_not_e2e_marker() -> None:
    """``-m 'not e2e_docker'`` (the project's unit-test default) must NOT activate."""
    config = _FakeConfig(
        _values={"markexpr": "not e2e_docker", "numprocesses": None},
    )

    pytest_configure(cast(pytest.Config, config))

    assert config.option.numprocesses is None
    assert config.option.dist == "no"
    assert config.option.tx == []


def test_no_op_for_negated_e2e_in_compound() -> None:
    """``-m 'unit and not e2e_docker'`` must NOT activate."""
    config = _FakeConfig(
        _values={"markexpr": "unit and not e2e_docker", "numprocesses": None},
    )

    pytest_configure(cast(pytest.Config, config))

    assert config.option.numprocesses is None
    assert config.option.tx == []


def test_preserves_existing_dist_when_not_no() -> None:
    """If user supplied ``--dist=loadscope``, don't clobber it."""
    config = _FakeConfig(
        _values={"markexpr": "e2e_docker", "numprocesses": None},
        option=_FakeOption(dist="loadscope"),
    )

    pytest_configure(cast(pytest.Config, config))

    assert config.option.numprocesses == 4
    assert config.option.dist == "loadscope"
    assert config.option.tx == ["popen", "popen", "popen", "popen"]
