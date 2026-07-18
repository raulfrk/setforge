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
    """Fence ``data`` between two ``token`` lines as inert content; the caller
    passes a fresh per-call token so fenced data can't spoof the delimiter.

    ``data`` is display/prompt-only (embedded verbatim into the Claude prompt,
    never hashed or reconstructed), so a stray non-UTF-8 byte decodes with
    ``errors="replace"`` — rendering as U+FFFD rather than crashing the prompt."""
    return f"--{label}--\n{token}\n{data.decode('utf-8', errors='replace')}\n{token}"


def _strip_fence(text: str) -> str:
    lines = text.strip("\n").splitlines()
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1])
    return text


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
