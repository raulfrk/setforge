"""Tests for the file-type-dispatched span anchor validator.

:func:`setforge.spans.validate_spans_file_type` routes each declared span's
anchor through a grammar check chosen by the source's file type: markdown
sources permit only heading-shaped anchors; structural (yaml/json/jsonc)
sources permit only dotted-path anchors. A wrong-grammar anchor raises
:class:`~setforge.errors.ConfigError` at parse / validate / install time
(p5qc.8.1, the file-type-dispatch acceptance row).
"""

from pathlib import Path

import pytest

from setforge.errors import ConfigError
from setforge.spans import (
    SpanEntry,
    is_heading_anchor,
    validate_spans_file_type,
)


def _heading(anchor: str = "## Foo") -> SpanEntry:
    return SpanEntry.model_validate({"anchor": anchor})


def _dotted(anchor: str = "editor.fontSize") -> SpanEntry:
    return SpanEntry.model_validate({"anchor": anchor})


# ---------------------------------------------------------------------------
# is_heading_anchor classification.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("anchor", "expected"),
    [
        ("## Foo", True),
        ("# Top", True),
        ("###### Deep", True),
        ("   ## Indented", True),
        ("editor.fontSize", False),
        ("a.b.c", False),
        ("plain", False),
        ("key#withhash", False),
    ],
)
def test_is_heading_anchor(anchor: str, expected: bool) -> None:
    assert is_heading_anchor(anchor) is expected


# ---------------------------------------------------------------------------
# markdown sources: heading anchors legal, dotted-path rejected.
# ---------------------------------------------------------------------------


def test_markdown_allows_heading_anchor() -> None:
    validate_spans_file_type("notes.md", [_heading()], Path("notes.md"))


def test_markdown_rejects_dotted_path_anchor() -> None:
    with pytest.raises(ConfigError, match="heading-shaped"):
        validate_spans_file_type("notes.md", [_dotted()], Path("notes.md"))


# ---------------------------------------------------------------------------
# structural sources: dotted-path legal, heading rejected.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["c.yaml", "c.yml", "c.json", "c.jsonc"])
def test_structural_allows_dotted_path_anchor(name: str) -> None:
    validate_spans_file_type(name, [_dotted()], Path(name))


@pytest.mark.parametrize("name", ["c.yaml", "c.json", "c.jsonc"])
def test_structural_rejects_heading_anchor(name: str) -> None:
    with pytest.raises(ConfigError, match="dotted path"):
        validate_spans_file_type(name, [_heading()], Path(name))


# ---------------------------------------------------------------------------
# unsupported file types + empty no-op.
# ---------------------------------------------------------------------------


def test_unsupported_suffix_rejected() -> None:
    with pytest.raises(ConfigError, match="supported only"):
        validate_spans_file_type("c.toml", [_dotted()], Path("c.toml"))


def test_noop_when_no_spans_declared() -> None:
    # No span declared -> file type irrelevant, never raises.
    validate_spans_file_type("c.toml", [], Path("c.toml"))
    validate_spans_file_type("c.json", [], Path("c.json"))
