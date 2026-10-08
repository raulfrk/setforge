"""Validate-error formatting per mockup D.

Two error categories with distinct UX:

- **YAML PARSE ERROR** — one-line ``✗ YAML PARSE ERROR (file:line): msg``.
  No snippet/pointer — the parser failed at a structural level so any
  slice of the source may be unsafe (mid-token, mid-string, etc.).

- **SCHEMA VALIDATION ERROR** — multi-line shape per mockup D:
  ``✗ SCHEMA VALIDATION ERROR`` header, indented snippet, ``←─── line N``
  marker on the offending line, ``^^^^`` underline beneath the offending
  value, optional ``Did you mean '<close-match>'`` suggestion from
  :func:`suggest_close_match`, and a ``Fix: ...`` action hint.

The close-match suggester is a single stdlib :func:`difflib.get_close_matches`
call. Its cutoff sits just under 2/3, the similarity of a one-character slip
in a three-letter key, so short keys such as ``dst`` or ``add`` still get a
suggestion while unrelated names do not.

The formatters emit plain text only — no ANSI codes. The unicode
glyphs (``✗``, ``←───``) are literal characters, not escape
sequences; a non-TTY consumer (CI logs, redirected output) sees
them as-is.
"""

from __future__ import annotations

import difflib
from pathlib import Path

_CLOSE_MATCH_CUTOFF = 0.66


def suggest_close_match(word: str, candidates: list[str]) -> str | None:
    """Return the closest candidate to ``word``, or ``None`` when none is near."""
    matches = difflib.get_close_matches(
        word, candidates, n=1, cutoff=_CLOSE_MATCH_CUTOFF
    )
    return matches[0] if matches else None


def format_yaml_parse_error(path: Path, line: int, col: int, msg: str) -> str:
    """Render the YAML PARSE category — one line, no snippet.

    Parse errors fail at a structural level so the indented snippet +
    underline UX (used by :func:`format_schema_validation_error`) is
    intentionally absent — any slice of the source may be mid-token /
    mid-string and unsafe to render. The column position is surfaced
    via the ``file:line:col`` prefix so editors that parse error
    addresses can jump straight to the failure site.
    """
    return f"✗ YAML PARSE ERROR ({path.name}:{line}:{col}): {msg}"


def format_schema_validation_error(
    path: Path,
    line: int,
    col: int,
    snippet_lines: list[str],
    field_value: str,
    fix_hint: str,
    suggestion: str | None = None,
) -> str:
    """Render the SCHEMA VALIDATION category — multi-line mockup-D shape.

    Layout (each line indented with 4 spaces to match the mockup):

    ::

        ✗ SCHEMA VALIDATION ERROR (<file.name>:<line>):
            <snippet line 1>
            <snippet line N>     ←─── line <line>
                       ^^^^      (underline of field_value at col)
            Did you mean '<suggestion>'?       [only if suggestion is set]
            Fix: <fix_hint>

    The marker line (``←─── line N``) trails the LAST snippet line — the
    one carrying the offending value. The underline line follows it,
    with ``len(field_value)`` carets positioned at ``col`` (1-indexed
    column, matching ruamel's ``.lc.value`` convention).
    """
    indent = "    "
    out: list[str] = [f"✗ SCHEMA VALIDATION ERROR ({path.name}:{line}):"]
    last_idx = len(snippet_lines) - 1
    for i, snippet_line in enumerate(snippet_lines):
        if i == last_idx:
            out.append(f"{indent}{snippet_line}     ←─── line {line}")
        else:
            out.append(f"{indent}{snippet_line}")
    # Underline: pad to the field's column (1-indexed) then emit
    # ``len(field_value)`` carets. Snippet indent (4 spaces) plus
    # ``col - 1`` padding inside the line.
    pad = " " * max(col - 1, 0)
    underline = "^" * max(len(field_value), 1)
    out.append(f"{indent}{pad}{underline}")
    if suggestion is not None:
        out.append(f"{indent}Did you mean '{suggestion}'?")
    out.append(f"{indent}Fix: {fix_hint}")
    return "\n".join(out)
