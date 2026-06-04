"""Pure, I/O-free line-based stored-base 3-way merge engine for markdown.

This module is intentionally I/O-free and format-agnostic at the line level:
it operates on whole text blobs split into lines and reconciles a
``{base, ours=live, theirs=upstream}`` triple. Section markers or any other
markup in the text are OPAQUE — they receive no special handling; the engine
is a plain line-oriented 3-way merge.

The merge is driven by :class:`merge3.Merge3` using
:class:`patiencediff.PatienceSequenceMatcher` as the diff matcher, which gives
stable anchoring (unique lines pin alignment) and avoids the false-conflict
collapse a naive longest-match matcher can produce on adjacent edits.

Trailing-newline handling: inputs are split with ``str.splitlines(keepends=True)``
so each line carries its own terminator. The final line's terminator is stripped
from all three sides BEFORE the merge and the ``ours`` terminator is restored
AFTER, so a base/ours/theirs disagreement on the final ``\\n`` is not a spurious
last-line conflict and a clean ``merge_markdown(x, x, x)`` stays byte-exact.
"""

from dataclasses import dataclass
from typing import cast

import merge3
from patiencediff import PatienceSequenceMatcher

# merge3 calls ``sequence_matcher(None, a, b)`` and only relies on a
# ``get_matching_blocks``-style surface, which PatienceSequenceMatcher provides.
# Its published type is ``difflib.SequenceMatcher`` rather than merge3's own
# ``SequenceMatcherProtocol``, so the cast restates the structural compatibility
# the runtime already relies on.
_PATIENCE_MATCHER = cast(
    "type[merge3.SequenceMatcherProtocol[str]]", PatienceSequenceMatcher
)


@dataclass(frozen=True, slots=True)
class LineConflict:
    """A single conflicting hunk, carrying the three sides' line-blocks.

    Each field is the list of lines (terminators kept) that the corresponding
    side contributed to the conflicting region. Any side may be empty (e.g. a
    both-add conflict has an empty ``base``).
    """

    base: list[str]
    ours: list[str]
    theirs: list[str]


@dataclass(frozen=True, slots=True)
class MarkdownMergeResult:
    """Outcome of :func:`merge_markdown`.

    ``merged_text`` holds the reconciled text when ``clean`` is True and is
    ``None`` on conflict. ``conflicts`` is empty when ``clean`` is True and
    otherwise lists every conflicting hunk in document order.
    """

    clean: bool
    merged_text: str | None
    conflicts: list[LineConflict]


def _split_strip_final(text: str) -> tuple[list[str], str]:
    """Split ``text`` into kept-terminator lines, stripping the final terminator.

    Returns the line list (with the last line's trailing ``\\r\\n``/``\\n``/``\\r``
    removed) plus the terminator that was stripped (``""`` when the text has no
    final terminator or is empty). Normalizing the final terminator across all
    three sides keeps a final-newline-only disagreement from registering as a
    distinct last line, which would otherwise be a spurious conflict.
    """
    lines = text.splitlines(keepends=True)
    if not lines:
        return [], ""
    last = lines[-1]
    # Strip exactly ONE trailing terminator (``\r\n``/``\n``/``\r`` as a unit),
    # not a run of newline chars, so a doubled terminator or a literal final
    # ``\r`` line is not over-stripped.
    if last.endswith("\r\n"):
        terminator = "\r\n"
    elif last.endswith(("\n", "\r")):
        terminator = last[-1]
    else:
        terminator = ""
    lines[-1] = last[: len(last) - len(terminator)]
    return lines, terminator


def _restore_final(lines: list[str], terminator: str) -> str:
    """Join ``lines`` and reattach ``terminator`` to the document.

    The terminator was removed by :func:`_split_strip_final`; reattaching the
    ``ours`` terminator makes a clean self-merge byte-exact. The guard is on
    ``lines`` being non-empty, NOT on the joined text: a sole-terminator input
    (e.g. ``"\\n"``) splits to ``lines=[""]`` with the terminator carried
    separately, so its joined text is empty yet it must still restore its
    terminator. A genuinely empty document yields ``lines=[]`` and returns
    ``""`` with no spurious terminator.
    """
    text = "".join(lines)
    if lines:
        return text + terminator
    return text


def merge_markdown(base: str, ours: str, theirs: str) -> MarkdownMergeResult:
    """Perform a whole-file line-based 3-way merge of ``base``/``ours``/``theirs``.

    ``ours`` is the live side and ``theirs`` the upstream side; ``base`` is the
    stored common ancestor. The merge is line-oriented with markup treated as
    opaque text. On success returns a clean result whose ``merged_text`` is
    byte-exact for a self-merge and carries the ``ours`` trailing-newline
    convention; on any conflicting hunk returns ``clean=False``,
    ``merged_text=None`` and the list of :class:`LineConflict` hunks.
    """
    base_lines, _base_term = _split_strip_final(base)
    ours_lines, ours_term = _split_strip_final(ours)
    theirs_lines, _theirs_term = _split_strip_final(theirs)

    merger = merge3.Merge3(
        base_lines,
        ours_lines,
        theirs_lines,
        sequence_matcher=_PATIENCE_MATCHER,
    )

    resolved: list[str] = []
    conflicts: list[LineConflict] = []

    for group in merger.merge_groups():
        match group:
            case ("unchanged", lines):
                resolved.extend(lines)
            case ("a", lines):
                resolved.extend(lines)
            case ("b", lines):
                resolved.extend(lines)
            case ("same", lines):
                # Both sides made the identical change -> take it ONCE.
                resolved.extend(lines)
            case ("conflict", base_block, a_block, b_block):
                conflicts.append(
                    LineConflict(
                        base=list(base_block),
                        ours=list(a_block),
                        theirs=list(b_block),
                    )
                )
            case _:  # pragma: no cover - defensive against merge3 API drift
                raise ValueError(f"unexpected merge group tag: {group[0]!r}")

    if conflicts:
        return MarkdownMergeResult(clean=False, merged_text=None, conflicts=conflicts)

    merged_text = _restore_final(resolved, ours_term)
    return MarkdownMergeResult(clean=True, merged_text=merged_text, conflicts=[])
