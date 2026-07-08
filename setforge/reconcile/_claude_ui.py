"""Shared pure UI/prompt helpers for the reconcile Claude sub-flows.

De-duped from wizard/claude_merge/share_draft/cli.stage into one definition
each — a weakened copy of the injection fence or the style-key guard would be
a security regression. Deliberately excluded: ``_validate`` /
``_structured_validate`` diverge on purpose per caller and must stay
per-module, each owning its own revalidation policy.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from prompt_toolkit.styles import Style

from setforge._editor import run_editor
from setforge.ui.theme import THEME, pt_style
from setforge.ui.widgets import CANCEL, Cancelled


def _themed_style() -> Style:
    """Tokyo Night palette as a prompt_toolkit ``Style``.

    ``pt_style`` returns reference-form keys (``class:success``);
    ``Style.from_dict`` needs definition-form (bare ``success``), so the
    ``class:`` prefix is stripped here to avoid silently unstyled output.
    """
    rules = {
        key.removeprefix("class:"): value for key, value in pt_style(THEME).items()
    }
    return Style.from_dict(rules)


def _fenced(label: str, data: bytes, token: str) -> str:
    """One labelled region fenced between ``token`` lines as inert DATA.

    The random per-invocation ``token`` is what makes the fence un-spoofable —
    nothing inside ``data`` can close it or pose as an instruction. Callers mint
    the token once and pass the same one into the prompt header.
    """
    return f"--{label}--\n{token}\n{data.decode('utf-8')}\n{token}"


def _strip_fence(text: str) -> str:
    lines = text.strip("\n").splitlines()
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1])
    return text


def _sanitize_controls(text: str) -> str:
    """Caret-notation controls/DEL for display (a raw control char corrupts a
    ``FormattedTextControl`` cell); distinct from share-draft's storage gate.
    """
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
    """Open ``$EDITOR`` on the draft; a benign abort or non-UTF-8 read-back
    re-prompts (:data:`CANCEL`), an editor-config fault propagates.
    """
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
