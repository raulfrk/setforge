"""Unit tests for setforge.host_local_inject.resolve_anchor (setforge-xsco).

Covers the 5 anchor kinds plus error paths (not found, ambiguous,
fenced-code-block skipping, CRLF normalisation, after-section against
existing user-section markers).
"""

from __future__ import annotations

import pytest

from setforge.errors import AnchorAmbiguousError, AnchorNotFoundError
from setforge.host_local_inject import (
    inject_all,
    inject_host_local_section,
    resolve_anchor,
)
from setforge.sections import extract_marker_hashes, hash_sections
from setforge.source import (
    AnchorAfterHeading,
    AnchorAfterSection,
    AnchorAtEndOfFile,
    AnchorAtStartOfFile,
    AnchorBeforeHeading,
    HostLocalSection,
    HostLocalSectionName,
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

        Capture-back path (setforge-xsco): host-local sections injected
        by `install` via local.yaml must be stripped from live before
        write-back to tracked. Pairs the user authored directly in
        tracked (not in the names set) must survive.
        """
        from setforge.sections import strip_host_local_sections

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
        from setforge.sections import strip_host_local_sections

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


class TestInjectHostLocalSection:
    """End-to-end splice preserves the post-install hash invariant."""

    def test_inject_preserves_hash_invariant(self) -> None:
        text = "# Title\n\n## Workflow\n\nbody\n"
        out = inject_host_local_section(
            text, "work-overrides", AnchorAfterHeading(value="Workflow"), "INJECTED"
        )
        assert extract_marker_hashes(out) == hash_sections(out)

    def test_inject_uses_host_local_semantics_keyword(self) -> None:
        text = "# Title\n\n## Workflow\n\n"
        out = inject_host_local_section(
            text, "w", AnchorAfterHeading(value="Workflow"), "x"
        )
        assert "start host-local w" in out
        assert "end host-local w" in out

    def test_inject_rejects_duplicate_section_name(self) -> None:
        """Defensive guard: re-injection of an existing name raises.

        Direct callers of ``inject_host_local_section`` that bypass the
        body-replace path in ``inject_all`` would otherwise produce a
        malformed file with two pairs sharing one name. The guard catches
        this at the splice boundary.
        """
        dup_name = HostLocalSectionName("dup")
        once = inject_host_local_section(
            "# T\n", dup_name, AnchorAtEndOfFile(), "first"
        )
        with pytest.raises(AnchorAmbiguousError):
            inject_host_local_section(once, dup_name, AnchorAtEndOfFile(), "second")


class TestInjectAllIdempotency:
    """``inject_all`` updates existing sections in place — no duplicate pair."""

    def test_re_inject_updates_body_does_not_duplicate(self) -> None:
        sections = {
            "s": HostLocalSection(anchor=AnchorAtEndOfFile(), body="first body")
        }
        once = inject_all("# T\n", sections)
        sections2 = {
            "s": HostLocalSection(anchor=AnchorAtEndOfFile(), body="updated body")
        }
        twice = inject_all(once, sections2)
        assert twice.count("start host-local s") == 1
        assert "updated body" in twice
        assert "first body" not in twice

    def test_hash_invariant_holds_after_re_inject(self) -> None:
        sections = {"s": HostLocalSection(anchor=AnchorAtEndOfFile(), body="initial")}
        out = inject_all(inject_all("# T\n", sections), sections)
        assert extract_marker_hashes(out) == hash_sections(out)
