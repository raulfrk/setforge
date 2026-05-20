"""Validate-error formatting per mockup D (setforge-tmln).

Two error categories with distinct UX:

- **YAML PARSE ERROR** — one-line ``✗ YAML PARSE ERROR (file:line): msg``.
  No snippet/pointer — the parser failed at a structural level so any
  slice of the source may be unsafe (mid-token, mid-string, etc.).

- **SCHEMA VALIDATION ERROR** — multi-line shape per mockup D:
  ``✗ SCHEMA VALIDATION ERROR`` header, indented snippet, ``←─── line N``
  marker on the offending line, ``^^^^`` underline beneath the offending
  value, optional ``Did you mean '<close-match>'`` suggestion gated by
  Levenshtein ≤ ``max_distance`` over a stdlib ``difflib`` pre-filter,
  and a ``Fix: ...`` action hint.

The close-match suggester deliberately combines a permissive ``difflib``
pre-filter (cheap, character-level ratio) with a strict
:func:`setforge._levenshtein.levenshtein` distance gate. ``difflib``
alone is too noisy for one-token typos (cutoff=0.5 surfaces 50%-similar
strings as "matches"); Levenshtein alone over a long candidate list is
slower than the difflib-first pipeline. The combination is fast AND
free of false-positive "Did you mean" suggestions.

ANSI handling lives in :func:`setforge.cli.validate._check_local_yaml`,
NOT here — the formatters return plain strings; the caller wraps them
when ``sys.stdout.isatty()``.
"""

from __future__ import annotations

import difflib
from pathlib import Path

from setforge._levenshtein import levenshtein


def suggest_close_match(
    word: str, candidates: list[str], max_distance: int = 2
) -> str | None:
    """Return the single best close-match candidate or ``None``.

    Two-stage pipeline: stdlib :func:`difflib.get_close_matches` produces
    up to 3 candidates ordered by descending similarity ratio (cutoff
    0.5); the first whose :func:`setforge._levenshtein.levenshtein`
    distance is ``<= max_distance`` wins. The hard Levenshtein gate
    prevents "Did you mean" false-positives that difflib's ratio would
    let through (anti-smell from SPEC 9).
    """
    pre = difflib.get_close_matches(word, candidates, n=3, cutoff=0.5)
    for c in pre:
        if levenshtein(word, c) <= max_distance:
            return c
    return None


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
