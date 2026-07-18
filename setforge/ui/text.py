from __future__ import annotations


def sanitize_controls(text: str) -> str:
    """``text`` with terminal control characters rewritten as caret notation.

    Each C0 control (``ord < 0x20``) becomes ``^X`` where ``X = chr(code + 0x40)``
    (e.g. ``\\t`` → ``^I``, ``NUL`` → ``^@``); ``DEL`` (``0x7F``) becomes ``^?``.
    Newline (``\\n``) is the sole exception — passed through unescaped so
    multi-line text keeps its line breaks. All other characters are returned
    as-is, so untrusted bytes can never drive the terminal.
    """
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
