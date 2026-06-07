"""Overlay-body edit wizard: handle a hand-edited deployed host-local body.

A markerless OVERLAY body is ``local.yaml``-authoritative — re-imposed
every deploy. But a user may hand-edit the deployed body in the live file
between installs. The exact-bytes excise then misses (the live body no
longer matches the recorded needle), yet the body is still locatable near
its anchor. Rather than a dead-end refuse, this wizard offers:

* ``[k]`` keep — write the edited body into ``local.yaml``'s ``spans``
  body so the next deploy re-imposes the EDIT (``local.yaml`` only, NEVER
  tracked — the tracked / base writes stay body-free).
* ``[d]`` discard — leave ``local.yaml`` as-is; the next deploy re-imposes
  the canonical body, overwriting the edit.
* ``[s]`` skip — keep live untouched, ask again next sync.

Non-interactive: ``--auto=use-live`` maps to keep, ``--auto=keep-tracked``
to discard; with no ``--auto`` and no TTY,
:class:`~setforge.errors.CaptureRequiresInteractive` is raised.

The detection (:func:`detect_overlay_body_edit`) uses the relocation ladder
to find the edited region near the anchor; it returns ``None`` when the
body is unchanged (the exact excise handles that) or unlocatable. The
caller (:func:`setforge.capture._capture_overlay_bodies`) disambiguates the
unlocatable ``None`` via the sidecar: a body that WAS deployed but cannot be
located fails closed (raises :class:`~setforge.errors.OverlayBodyUnlocatable`)
rather than leak; a no-deploy record is the clean first-deploy / absent case.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from setforge.errors import (
    AnchorAmbiguousError,
    AnchorNotFoundError,
    CaptureRequiresInteractive,
)
from setforge.markdown_spans import bound_span
from setforge.migrations._yaml_ops import atomic_write_yaml, yaml_rt
from setforge.overlay_deploy import canonical_overlay_body
from setforge.overlay_inject import canonical_body
from setforge.spans import SpanEntry
from setforge.spans_relocation import _fuzzy_relocate
from setforge.spans_store import SpanState

__all__ = [
    "OverlayBodyEdit",
    "OverlayEditChoice",
    "detect_overlay_body_edit",
    "resolve_auto",
    "write_edited_body_to_local",
]


class OverlayEditChoice(StrEnum):
    """Closed set of resolutions for a hand-edited overlay body."""

    KEEP = "keep"
    DISCARD = "discard"
    SKIP = "skip"


@dataclass(slots=True, frozen=True)
class OverlayBodyEdit:
    """A detected hand-edit of a deployed overlay body.

    ``tracked_file_id`` + ``anchor`` identify the overlay span;
    ``canonical_body`` is the ``local.yaml``-authoritative body (what deploy
    would re-impose); ``live_body`` is the user's edited body found in the
    live file. The keep path writes ``live_body`` into ``local.yaml``.
    """

    tracked_file_id: str
    anchor: str
    canonical_body: str
    live_body: str


def detect_overlay_body_edit(
    live_text: str,
    span: SpanEntry,
    stored: SpanState | None,
    *,
    tracked_file_id: str = "",
) -> OverlayBodyEdit | None:
    """Detect a hand-edited overlay body in ``live_text``, or return ``None``.

    Returns ``None`` when the canonical body is present verbatim (no edit —
    the exact excise handles it) or when no candidate region can be located
    near the anchor (unlocatable — the caller fails closed with
    :class:`~setforge.errors.OverlayBodyUnlocatable` when a body was deployed
    here). Otherwise returns the :class:`OverlayBodyEdit` describing the edit.

    Location strategy: bound the span at its heading-identity anchor; the
    region BELOW the heading (the injected body) is the candidate. When a
    stored state exists, the fuzzy relocation ladder refines the heading
    line first so a moved heading still resolves.
    """
    assert span.overlay is not None
    canonical = canonical_overlay_body(span.overlay)
    if canonical in live_text:
        return None  # unchanged — exact excise owns this case

    located = _locate_body_region(live_text, span.anchor, stored)
    if located is None:
        return None
    live_body = canonical_body(located)
    if live_body == canonical:
        return None
    return OverlayBodyEdit(
        tracked_file_id=tracked_file_id,
        anchor=span.anchor,
        canonical_body=canonical,
        live_body=live_body,
    )


def _locate_body_region(
    live_text: str, anchor: str, stored: SpanState | None
) -> str | None:
    """Return the body text below ``anchor``'s heading in ``live_text``, or None."""
    region = _locate_body_region_bounds(live_text, anchor, stored)
    if region is None:
        return None
    start_line, end_line = region
    keep = live_text.splitlines(keepends=True)
    body = "".join(keep[start_line:end_line])
    return body or None


