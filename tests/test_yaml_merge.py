"""Tests for yaml_merge path parsing.

The ``_parse_path`` / ``PathTokenKind`` cluster is consumed by
:mod:`setforge.scalar_path`; ``_navigate`` / ``_MISSING`` back its lookups.
This module guards the token-tagging contract those consumers rely on.
"""

from setforge.yaml_merge import PathTokenKind, _parse_path


def test_path_token_kind_values_are_the_literal_strings() -> None:
    assert (
        PathTokenKind.KEY,
        PathTokenKind.KEY_EACH,
        PathTokenKind.KEY_WHOLE,
    ) == ("key", "key_each", "key_whole")


def test_parse_path_dispatches_each_path_token_kind() -> None:
    """Every PathTokenKind is producible and correctly tagged by _parse_path."""
    assert _parse_path("a.b") == [
        (PathTokenKind.KEY, "a"),
        (PathTokenKind.KEY, "b"),
    ]
    assert _parse_path("a.b[*]") == [
        (PathTokenKind.KEY, "a"),
        (PathTokenKind.KEY_EACH, "b"),
    ]
    assert _parse_path("a.b[]") == [
        (PathTokenKind.KEY, "a"),
        (PathTokenKind.KEY_WHOLE, "b"),
    ]
