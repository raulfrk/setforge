"""Assert interactive CLI Consoles write to stderr, not stdout.

The interactive/diagnostic sites must instantiate ``Console(stderr=True)`` so
their panels/markup do not intermingle with ``--format=json`` machine output
on stdout. Verified by AST inspection of each named module: every
``Console(...)`` call there must pass ``stderr=True``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import ModuleType

import pytest
from rich.console import Console

from setforge.cli import (
    _confirm,
    _git_check,
    _revert_confirm,
    _secrets_confirm,
    _welcome,
)

_INTERACTIVE_MODULES = [
    _confirm,
    _git_check,
    _revert_confirm,
    _secrets_confirm,
    _welcome,
]


def _console_calls(tree: ast.AST) -> list[ast.Call]:
    """Return every ``Console(...)`` call node in ``tree``."""
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Name) and func.id == "Console") or (
                isinstance(func, ast.Attribute) and func.attr == "Console"
            ):
                calls.append(node)
    return calls


def _has_stderr_true(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg == "stderr":
            return isinstance(kw.value, ast.Constant) and kw.value.value is True
    return False


@pytest.mark.parametrize(
    "module",
    _INTERACTIVE_MODULES,
    ids=lambda m: m.__name__.rsplit(".", 1)[-1],
)
def test_interactive_console_uses_stderr(module: ModuleType) -> None:
    """Every Console() in an interactive module passes ``stderr=True``."""
    assert module.__file__ is not None
    source = Path(module.__file__).read_text()
    tree = ast.parse(source)
    calls = _console_calls(tree)
    assert calls, f"expected at least one Console() in {module.__name__}"
    for call in calls:
        assert _has_stderr_true(call), (
            f"Console() in {module.__name__} must pass stderr=True"
        )


def test_console_stderr_flag_semantics() -> None:
    """Confirm ``Console(stderr=True)`` actually targets the stderr stream."""
    assert Console(stderr=True).stderr is True
    assert Console().stderr is False
