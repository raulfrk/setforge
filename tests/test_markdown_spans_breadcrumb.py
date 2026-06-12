"""Tests for breadcrumb span anchors (duplicate-heading disambiguation).

A breadcrumb anchor joins full ATX heading segments with ``" > "``
(e.g. ``"## Final checks > ### Failure handling"``) so a heading whose
text+level repeats under different parents can still be pinned. Detection
is back-compatible: a string is a breadcrumb only when EVERY ``" > "``
segment parses as an ATX heading — otherwise it is a simple literal
anchor exactly as before. Resolution matches the LEAF segment whose
ancestor chain (nearest preceding heading of STRICTLY lower level, fence
aware) ends with the breadcrumb's segments as a suffix; the span's end
boundary scans at the LEAF level. Still-ambiguous breadcrumbs raise
:class:`AnchorAmbiguousError` (Invariant I8: never pick-first).
"""

import pytest

from setforge.errors import AnchorAmbiguousError, AnchorNotFoundError
from setforge.markdown_spans import bound_span

# Two "### Failure handling" leaves under DIFFERENT ## parents — the
# motivating duplicate-heading shape a breadcrumb disambiguates.
_DUP_DOC = """\
# Title

## Final checks

Check intro.

### Failure handling

Final-checks failure body.

### Other sub

Other sub body.

## Deployment

Deploy intro.

### Failure handling

Deployment failure body.
"""


def test_breadcrumb_resolves_duplicate_leaf() -> None:
    span = bound_span(_DUP_DOC, "## Final checks > ### Failure handling")
    assert span.level == 3
    assert span.body.startswith("### Failure handling")
    assert "Final-checks failure body." in span.body
    # End boundary at the LEAF level: the sibling "### Other sub" closes
    # the span; nothing from the other parent leaks in.
    assert "### Other sub" not in span.body
    assert "Deployment failure body." not in span.body

    other = bound_span(_DUP_DOC, "## Deployment > ### Failure handling")
    assert "Deployment failure body." in other.body
    assert "Final-checks failure body." not in other.body


def test_breadcrumb_ancestor_suffix_need_not_start_at_root() -> None:
    # "# Title" is above both parents; a chain starting at "##" (not the
    # "#" root) still matches as an ancestor-chain SUFFIX.
    doc = """\
# Root

## Parent A

### Leaf

A body.

## Parent B

### Leaf

B body.
"""
    span = bound_span(doc, "## Parent B > ### Leaf")
    assert "B body." in span.body
    assert "A body." not in span.body


def test_breadcrumb_segment_with_gt_in_text_is_simple_anchor() -> None:
    # "### Use A > B form" splits into ["### Use A", "B form"]; "B form"
    # is not a heading, so the whole string is a SIMPLE literal anchor,
    # identical to today's behavior.
    doc = "# Top\n\n### Use A > B form\n\nbody\n"
    span = bound_span(doc, "### Use A > B form")
    assert span.body.startswith("### Use A > B form")
    assert "body" in span.body


def test_breadcrumb_still_ambiguous_raises() -> None:
    # The breadcrumb itself repeats: identical parent+leaf chains twice.
    doc = """\
## Parent

### Leaf

first

## Parent

### Leaf

second
"""
    with pytest.raises(AnchorAmbiguousError):
        bound_span(doc, "## Parent > ### Leaf")


def test_breadcrumb_not_found_raises() -> None:
    with pytest.raises(AnchorNotFoundError):
        bound_span(_DUP_DOC, "## Nowhere > ### Failure handling")


def test_breadcrumb_trailing_separator_clean_error() -> None:
    # "## Final checks > ### Failure handling > " has an empty trailing
    # segment -> NOT a breadcrumb -> simple-anchor fallthrough -> clean
    # AnchorNotFoundError (never an IndexError).
    with pytest.raises(AnchorNotFoundError):
        bound_span(_DUP_DOC, "## Final checks > ### Failure handling > ")


def test_breadcrumb_empty_segment_clean_error() -> None:
    # Empty middle segment ("## A >  > ### B") -> simple-anchor
    # fallthrough -> clean AnchorNotFoundError.
    with pytest.raises(AnchorNotFoundError):
        bound_span(_DUP_DOC, "## Final checks >  > ### Failure handling")


def test_breadcrumb_fence_aware_per_segment() -> None:
    # A decoy parent AND a decoy leaf inside fenced code blocks must be
    # ignored by BOTH the ancestor walk and the leaf match.
    doc = """\
## Real parent

```sh
## Decoy parent
### Leaf
```

### Leaf

real body

## Other

### Leaf

other body
"""
    span = bound_span(doc, "## Real parent > ### Leaf")
    assert "real body" in span.body
    assert "other body" not in span.body


def test_breadcrumb_crlf_parity() -> None:
    crlf = _DUP_DOC.replace("\n", "\r\n")
    span = bound_span(crlf, "## Deployment > ### Failure handling")
    assert "Deployment failure body." in span.body
    assert "Final-checks failure body." not in span.body


def test_breadcrumb_non_strict_levels_not_found() -> None:
    # Parent association is STRICTLY lower level: a "## B" following
    # "## A" is a SIBLING of A, never its child, so "## A > ## B" cannot
    # resolve.
    doc = "## A\n\na body\n\n## B\n\nb body\n"
    with pytest.raises(AnchorNotFoundError):
        bound_span(doc, "## A > ## B")
