"""Tests for markdown heading-scoped span bounding (Stage 3).

A span anchored on a heading covers ``[heading_line, boundary)`` where
``boundary`` is the first subsequent heading of level <= the anchor's
level, else EOF. Children at a deeper level fall inside the span. A
``#``-line inside a fenced code block must NOT close the span (fence-aware
end-scan, the most likely bug-injection site).
"""

import pytest

from setforge.errors import AnchorAmbiguousError, AnchorNotFoundError
from setforge.markdown_spans import MarkdownSpan, bound_span, heading_level


def test_heading_level_counts_hash_run() -> None:
    assert heading_level("# Top") == 1
    assert heading_level("## Foo") == 2
    assert heading_level("### Bar") == 3
    assert heading_level("Not a heading") is None


_DOC = """\
# Title

Intro line.

## Foo

Body of Foo.

### Child

Child body.

## Bar

Body of Bar.
"""


def test_bound_span_includes_children() -> None:
    span = bound_span(_DOC, "## Foo")
    # The span starts at the "## Foo" heading line and runs up to (not
    # including) the next level-<=2 heading "## Bar".
    assert span.start_line == 4  # 0-indexed line of "## Foo"
    body = "".join(_DOC.splitlines(keepends=True)[span.start_line : span.end_line])
    assert "## Foo" in body
    assert "### Child" in body  # children included
    assert "## Bar" not in body  # sibling excluded


def test_bound_span_last_section_runs_to_eof() -> None:
    span = bound_span(_DOC, "## Bar")
    lines = _DOC.splitlines(keepends=True)
    assert span.end_line == len(lines)
    body = "".join(lines[span.start_line : span.end_line])
    assert "Body of Bar." in body


def test_bound_span_top_level_includes_all() -> None:
    span = bound_span(_DOC, "# Title")
    # A level-1 anchor with no later level-1 heading runs to EOF.
    lines = _DOC.splitlines(keepends=True)
    assert span.start_line == 0
    assert span.end_line == len(lines)


_FENCED = """\
## Real

Body.

```sh
## Not a heading inside a fence
```

More body.

## Next

Next body.
"""


def test_bound_span_ignores_heading_inside_fence() -> None:
    span = bound_span(_FENCED, "## Real")
    body = "".join(_FENCED.splitlines(keepends=True)[span.start_line : span.end_line])
    # The fenced "## Not a heading" must NOT close the span.
    assert "## Not a heading inside a fence" in body
    assert "More body." in body
    assert "## Next" not in body


def test_bound_span_not_found() -> None:
    with pytest.raises(AnchorNotFoundError):
        bound_span(_DOC, "## Missing")


def test_bound_span_duplicate_heading_ambiguous() -> None:
    doc = "## Dup\n\nA\n\n## Dup\n\nB\n"
    with pytest.raises(AnchorAmbiguousError):
        bound_span(doc, "## Dup")


def test_bound_span_returns_level_and_fingerprintable_body() -> None:
    span = bound_span(_DOC, "## Foo")
    assert isinstance(span, MarkdownSpan)
    assert span.level == 2
    assert span.body.startswith("## Foo")
