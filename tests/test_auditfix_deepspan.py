"""Regression tests for the deep structural-span pin lossy-writeback fix.

A ``deep=True`` PINNED structural span (the schema-2.0 carrier of the legacy
``preserve_user_keys_deep`` semantics) used to deep-merge over a
COMMENT-STRIPPED plain copy of the merged subtree and write it back with the
plain ``set_at_path`` seam, destroying every inline comment (and, for JSONC,
all formatting/indentation) in the pinned subtree on every install. The fix
deep-merges live's plain snapshot OVER the still-WRAPPED merged node in place
and splices it back via the comment-bearing ``set_node_at_path`` seam, so
untouched keys (and their comment tokens / formatting) survive byte-for-byte.

These tests assert comment/formatting survival on the deep path; the sibling
``tests/test_spans_deep_capture_mode.py`` already gates key-survival semantics.
"""

from __future__ import annotations

from pathlib import Path

from setforge.config import Disposition
from setforge.disposition_merge import resolve_file
from setforge.spans import SpanEntry, SpanKind


def _resolve(dst: str, base: str, live: str, tracked: str, span: SpanEntry):
    return resolve_file(
        disposition=Disposition.SHARED,
        dst=Path(dst),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
        structural_spans=[span],
    )


def test_deep_pinned_span_preserves_comments_yaml() -> None:
    """A deep PINNED YAML span keeps every inline comment in the subtree.

    ``a`` is edited live (takes live's value AND live's comment), ``b`` is
    untouched, ``c`` is a tracked-only add the 3-way merge kept. Pre-fix all
    three comments were stripped; post-fix all survive.
    """
    base = "settings:\n  a: 1  # comment-a\n  b: 2  # comment-b\n"
    tracked = (
        "settings:\n  a: 1  # comment-a\n  b: 2  # comment-b\n  c: 3  # comment-c\n"
    )
    live = "settings:\n  a: 99  # comment-a-live\n  b: 2  # comment-b\n"
    span = SpanEntry(anchor="settings", kind=SpanKind.PINNED, deep=True)

    result = _resolve("settings.yaml", base, live, tracked, span)

    assert "a: 99" in result.text
    assert "# comment-a-live" in result.text
    assert "# comment-b" in result.text
    assert "# comment-c" in result.text
    # tracked-only sub-key (and its comment) survive the deep merge.
    assert "c: 3" in result.text


def test_deep_pinned_span_preserves_comments_and_indent_jsonc() -> None:
    """A deep PINNED JSONC span keeps trailing comments AND 2-space indentation.

    Both keys are present on all three sides; live edits only ``fontSize``. The
    pre-fix path collapsed the subtree to a flattened, comment-free object; the
    fix preserves both ``//`` comments and the original indentation.
    """
    base = (
        '{\n  "editor": {\n'
        '    "fontSize": 12, // size\n'
        '    "theme": "dark" // theme-comment\n'
        "  }\n}\n"
    )
    tracked = base
    live = (
        '{\n  "editor": {\n'
        '    "fontSize": 14, // size-live\n'
        '    "theme": "dark" // theme-comment\n'
        "  }\n}\n"
    )
    span = SpanEntry(anchor="editor", kind=SpanKind.PINNED, deep=True)

    result = _resolve("settings.jsonc", base, live, tracked, span)

    assert '"fontSize": 14' in result.text
    assert "// size-live" in result.text
    assert "// theme-comment" in result.text
    # 2-space member indentation is unchanged (no flattening).
    assert '    "fontSize"' in result.text
    assert '    "theme"' in result.text


def test_deep_pinned_span_list_replace_keeps_sibling_comment_yaml() -> None:
    """A list sub-value is whole-replaced with live's; a sibling comment survives.

    The deep merge whole-replaces a shared list with live's list (matching the
    plain deep-merge terminal), but the sibling scalar's inline comment is left
    byte-identical because only live-touched keys are written.
    """
    base = "settings:\n  items:\n  - 1\n  - 2\n  note: keep  # note-comment\n"
    tracked = base
    live = "settings:\n  items:\n  - 9\n  note: keep  # note-comment\n"
    span = SpanEntry(anchor="settings", kind=SpanKind.PINNED, deep=True)

    result = _resolve("settings.yaml", base, live, tracked, span)

    assert "- 9" in result.text
    assert "- 1" not in result.text  # live's list won wholesale.
    assert "# note-comment" in result.text


def test_deep_pinned_span_scalar_anchor_whole_replaces() -> None:
    """A deep pin whose anchor resolves to a SCALAR is a degenerate terminal.

    The deep-merge guard only engages on a mapping snapshot over a mapping
    node; a scalar anchor falls through to the plain ``set_at_path`` terminal
    so live whole-replaces, matching the legacy overlay's scalar/list terminal.
    """
    base = "x: 1\n"
    tracked = "x: 1\n"
    live = "x: 5\n"
    span = SpanEntry(anchor="x", kind=SpanKind.PINNED, deep=True)

    result = _resolve("s.yaml", base, live, tracked, span)

    assert "x: 5" in result.text
