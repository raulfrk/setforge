"""Vendor-free Keep-a-Changelog scanner for ``setforge upgrade``.

Single public entry point: :func:`parse_changelog`. Given the raw text
of a ``CHANGELOG.md`` (Keep-a-Changelog flavored) and a target version
string, return the body of that release's section, or ``None`` when the
target version is not listed.

Two heading shapes are recognized:

* canonical Keep-a-Changelog: ``## [0.3.0] - 2026-05-19``
* common Markdown variant:    ``## 0.3.0 (2026-05-19)`` or ``## v0.3.0``

The scanner tracks triple-backtick code-fence depth so a heading-looking
line inside a fenced code block does not terminate the slice early.
Trailing reference-link blocks (``[0.3.0]: https://...``) are stripped
from the returned body — they belong to the document footer, not the
release.
"""

from __future__ import annotations

import re

# ``## `` headings whose token after ``## `` is either ``[X.Y.Z]`` or
# ``vX.Y.Z`` or bare ``X.Y.Z``. The captured group ``ver`` is the
# normalized X.Y.Z (without leading ``v`` or surrounding brackets).
_HEADING_RE: re.Pattern[str] = re.compile(
    r"^##\s+\[?v?(?P<ver>\d+\.\d+\.\d+)\]?",
    re.IGNORECASE,
)
# Used to detect the *next* release heading after we have begun
# collecting the target's body. Same shape, used only to terminate.
_NEXT_HEADING_RE: re.Pattern[str] = re.compile(r"^##\s+\[?v?\d+\.\d+\.\d+\]?")
# Reference-link line; stripped from the tail of the captured body so
# the user does not see a wall of ``[0.3.0]: https://...`` URL refs.
_LINK_REF_RE: re.Pattern[str] = re.compile(r"^\[[^\]]+\]:\s+\S+")
_FENCE_RE: re.Pattern[str] = re.compile(r"^```")
# Skip the "[Unreleased]" placeholder per Keep-a-Changelog convention.
_UNRELEASED_RE: re.Pattern[str] = re.compile(
    r"^##\s+\[?unreleased\]?",
    re.IGNORECASE,
)


def parse_changelog(text: str, target_version: str) -> str | None:
    """Extract the release-notes body for ``target_version`` from ``text``.

    Returns the body lines joined by newlines (no trailing newline) or
    ``None`` when ``target_version`` is not a heading in ``text``. The
    body is the lines between the matched ``## `` heading and the next
    ``## `` heading (or EOF). Trailing reference-link lines and trailing
    blank lines are stripped.
    """
    target = target_version.lstrip("v")
    state = _ScanState()
    for line in text.splitlines():
        _scan_line(line, target, state)
        if state.done:
            break
    if not state.inside_target:
        return None
    return _strip_trailing(state.collected)


class _ScanState:
    """Mutable accumulator for the line-by-line scanner."""

    __slots__ = ("collected", "done", "in_fence", "inside_target")

    def __init__(self) -> None:
        self.in_fence: bool = False
        self.inside_target: bool = False
        self.collected: list[str] = []
        self.done: bool = False


def _scan_line(line: str, target: str, state: _ScanState) -> None:
    """Apply one line of input to ``state`` (toggles fence / collects / ends)."""
    if _FENCE_RE.match(line):
        state.in_fence = not state.in_fence
        _maybe_collect(line, state)
        return
    if state.in_fence:
        _maybe_collect(line, state)
        return
    if _UNRELEASED_RE.match(line):
        if state.inside_target:
            state.done = True
        return
    heading_match = _HEADING_RE.match(line)
    if heading_match is not None:
        _handle_heading(heading_match.group("ver"), target, state)
        return
    _maybe_collect(line, state)


def _maybe_collect(line: str, state: _ScanState) -> None:
    """Append ``line`` to the buffer when the scanner is inside the target."""
    if state.inside_target:
        state.collected.append(line)


def _handle_heading(version: str, target: str, state: _ScanState) -> None:
    """Open the target slice on first match; terminate on the next heading."""
    if state.inside_target:
        state.done = True
        return
    if version == target:
        state.inside_target = True


def _strip_trailing(lines: list[str]) -> str:
    """Drop trailing blank lines + trailing reference-link block; join."""
    out = list(lines)
    while out and _LINK_REF_RE.match(out[-1]):
        out.pop()
    while out and out[-1].strip() == "":
        out.pop()
    # Also strip leading blank lines so the body begins at the first
    # content line.
    while out and out[0].strip() == "":
        out.pop(0)
    return "\n".join(out)
