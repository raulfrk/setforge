"""Non-interactive disposition merge driver.

Central resolution policy that maps a ``(disposition, auto, base-presence)``
triple to the bytes deployed at a live destination plus a re-baseline
decision. It is the single seam install uses to turn the stored-base 3-way
engines (:mod:`setforge.structural_merge`, :mod:`setforge.markdown_merge`)
into a concrete file outcome, free of any interactive prompting.

Orientation: ``ours = live``, ``theirs = tracked/upstream``. The two
``auto`` modes (reusing :class:`setforge.section_wizard.ReconcileAuto`) read
as ``keep-live = keep ours`` and ``use-tracked = take theirs``.

Three policy axes:

* **disposition** — :data:`~setforge.config.Disposition.PINNED` short-circuits
  to live verbatim (live is authoritative, base untouched);
  ``SHARED`` / ``FORKED`` both run the 3-way merge (the SHARED-vs-FORKED
  difference — whether live edits are captured back to tracked — is the
  caller's concern, not this driver's).
* **base presence** — a ``None`` base is a first run: a disposition file has
  no preserve config, so the fallback is to deploy ``tracked`` verbatim and
  seed the stored base to it.
* **auto + conflict** — a conflicting merge under a set ``auto`` resolves
  every conflict that way and re-baselines; under bare (``auto is None``) the
  conflict is left at ours (keep-live) and re-baselining is DEFERRED so the
  next run re-detects the still-pending divergence.

Format detection is by ``dst`` suffix: JSON/JSONC (``jsonc.is_jsonc_file``)
and ``.yaml`` / ``.yml`` route through the comment-preserving structural
engine; everything else (``.md``, arbitrary text) routes through the
line-based markdown engine. A structurally-incompatible structural file
(a :class:`~setforge.errors.MergeTypeMismatch` shape clash between sides)
falls back to the line-based path on the raw text — a malformed-shape file
still merges as text rather than crashing the install.
"""

import io
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from json5.dumper import ModelDumper
from json5.dumper import dumps as _json5_dumps
from json5.loader import ModelLoader
from json5.loader import loads as _json5_loads
from ruamel.yaml import YAML

from setforge import jsonc
from setforge.config import Disposition
from setforge.errors import MergeTypeMismatch
from setforge.markdown_merge import (
    LineConflict,
    _split_strip_final,
    merge_markdown_segments,
    resolve_segments,
)
from setforge.section_wizard import ReconcileAuto
from setforge.structural_merge import (
    PathConflict,
    merge_structural,
    set_at_path,
)

__all__ = ["FileResolution", "resolve_file"]


@dataclass(frozen=True, slots=True)
class FileResolution:
    """Outcome of :func:`resolve_file`.

    ``text`` is the bytes to write to the live destination. ``conflicts`` is
    empty on a clean merge and otherwise lists every conflicting hunk/path —
    it stays non-empty even when an ``auto`` mode resolved the conflict, so the
    caller can still warn. ``advance_base`` is ``True`` when the caller should
    re-baseline the stored base to ``text``; ``False`` defers (a bare conflict
    is re-detected next run). ``base_absent`` is ``True`` only on the first-run
    fallback (no stored base), where ``text == tracked``.
    """

    text: str
    conflicts: list[LineConflict | PathConflict]
    advance_base: bool
    base_absent: bool


def resolve_file(
    disposition: Disposition,
    dst: Path,
    base: str | None,
    live: str,
    tracked: str,
    auto: ReconcileAuto | None,
) -> FileResolution:
    """Resolve one file to deployed text + a re-baseline decision.

    See the module docstring for the full policy. ``dst`` is used for format
    detection (suffix) only; no filesystem access occurs.
    """
    if disposition is Disposition.PINNED:
        return FileResolution(
            text=live, conflicts=[], advance_base=False, base_absent=False
        )

    if base is None:
        # First run: a disposition file has no preserve config, so the 2-way
        # fallback deploys tracked verbatim and seeds base = text = tracked.
        return FileResolution(
            text=tracked, conflicts=[], advance_base=True, base_absent=True
        )

    if _is_structural(dst):
        try:
            return _resolve_structural(dst, base, live, tracked, auto)
        except MergeTypeMismatch:
            # A structurally-incompatible file (shape clash between sides)
            # still merges as raw text via the line-based path.
            return _resolve_line_based(base, live, tracked, auto)

    return _resolve_line_based(base, live, tracked, auto)


def _is_structural(dst: Path) -> bool:
    """Whether ``dst`` routes through the structural (comment-tree) engine."""
    return jsonc.is_jsonc_file(dst) or dst.suffix in {".yaml", ".yml"}


