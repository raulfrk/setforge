"""Unit tests for setforge.host_local_inject.resolve_anchor.

Covers the 5 anchor kinds plus error paths (not found, ambiguous,
fenced-code-block skipping, CRLF normalisation, after-section against
existing user-section markers).
"""

from __future__ import annotations

import pytest

from setforge.errors import AnchorAmbiguousError, AnchorNotFoundError
from setforge.host_local_inject import (
    _resolve_in_section,
    heading_level,
    resolve_anchor,
)
from setforge.source import (
    AnchorAfterHeading,
    AnchorAfterSection,
    AnchorAtEndOfFile,
    AnchorAtStartOfFile,
    AnchorBeforeHeading,
    AnchorInSection,
)


class TestResolveAnchor:
    """Per-kind anchor resolution against rendered markdown text."""

    def test_after_heading_returns_offset_below_heading(self) -> None:
        text = "# Title\n\n## Workflow\n\nbody\n"
        offset = resolve_anchor(text, AnchorAfterHeading(value="Workflow"))
        # Heading is line idx 2 (0-indexed); offset is one below.
        assert offset == 3

    def test_before_heading_returns_offset_of_heading_line(self) -> None:
        text = "# Title\n\n## Workflow\n\nbody\n"
        offset = resolve_anchor(text, AnchorBeforeHeading(value="Workflow"))
        # Heading is line idx 2; before-heading splices at the heading line.
        assert offset == 2

    def test_at_start_of_file_returns_zero(self) -> None:
        text = "# Title\n\nbody\n"
        assert resolve_anchor(text, AnchorAtStartOfFile()) == 0

    def test_at_end_of_file_returns_line_count(self) -> None:
        text = "# Title\n\nbody\n"
        # 3 lines: "# Title", "", "body"
        assert resolve_anchor(text, AnchorAtEndOfFile()) == 3

    def test_after_heading_skips_fenced_code_blocks(self) -> None:
        text = (
            "# Title\n"
            "\n"
            "```python\n"
            "## Workflow\n"  # heading-shaped string inside code fence
            "```\n"
            "\n"
            "## Workflow\n"  # real heading
            "body\n"
        )
        offset = resolve_anchor(text, AnchorAfterHeading(value="Workflow"))
        # The real heading is at line idx 6; offset is 7.
        assert offset == 7

    def test_after_heading_not_found_raises(self) -> None:
        text = "# Title\n\n## Other\nbody\n"
        with pytest.raises(AnchorNotFoundError) as exc_info:
            resolve_anchor(text, AnchorAfterHeading(value="Workflow"))
        assert "Workflow" in str(exc_info.value)

    def test_after_heading_duplicate_raises_ambiguous(self) -> None:
        text = "# Title\n## Workflow\nA\n## Workflow\nB\n"
        with pytest.raises(AnchorAmbiguousError) as exc_info:
            resolve_anchor(text, AnchorAfterHeading(value="Workflow"))
        msg = str(exc_info.value)
        assert "2" in msg
        assert "4" in msg

    def test_crlf_input_normalises_to_lf(self) -> None:
        text = "# Title\r\n\r\n## Workflow\r\nbody\r\n"
        offset = resolve_anchor(text, AnchorAfterHeading(value="Workflow"))
        assert offset == 3


class TestAfterSectionAnchor:
    """The ``after-section`` anchor resolves against existing marker pairs."""

    def test_after_section_returns_offset_below_end_marker(self) -> None:
        text = (
            "# Title\n"
            "<!-- setforge:user-section start shared notes "
            "hash=ee013d9917ee8d6e0fc3dcdee31d77c2f47f7e9fc85f7063e02ae69eb9215385 -->\n"  # noqa: E501 — explanatory long literal
            "body\n"
            "<!-- setforge:user-section end shared notes "
            "hash=ee013d9917ee8d6e0fc3dcdee31d77c2f47f7e9fc85f7063e02ae69eb9215385 -->\n"  # noqa: E501 — explanatory long literal
            "trailing\n"
        )
        offset = resolve_anchor(text, AnchorAfterSection(name="notes"))
        # The end marker is at line 4 (1-indexed); offset is the same value
        # (line after the end marker, 0-indexed).
        assert offset == 4

    def test_after_section_not_found_raises(self) -> None:
        text = "# Title\nbody\n"
        with pytest.raises(AnchorNotFoundError):
            resolve_anchor(text, AnchorAfterSection(name="missing"))

    def test_strip_host_local_sections_drops_named_pairs(self) -> None:
        """``strip_host_local_sections`` removes named host-local pairs only.

        Capture-back path: host-local sections injected
        by `install` via local.yaml must be stripped from live before
        write-back to tracked. Pairs the user authored directly in
        tracked (not in the names set) must survive.
        """
        from setforge.user_section_markers import strip_host_local_sections

        text = (
            "<!-- setforge:user-section start host-local injected hash=a -->\n"
            "injected body\n"
            "<!-- setforge:user-section end host-local injected hash=a -->\n"
            "<!-- setforge:user-section start host-local user-authored hash=b -->\n"
            "user-authored body\n"
            "<!-- setforge:user-section end host-local user-authored hash=b -->\n"
            "<!-- setforge:user-section start shared notes hash=c -->\n"
            "shared body\n"
            "<!-- setforge:user-section end shared notes hash=c -->\n"
        )
        result = strip_host_local_sections(text, names=frozenset({"injected"}))
        assert "injected body" not in result
        assert "injected" not in result.split("\n")[0]  # start marker gone
        assert "user-authored body" in result  # not in names set — kept
        assert "shared body" in result

    def test_strip_host_local_sections_noop_on_empty_names(self) -> None:
        """No-op when no host-local names declared in local.yaml."""
        from setforge.user_section_markers import strip_host_local_sections

        text = (
            "<!-- setforge:user-section start host-local x hash=a -->\n"
            "body\n"
            "<!-- setforge:user-section end host-local x hash=a -->\n"
        )
        assert strip_host_local_sections(text, names=frozenset()) == text

    def test_after_section_offset_is_line_below_end_marker(self) -> None:
        """Explicit 0-based offset semantics for after-section.

        Constructs a 3-line file with a section spanning lines 0-2:
          line 0: start marker
          line 1: body
          line 2: end marker
        ``after-section`` MUST resolve to offset 3 (one past the end
        marker) so the splice lands on a fresh line — same convention
        as the other resolvers (``_resolve_after_heading`` returns
        ``matches[0] + 1`` for "line below"). Guards against off-by-one
        regressions when the walker / enumerate base changes.
        """
        text = (
            "<!-- setforge:user-section start shared notes -->\n"
            "body\n"
            "<!-- setforge:user-section end shared notes -->\n"
        )
        offset = resolve_anchor(text, AnchorAfterSection(name="notes"))
        assert offset == 3


