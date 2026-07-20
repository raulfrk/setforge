from __future__ import annotations

#: Unicode bidirectional formatting controls (overrides + isolates). Passed
#: through, these let untrusted config text VISUALLY reorder the diff/inspect
#: view (a Trojan-Source-class spoof), so they are neutralized to a visible
#: ``<U+XXXX>`` token rather than acting on the display. Scoped to exactly the
#: override/embedding/isolate set — legitimate RTL LETTERS are untouched.
_BIDI_CONTROLS = frozenset(range(0x202A, 0x202F)) | frozenset(range(0x2066, 0x206A))


def sanitize_controls(text: str) -> str:
    """``text`` with terminal control characters rewritten as caret notation.

    Each C0 control (``ord < 0x20``) becomes ``^X`` where ``X = chr(code + 0x40)``
    (e.g. ``\\t`` → ``^I``, ``NUL`` → ``^@``); ``DEL`` (``0x7F``) becomes ``^?``;
    each C1 control (``0x80``-``0x9F``, incl. ``CSI`` = ``0x9B``) becomes ``^X``
    where ``X = chr(code - 0x40)``. Unicode bidi-override / isolate controls
    (:data:`_BIDI_CONTROLS`) become a visible ``<U+XXXX>`` token so untrusted
    text cannot visually reorder the display (Trojan-Source spoofing). Newline
    (``\\n``) is the sole exception — passed through unescaped so multi-line
    text keeps its line breaks. Every other character is returned as-is, so
    untrusted bytes can never drive the terminal.
    """
    # Caret / token notation so untrusted bytes can't drive or reorder the terminal.
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if ch == "\n":
            out.append(ch)
        elif code < 0x20:
            out.append(f"^{chr(code + 0x40)}")
        elif code == 0x7F:
            out.append("^?")
        elif 0x80 <= code <= 0x9F:
            out.append(f"^{chr(code - 0x40)}")
        elif code in _BIDI_CONTROLS:
            out.append(f"<U+{code:04X}>")
        else:
            out.append(ch)
    return "".join(out)
