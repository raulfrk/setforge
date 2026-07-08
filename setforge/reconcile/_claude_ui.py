from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from prompt_toolkit.styles import Style

from setforge._editor import run_editor
from setforge.ui.theme import THEME, pt_style
from setforge.ui.widgets import CANCEL, Cancelled


def _themed_style() -> Style:
    rules = {
        key.removeprefix("class:"): value for key, value in pt_style(THEME).items()
    }
    return Style.from_dict(rules)


def _fenced(label: str, data: bytes, token: str) -> str:
    """A fresh per-call token, not a fixed one, is what keeps this un-spoofable."""
    return f"--{label}--\n{token}\n{data.decode('utf-8')}\n{token}"


def _strip_fence(text: str) -> str:
    lines = text.strip("\n").splitlines()
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1])
    return text


def _sanitize_controls(text: str) -> str:
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


def _edit_draft(seed: str) -> bytes | Cancelled:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", encoding="utf-8", delete=False
    ) as handle:
        handle.write(seed)
        tmp_path = Path(handle.name)
    try:
        run_editor(tmp_path)
        text = tmp_path.read_text(encoding="utf-8")
    except (subprocess.CalledProcessError, UnicodeDecodeError):
        return CANCEL
    finally:
        tmp_path.unlink(missing_ok=True)
    if text == "":
        return CANCEL
    return text.encode("utf-8")