_RESOLVE_TRACKED = "# Title\n\n## A\nx1\nx2\n\n## B\nb1\n"


class TestResolveInSection:
    """In-section anchor resolution (``host_local_inject._resolve_in_section``).

    Re-homed from the retired overlay carve-pipeline suite — pure-function
    unit tests over the surviving in-section resolver.
    """

    def test_exact_preceding_line(self) -> None:
        anchor = AnchorInSection(heading="A", level=2, after_line="x1", offset=1)
        line, fell_back = _resolve_in_section(_RESOLVE_TRACKED, anchor)
        assert fell_back is False
        assert _RESOLVE_TRACKED.splitlines()[line] == "x2"

    def test_offset_when_after_line_absent(self) -> None:
        anchor = AnchorInSection(heading="A", level=2, after_line=None, offset=2)
        line, fell_back = _resolve_in_section(_RESOLVE_TRACKED, anchor)
        assert fell_back is False
        # heading line 2 + 1 + offset 2 = 5, still inside section A (## B is line 6)
        assert line == 5

    def test_falls_back_to_end_of_section(self) -> None:
        anchor = AnchorInSection(heading="A", level=2, after_line="GONE", offset=99)
        line, fell_back = _resolve_in_section(_RESOLVE_TRACKED, anchor)
        assert fell_back is True
        assert _RESOLVE_TRACKED.splitlines()[line] == "## B"

    def test_heading_gone_hard_fails(self) -> None:
        anchor = AnchorInSection(heading="A", level=2, after_line="x1", offset=1)
        with pytest.raises(AnchorNotFoundError):
            _resolve_in_section("# Title\n\n## Z\nz1\n", anchor)

    def test_heading_duplicated_hard_fails(self) -> None:
        anchor = AnchorInSection(heading="A", level=2, after_line="x1", offset=1)
        with pytest.raises(AnchorAmbiguousError):
            _resolve_in_section("## A\nx1\n\n## A\nq\n", anchor)

    def test_section_end_ignores_heading_inside_fence(self) -> None:
        # A ``#``-line inside a fenced code block must NOT close the section,
        # so the fallback end-of-section lands past the fence at the real
        # next same-level heading (``## B``). Guards the fence-aware
        # ``_scan_end`` re-established from the file head.
        text = "## A\nx1\n```sh\n## not a heading inside a fence\n```\nmore\n## B\nb1\n"
        anchor = AnchorInSection(heading="A", level=2, after_line="GONE", offset=99)
        line, fell_back = _resolve_in_section(text, anchor)
        assert fell_back is True
        assert text.splitlines()[line] == "## B"

    def test_section_end_spans_deeper_child_headings(self) -> None:
        # A deeper child heading (``###`` under ``##``) stays INSIDE the
        # section, so the fallback end-of-section lands at the next SAME-level
        # heading (``## B``), never at the child. Guards ``_scan_end``'s
        # ``<= level`` boundary against closing a section on its own child.
        text = "## A\nx1\n### Child\nc1\n## B\nb1\n"
        anchor = AnchorInSection(heading="A", level=2, after_line="GONE", offset=99)
        line, fell_back = _resolve_in_section(text, anchor)
        assert fell_back is True
        assert text.splitlines()[line] == "## B"

    def test_section_end_closes_at_lower_level_heading(self) -> None:
        # A LOWER-level heading (``#`` above a ``##`` section) closes the
        # section. Guards ``_scan_end``'s ``this_level <= level`` boundary: a
        # mutation to ``== level`` would stop a level-1 heading from closing a
        # level-2 section, running the fallback to EOF instead of ``# Top``.
        text = "## A\nx1\n# Top\nt1\n"
        anchor = AnchorInSection(heading="A", level=2, after_line="GONE", offset=99)
        line, fell_back = _resolve_in_section(text, anchor)
        assert fell_back is True
        assert text.splitlines()[line] == "# Top"


class TestHeadingLevel:
    """The pure ATX heading-level classifier folded into host_local_inject."""

    def test_heading_level_counts_hash_run(self) -> None:
        assert heading_level("# Top") == 1
        assert heading_level("## Foo") == 2
        assert heading_level("### Bar") == 3
        assert heading_level("Not a heading") is None