def excise_located_body(live_text: str, anchor: str, stored: SpanState | None) -> str:
    """Return ``live_text`` with the located (edited) overlay body removed.

    Used by capture to keep the tracked write body-free even when the body
    was hand-edited (so the exact-bytes needle missed). Bounds the body
    region below the anchor heading and splices it out; a no-location is a
    no-op (the caller's :class:`~setforge.errors.OverlayBodyUnlocatable`
    fail-closed gate covers the truly-unlocatable deployed-body case before
    this is reached).
    """
    region = _locate_body_region_bounds(live_text, anchor, stored)
    if region is None:
        return live_text
    start_line, end_line = region
    keep = live_text.splitlines(keepends=True)
    return "".join(keep[:start_line] + keep[end_line:])


def _locate_body_region_bounds(
    live_text: str, anchor: str, stored: SpanState | None
) -> tuple[int, int] | None:
    """Return the ``[start_line, end_line)`` of the edited body below ``anchor``.

    The overlay body was injected as the ``position_hint_n_lines`` lines
    immediately AFTER the recorded body's start line. That recorded line
    span (NOT the heading section's full extent to EOF) is the body region,
    so excising it leaves the surrounding shared content intact.

    Without a stored state the body's extent is unknown — a markerless body
    has no self-delimiting boundary, so we return ``None`` (unlocatable; the
    caller refuses) rather than over-excise the whole heading section.
    """
    if stored is None:
        return None
    n_lines = stored.position_hint_n_lines
    if n_lines <= 0:
        return None
    # Refine the body's START line. The recorded body starts at
    # ``position_hint_start_line``; the fuzzy ladder re-finds the anchor
    # heading if it moved, and the body is the line after the heading.
    fuzzy = _fuzzy_relocate(live_text, stored)
    if fuzzy is not None:
        start = fuzzy.start_line + 1
    else:
        try:
            span_loc = bound_span(live_text, anchor)
        except (AnchorNotFoundError, AnchorAmbiguousError):
            start = stored.position_hint_start_line
        else:
            start = span_loc.start_line + 1
    total = len(live_text.splitlines())
    end = min(start + n_lines, total)
    if end <= start:
        return None
    return start, end


def resolve_auto(auto: object) -> OverlayEditChoice | None:
    """Map a ``CaptureAuto`` value to a wizard choice (or ``None`` for interactive).

    ``use-live`` keeps the edited body (write to local.yaml); ``keep-tracked``
    discards it (re-impose canonical). ``None`` means run the interactive
    prompt.
    """
    from setforge.capture import CaptureAuto

    if auto is CaptureAuto.USE_LIVE:
        return OverlayEditChoice.KEEP
    if auto is CaptureAuto.KEEP_TRACKED:
        return OverlayEditChoice.DISCARD
    return None


def require_interactive_or_auto(
    auto: object, interactive: bool, edit_count: int
) -> OverlayEditChoice | None:
    """Return the auto-mapped choice, or raise when a prompt is needed but blocked."""
    choice = resolve_auto(auto)
    if choice is not None:
        return choice
    if not interactive:
        raise CaptureRequiresInteractive(
            f"a hand-edited host-local overlay body needs a decision for "
            f"{edit_count} span(s); run interactively or pass "
            "--auto=use-live / --auto=keep-tracked."
        )
    return None


def write_edited_body_to_local(
    edit: OverlayBodyEdit,
    *,
    local_config_path: Path,
) -> None:
    """Write ``edit.live_body`` into ``local.yaml``'s overlay span body.

    Mutates ONLY ``local.yaml`` (the keep path's contract — the body never
    reaches tracked or the base). Locates
    ``tracked_files.<id>.spans[*].overlay.body`` whose span ``anchor``
    matches ``edit.anchor`` and replaces its body, then atomically rewrites
    the file via :func:`~setforge.migrations._yaml_ops.atomic_write_yaml`
    (ruamel round-trip — comments / order / mode preserved).

    Raises :class:`KeyError` when the tracked_file id or matching span is
    absent from ``local.yaml`` (a structural contradiction the caller surfaces).
    """
    yaml = yaml_rt()
    with local_config_path.open("r", encoding="utf-8") as fh:
        doc = yaml.load(fh)

    tracked_files = doc.get("tracked_files") if isinstance(doc, dict) else None
    if not isinstance(tracked_files, dict) or edit.tracked_file_id not in tracked_files:
        raise KeyError(
            f"local.yaml has no tracked_file {edit.tracked_file_id!r} to "
            "write the edited overlay body into"
        )
    spans = tracked_files[edit.tracked_file_id].get("spans")
    target = None
    if isinstance(spans, list):
        for span_node in spans:
            if isinstance(span_node, dict) and span_node.get("anchor") == edit.anchor:
                target = span_node
                break
    if target is None or "overlay" not in target:
        raise KeyError(
            f"local.yaml tracked_file {edit.tracked_file_id!r} has no overlay "
            f"span with anchor {edit.anchor!r} to update"
        )
    target["overlay"]["body"] = edit.live_body
    # The body_file form (if present) is superseded by an inline edit.
    if "body_file" in target["overlay"]:
        del target["overlay"]["body_file"]
    atomic_write_yaml(local_config_path, doc)
