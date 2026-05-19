"""Frozen-tuple regression test for CLI command registration order.

The ``setforge --help`` listing order is driven by Typer's preservation
of ``@app.command()`` registration order, which in the
:mod:`setforge.cli` package is driven by the import order of the
subcommand modules at the bottom of ``setforge/cli/__init__.py``. That
block is protected by ``# isort: off / # isort: on`` so ruff doesn't
re-sort it alphabetically — but the pin is convention-only. This test
asserts the registered command and subgroup names match a frozen tuple
so any accidental reorder fails CI loudly instead of silently shipping
a renamed-or-reordered ``--help`` body.

If you intentionally change the command order, update the tuples here.
"""

from __future__ import annotations

from setforge.cli import app

# Order MUST match the pre-split cli.py source order — see the
# `# isort: off` block in setforge/cli/__init__.py.
EXPECTED_DIRECT_COMMANDS: tuple[str, ...] = (
    "install",
    "compare",
    "capture",
    "merge",
    "sync",
    "revert",
    "validate",
    "fetch",
    "init",
    "upgrade",
    "migrate",
    "status",
)

EXPECTED_SUBGROUPS: tuple[str, ...] = (
    "transitions",
    "ext",
    "plugin",
    "marketplace",
    "section",
)


def test_direct_command_names_in_registration_order() -> None:
    """``setforge --help`` lists direct commands in pre-split source order."""
    names: list[str] = []
    for c in app.registered_commands:
        if c.name:
            names.append(c.name)
        else:
            assert c.callback is not None, (
                "registered command has neither name nor callback"
            )
            names.append(c.callback.__name__)
    assert tuple(names) == EXPECTED_DIRECT_COMMANDS


def test_subgroup_names_in_registration_order() -> None:
    """``setforge --help`` lists subgroups in pre-split source order."""
    actual = tuple(g.name for g in app.registered_groups)
    assert actual == EXPECTED_SUBGROUPS
