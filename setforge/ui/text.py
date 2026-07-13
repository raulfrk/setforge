from __future__ import annotations


def sanitize_controls(text: str) -> str:
    # Caret notation so untrusted bytes can't drive the terminal.
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