# ---------------------------------------------------------------------------
# Structural (YAML / JSONC) path.
# ---------------------------------------------------------------------------


def _resolve_structural(
    dst: Path,
    base: str,
    live: str,
    tracked: str,
    auto: ReconcileAuto | None,
) -> FileResolution:
    """Run the comment-preserving 3-way merge over ``dst``'s structural model.

    Each side is parsed FRESH (never aliased) because
    :func:`setforge.structural_merge.merge_structural` mutates ours in place.
    The merged model is ours mutated, bearing ours' value at every
    :class:`~setforge.structural_merge.PathConflict`.
    """
    is_jsonc = jsonc.is_jsonc_file(dst)
    base_model = _load_structural(base, is_jsonc)
    live_model = _load_structural(live, is_jsonc)
    tracked_model = _load_structural(tracked, is_jsonc)

    result = merge_structural(base_model, live_model, tracked_model)
    conflicts: list[LineConflict | PathConflict] = list(result.conflicts)

    if not result.conflicts:
        return FileResolution(
            text=_dump_structural(result.merged_model, is_jsonc),
            conflicts=[],
            advance_base=True,
            base_absent=False,
        )

    if auto is ReconcileAuto.USE_TRACKED:
        for pc in result.conflicts:
            set_at_path(result.merged_model, pc.path, pc.theirs)
        advance = True
    else:
        # KEEP_LIVE or bare: ours is already in the model; nothing to write.
        advance = auto is ReconcileAuto.KEEP_LIVE

    return FileResolution(
        text=_dump_structural(result.merged_model, is_jsonc),
        conflicts=conflicts,
        advance_base=advance,
        base_absent=False,
    )


def _load_structural(text: str, is_jsonc: bool) -> object:
    """Parse ``text`` into a fresh comment-preserving model.

    JSONC goes through json-five's :class:`~json5.loader.ModelLoader`
    (comments / formatting on ``.wsc_before`` / ``.wsc_after``); YAML through
    ruamel ``YAML(typ="rt")`` round-trip mode (comments / anchors / quotes
    preserved), matching :func:`setforge.deploy._render_with_preserve_keys`.
    """
    if is_jsonc:
        return _json5_loads(text, loader=ModelLoader())
    yaml = _rt_yaml()
    return yaml.load(io.StringIO(text))


def _dump_structural(model: object, is_jsonc: bool) -> str:
    """Serialize a merged structural ``model`` back to byte-faithful text.

    Mirrors the load idioms in :func:`_load_structural` /
    :func:`setforge.deploy._render_with_preserve_keys`: json-five's
    :class:`~json5.dumper.ModelDumper` and ruamel ``YAML(typ="rt")`` keep
    comments, anchors, quote styles and key order intact on round-trip.
    """
    if is_jsonc:
        return _json5_dumps(model, dumper=ModelDumper())
    yaml = _rt_yaml()
    buf = io.StringIO()
    yaml.dump(model, buf)
    return buf.getvalue()


def _rt_yaml() -> YAML:
    """Build a ruamel round-trip YAML configured for byte-faithful preserve.

    ``preserve_quotes`` keeps a scalar's original quote style across the
    round-trip, matching the project's preserve idiom.
    """
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    return yaml


# ---------------------------------------------------------------------------
# Line-based (markdown / arbitrary text) path.
# ---------------------------------------------------------------------------


def _resolve_line_based(
    base: str,
    live: str,
    tracked: str,
    auto: ReconcileAuto | None,
) -> FileResolution:
    """Run the line-based 3-way merge over the raw text triple.

    Conflicts resolve per ``auto``: ``USE_TRACKED`` chooses theirs, every
    other mode (``KEEP_LIVE`` and bare) chooses ours. Re-baselining advances
    on a clean merge or any set ``auto``; a bare conflict defers.
    """
    segments = merge_markdown_segments(base, live, tracked)
    conflicts: list[LineConflict | PathConflict] = [
        seg for seg in segments if isinstance(seg, LineConflict)
    ]
    _ours_lines, ours_terminator = _split_strip_final(live)

    def _take_theirs(conflict: LineConflict) -> list[str]:
        return conflict.theirs

    def _take_ours(conflict: LineConflict) -> list[str]:
        return conflict.ours

    choose: Callable[[LineConflict], list[str]] = (
        _take_theirs if auto is ReconcileAuto.USE_TRACKED else _take_ours
    )

    text = resolve_segments(segments, choose, ours_terminator)

    # Clean -> advance; conflict under a set auto -> advance; bare conflict -> defer.
    advance = True if not conflicts else auto is not None

    return FileResolution(
        text=text,
        conflicts=conflicts,
        advance_base=advance,
        base_absent=False,
    )
