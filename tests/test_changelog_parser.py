"""Unit tests for :mod:`setforge._changelog_parser` (offline, fixture-driven)."""

from __future__ import annotations

import textwrap

from setforge._changelog_parser import parse_changelog


def test_parse_changelog_returns_none_when_version_absent() -> None:
    text = textwrap.dedent(
        """\
        # Changelog

        ## [Unreleased]

        ## [0.2.0] - 2026-05-01
        - first release
        """
    )
    assert parse_changelog(text, "0.3.0") is None


def test_parse_changelog_canonical_bracketed_form() -> None:
    text = textwrap.dedent(
        """\
        # Changelog

        ## [0.3.0] - 2026-05-19

        ### Added
        - cool feature

        ### Changed
        - something

        ## [0.2.0] - 2026-04-01
        - prior
        """
    )
    result = parse_changelog(text, "0.3.0")
    assert result is not None
    assert "cool feature" in result
    assert "prior" not in result
    assert "### Added" in result
    assert "### Changed" in result


def test_parse_changelog_skips_unreleased_heading() -> None:
    text = textwrap.dedent(
        """\
        # Changelog

        ## [Unreleased]
        - WIP

        ## [0.3.0] - 2026-05-19
        - shipped feature
        """
    )
    result = parse_changelog(text, "0.3.0")
    assert result is not None
    assert "shipped feature" in result
    assert "WIP" not in result


def test_parse_changelog_paren_date_variant() -> None:
    text = textwrap.dedent(
        """\
        ## 0.3.0 (2026-05-19)
        - feature

        ## 0.2.0 (2026-04-01)
        - older
        """
    )
    result = parse_changelog(text, "0.3.0")
    assert result is not None
    assert "feature" in result
    assert "older" not in result


def test_parse_changelog_v_prefix_variant() -> None:
    text = textwrap.dedent(
        """\
        ## v0.3.0
        - v-prefixed
        """
    )
    result = parse_changelog(text, "0.3.0")
    assert result is not None
    assert "v-prefixed" in result


def test_parse_changelog_target_v_prefix_input_is_normalized() -> None:
    """Caller may pass ``v0.3.0`` instead of ``0.3.0``."""
    text = textwrap.dedent(
        """\
        ## [0.3.0] - 2026-05-19
        - body
        """
    )
    result = parse_changelog(text, "v0.3.0")
    assert result is not None
    assert "body" in result


def test_parse_changelog_does_not_split_on_heading_inside_fence() -> None:
    """A ``## [0.4.0]`` line inside a fenced code block must not terminate."""
    text = textwrap.dedent(
        """\
        ## [0.3.0] - 2026-05-19
        - intro

        ```markdown
        ## [0.4.0] - fake-inside-fence
        ```

        - tail
        """
    )
    result = parse_changelog(text, "0.3.0")
    assert result is not None
    assert "intro" in result
    assert "tail" in result
    assert "fake-inside-fence" in result  # because it's inside the fence


def test_parse_changelog_strips_trailing_link_refs() -> None:
    text = textwrap.dedent(
        """\
        ## [0.3.0] - 2026-05-19
        - body
        [0.3.0]: https://example/0.3.0
        [0.2.0]: https://example/0.2.0
        """
    )
    result = parse_changelog(text, "0.3.0")
    assert result is not None
    assert result.endswith("- body")
    assert "https://example" not in result


def test_parse_changelog_strips_trailing_blank_lines() -> None:
    text = textwrap.dedent(
        """\
        ## [0.3.0] - 2026-05-19
        - body



        """
    )
    result = parse_changelog(text, "0.3.0")
    assert result == "- body"


def test_parse_changelog_terminates_at_next_release_heading() -> None:
    text = textwrap.dedent(
        """\
        ## [0.3.0] - 2026-05-19
        - keep me
        ## [0.2.0] - 2026-04-01
        - drop me
        """
    )
    result = parse_changelog(text, "0.3.0")
    assert result is not None
    assert "keep me" in result
    assert "drop me" not in result


def test_parse_changelog_empty_body_returns_empty_string() -> None:
    text = textwrap.dedent(
        """\
        ## [0.3.0] - 2026-05-19

        ## [0.2.0] - 2026-04-01
        - older
        """
    )
    result = parse_changelog(text, "0.3.0")
    assert result == ""
