"""Shared pure UI/prompt helpers for the reconcile Claude sub-flows.

These leaf helpers were byte-duplicated across the conflict wizard
(:mod:`setforge.reconcile.wizard`), the Claude-merge callback
(:mod:`setforge.reconcile.claude_merge`), the share-draft flow
(:mod:`setforge.reconcile.share_draft`), and the interactive ``stage`` walk
(:mod:`setforge.cli.stage`). They are collected here so there is ONE definition
of each — a renamed / weakened copy of the injection fence or the style-key
guard is a security regression, so a single source removes that drift risk.

**Deliberately excluded** (they stay per-module): the untrusted-output gates
``_validate`` / ``_structured_validate`` diverge on purpose across the three
callers and must NOT be collapsed, and each caller keeps its own post-edit
revalidation policy. This module holds only the format/display/editor plumbing.

Import discipline: this is a leaf — it depends ONLY on ``ui.theme``,
``ui.widgets``, and ``_editor``, never back into ``claude_merge`` / ``share_draft``
/ ``wizard`` / ``cli.stage``, so no import cycle is possible.
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
    """The Tokyo Night role palette as a prompt_toolkit ``Style``.

    :func:`~setforge.ui.theme.pt_style` returns *reference*-form keys
    (``class:success``), the shape a fragment uses to point at a class;
    :meth:`Style.from_dict` keys are *definition*-form (bare ``success``), so the
    ``class:`` prefix is stripped. Merged over the widgets' own button classes
    inside ``button_bar`` / ``text_prompt``.
    """
    rules = {
        key.removeprefix("class:"): value for key, value in pt_style(THEME).items()
    }
    return Style.from_dict(rules)


def _fenced(label: str, data: bytes, token: str) -> str:
    """One labelled region fenced between ``token`` lines as inert DATA.

    The random per-invocation ``token`` is what makes the fenced text inert —
    nothing inside ``data`` can close the fence or pose as an instruction. The
    SAME token must also flow into the prompt header, so callers mint it once per
    prompt and pass it here unchanged.

    Precondition: ``data`` decodes as UTF-8 — guaranteed by each caller's
    UTF-8 button/stage gate before drafting is reachable.
    """
    return f"--{label}--\n{token}\n{data.decode('utf-8')}\n{token}"


def _strip_fence(text: str) -> str:
    """Drop a single wrapping ``` code fence, if the whole draft is wrapped in one."""
    lines = text.strip("\n").splitlines()
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1])
    return text


def _sanitize_controls(text: str) -> str:
    """Map C0 controls (except newline) and DEL to caret notation for display.

    A raw ``\\x00``/ESC/control char written into a ``FormattedTextControl`` is
    emitted as a literal screen cell and corrupts the panel; newline is kept so
    the body stays multi-line. Tab becomes ``^I`` and DEL becomes ``^?`` — this
    is a DISPLAY sanitiser, distinct from share-draft's ``_FORBIDDEN`` accept
    gate (which permits ``\\t``/``\\n`` in stored bytes).
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
    """Open ``$EDITOR`` seeded with the draft; return edited bytes or :data:`CANCEL`.

    A benign editor abort (``CalledProcessError``) or a non-UTF-8 read-back
    re-prompts (``CANCEL``); an editor-config fault (``SetforgeError`` from
    :func:`run_editor`) propagates. The tempfile is unlinked on every outcome.

    This is only the editor round-trip; each caller decides independently whether
    to re-validate the returned bytes (claude_merge does not; share_draft does).
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
