"""Terminal-display text helpers shared across the UI layer."""

from __future__ import annotations


def sanitize_controls(text: str) -> str:
    """Render control chars as inert caret notation (``^G``, ``^?``) so
    untrusted bytes can't drive the terminal; newlines pass through."""
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if ch == "\n":
            out.append(ch)
        elif code < 0x20:
            out.append(f"^{chr(code + 0x40)}")
        elif code == 0x7F:
            out.append("^?")
        else:
            out.append(ch)
    return "".join(out)
