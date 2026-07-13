from __future__ import annotations

from setforge.ui.text import sanitize_controls


def test_c0_control_chars_become_caret_notation() -> None:
    assert sanitize_controls("a\x07b") == "a^Gb"
    assert sanitize_controls("\x1b") == "^["


def test_del_becomes_caret_question() -> None:
    assert sanitize_controls("x\x7fy") == "x^?y"


def test_newline_passes_through() -> None:
    assert sanitize_controls("a\nb") == "a\nb"


def test_printable_and_unicode_are_unchanged() -> None:
    assert sanitize_controls("héllo 世界") == "héllo 世界"


def test_empty_string() -> None:
    assert sanitize_controls("") == ""
