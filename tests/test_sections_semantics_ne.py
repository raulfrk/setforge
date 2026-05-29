"""Guard that end-marker semantics-mismatch validation still raises.

The semantics comparison in :func:`setforge.sections._handle_end_marker`
moved from ``is not`` to ``!=`` (value equality, per the style rule reserving
``is`` for None/True/False/sentinels). This must not weaken the check: a
mismatched-semantics end marker must still raise :class:`MarkerError`, and a
matching one must close cleanly.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from setforge import sections
from setforge.errors import MarkerError
from setforge.sections import extract_sections

_HASH = "hash=" + "a" * 64


def test_mismatched_end_semantics_raises() -> None:
    """An end marker whose semantics differ from the open section raises."""
    text = (
        "<!-- setforge:user-section start shared workflow -->\n"
        "body\n"
        f"<!-- setforge:user-section end host-local workflow {_HASH} -->\n"
    )
    with pytest.raises(MarkerError, match="end semantics"):
        extract_sections(text)


def test_matching_end_semantics_closes_cleanly() -> None:
    """A matching-semantics end marker extracts the section without error."""
    text = (
        "<!-- setforge:user-section start shared workflow -->\n"
        "body\n"
        f"<!-- setforge:user-section end shared workflow {_HASH} -->\n"
    )
    assert extract_sections(text) == {"workflow": "body\n"}


def test_semantics_comparison_uses_value_equality() -> None:
    """The end-marker semantics guard compares with ``!=``, not ``is``/``is not``.

    Parses ``_handle_end_marker`` and confirms exactly one ``semantics``
    comparison against ``state.section_semantics``, and that it is a ``NotEq``
    (``!=``) comparison rather than an identity (``is``/``is not``) check.
    """
    source = inspect.getsource(sections._handle_end_marker)
    tree = ast.parse(textwrap.dedent(source))
    matches: list[ast.Compare] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        right = node.comparators[0] if node.comparators else None
        if (
            isinstance(left, ast.Name)
            and left.id == "semantics"
            and isinstance(right, ast.Attribute)
            and right.attr == "section_semantics"
        ):
            matches.append(node)
    assert len(matches) == 1, "expected exactly one semantics comparison"
    (op,) = matches[0].ops
    assert isinstance(op, ast.NotEq), "semantics comparison must use != (NotEq)"
