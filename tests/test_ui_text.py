from __future__ import annotations

from setforge.ui.text import sanitize_controls


def test_c0_control_chars_become_caret_notation() -> None:
    assert sanitize_controls("a\x07b") == "a^Gb"
    assert sanitize_controls("\x1b") == "^["


def test_del_becomes_caret_question() -> None:
    assert sanitize_controls("x\x7fy") == "x^?y"


def test_c1_control_chars_become_caret_notation() -> None:
    assert sanitize_controls("a\x9bb") == "a^[b"
    assert sanitize_controls("a\x9db") == "a^]b"
    assert sanitize_controls("\x80") == "^@"
    assert sanitize_controls("\x9f") == "^_"


def test_newline_passes_through() -> None:
    assert sanitize_controls("a\nb") == "a\nb"


def test_printable_and_unicode_are_unchanged() -> None:
    assert sanitize_controls("héllo 世界") == "héllo 世界"


def test_empty_string() -> None:
    assert sanitize_controls("") == ""


def test_sanitize_controls_neutralizes_bidi_override() -> None:
    """A bidi-override (U+202E RLO) must render visibly, not pass through to
    visually reorder the diff view (Trojan-Source spoofing); legit RTL letters
    are untouched."""
    from setforge.ui.text import sanitize_controls

    out = sanitize_controls("safe‮EVIL")
    assert "‮" not in out
    assert "<U+202E>" in out
    assert sanitize_controls("א") == "א"  # Hebrew alef, untouched
