"""Capture: live → tracked.

The inverse of ``deploy.copy_atomic``. Reads each profile tracked_file's
``dst`` (the live copy) and writes a host-state-stripped version back to
``src`` (the tracked copy):

- markerless host-local OVERLAY bodies (carried in local.yaml) are excised
  by their exact recorded bytes, so live host-local content never bakes
  into the shared tracked source.
- legacy ``host_local_sections`` marker pairs that ``install`` injected are
  name-scoped stripped (markers and body both removed).

Capture is no longer a silent absorb. When a tracked_file carries drift
between tracked and live, capture resolves it via
``--auto={use-live, keep-tracked}`` (``use-live`` absorbs the drift into
tracked, ``keep-tracked`` refuses it) or an interactive confirm; the
per-tracked_file writeback then applies the host-state strip above.

The ``CaptureRequiresInteractive`` exception is raised when capture
would prompt but stdin is not a TTY and ``--auto`` wasn't supplied —
the CLI layer renders this as a non-zero exit with a clear migration
hint.
"""

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console

from setforge import (
    base_store,
    disposition_merge,
    jsonc,
    overlay_deploy,
    sections,
    spans_overlay,
    spans_store,
)
from setforge.compare import expand_tracked_file, resolve_dst, resolve_src
from setforge.config import Config, Disposition, resolve_profile
from setforge.errors import CaptureRequiresInteractive, OverlayBodyUnlocatable
from setforge.source import HostLocalSection, HostLocalSectionName
from setforge.spans import SpanEntry, SpanKind

if TYPE_CHECKING:
    from setforge.overlay_body_wizard import OverlayBodyEdit, OverlayEditChoice


class CaptureAction(StrEnum):
    UPDATED = "updated"
    NOOP = "noop"
    SKIPPED = "skipped"


class CaptureAuto(StrEnum):
    """Closed set of non-interactive resolutions for capture-time drift.

    ``USE_LIVE`` — absorb all drift (reproduces pre-`capture-wizard` silent-absorb).
    ``KEEP_TRACKED`` — refuse to absorb any drift.

    ``None`` is the third valid value the CLI seam accepts (interactive mode);
    it sits outside the enum because ``StrEnum`` members must be strings.
    """

    USE_LIVE = "use-live"
    KEEP_TRACKED = "keep-tracked"


@dataclass(frozen=True, slots=True)
class CaptureResult:
    name: str
    action: CaptureAction
    reason: str = ""


def capture_tracked_file(
    src: Path,
    dst: Path,
    *,
    host_local_section_names: frozenset[str] = frozenset(),
    spans: list[SpanEntry] | None = None,
    span_states: dict[str, "spans_store.SpanState"] | None = None,
    sub_name: str = "",
    tracked_file_id: str = "",
    auto: "CaptureAuto | None" = None,
    interactive: bool = False,
    local_config_path: Path | None = None,
) -> CaptureResult:
    """Write ``dst`` (live) back to ``src`` (tracked) for a disposition=None file.

    A ``disposition=None`` tracked_file deploys tracked verbatim, so capture is
    a wholesale live → tracked writeback — EXCEPT host-local content, which must
    never leak into the shared tracked source:

    - markerless host-local OVERLAY bodies (carried in local.yaml) are excised
      by their exact recorded bytes via :func:`_capture_overlay_bodies` (a
      hand-edited body routes to the keep/discard wizard);
    - legacy ``host_local_sections`` marker pairs injected by ``install`` are
      name-scoped stripped via :func:`sections.strip_host_local_sections`.

    Returns :class:`CaptureResult.NOOP` when the resulting tracked content is
    byte-identical to the existing tracked file, or SKIPPED when live is absent.
    """
    if not dst.exists():
        return CaptureResult(
            name=src.name, action=CaptureAction.SKIPPED, reason="live missing"
        )

    content = dst.read_text(encoding="utf-8")
    # MARKERLESS host-local OVERLAY bodies own their excise: install injects
    # them without markers, so the name-scoped marker strip below cannot see
    # them — they would round-trip into the shared tracked source (a host-state
    # leak). Runs only when overlay spans exist, so files without host-local
    # overlays are byte-for-byte the live content.
    md_overlay = overlay_deploy.overlay_spans(spans) if spans else []
    if md_overlay:
        content = _capture_overlay_bodies(
            content,
            md_overlay,
            span_states or {},
            sub_name=sub_name,
            tracked_file_id=tracked_file_id,
            auto=auto,
            interactive=interactive,
            local_config_path=local_config_path,
        )
    # Drop legacy host-local marker pairs + bodies injected by install (via
    # local.yaml host_local_sections) before the writeback. Name-scoped to
    # ``host_local_section_names`` so a host-local marker the user authored
    # directly in tracked passes through unchanged.
    if host_local_section_names:
        content = sections.strip_host_local_sections(
            content, names=host_local_section_names, allow_legacy=True
        )
    return _write_if_changed(src, content)


def _write_if_changed(src: Path, content: str) -> CaptureResult:
    """Write ``content`` to ``src`` unless it already matches; return action."""
    src.parent.mkdir(parents=True, exist_ok=True)
    if src.exists() and src.read_text(encoding="utf-8") == content:
        return CaptureResult(name=src.name, action=CaptureAction.NOOP)
    src.write_text(content, encoding="utf-8")
    return CaptureResult(name=src.name, action=CaptureAction.UPDATED)


def capture_profile(
    config: Config,
    profile_name: str,
    repo_root: Path,
    *,
    setforge_yaml_path: Path,
    interactive: bool | None = None,
    auto: CaptureAuto | None = None,
    snapshot_base: Path | None = None,
    console: Console | None = None,
    host_local_sections_map: (
        Mapping[str, dict[HostLocalSectionName, HostLocalSection]] | None
    ) = None,
    local_config_path: Path | None = None,
) -> list[CaptureResult]:
    """Capture every tracked_file in the resolved profile from live → tracked.

    Orchestrates the capture-time wizard (fires when there is drift the
    walker yields) and the per-tracked_file writeback that runs against
    post-wizard tracked.

    Parameters
    ----------
    config:
        Loaded :class:`setforge.config.Config`.
    profile_name:
        Profile to capture.
    repo_root:
        Repo root used for ``resolve_src``.
    setforge_yaml_path:
        Path to ``setforge.yaml`` — needed by the wizard's ``[s]``
        action.
    interactive:
        Force-toggle for whether the wizard prompts. ``None`` (default)
        auto-detects via ``sys.stdin.isatty()``. ``False`` requires
        ``auto`` to be set when drift exists.
    auto:
        Non-interactive resolution: ``"use-live"`` absorbs all drift
        (reproduces today's silent-absorb behavior),
        ``"keep-tracked"`` rejects all drift, ``None`` enables
        interactive prompts.
    snapshot_base:
        Override for the wizard's snapshot directory; defaults to
        ``~/.local/state/setforge/sync-snapshots``.
    console:
        Rich Console for the wizard (defaults to a fresh
        ``Console()``).

    Raises
    ------
    CaptureRequiresInteractive
        When drift would prompt but stdin is not a TTY and ``auto`` is
        unset.
    KeyboardInterrupt
        Propagated from the wizard when the user cancels mid-prompt;
        the CLI layer renders the cancellation and exits 130.
    """
    if interactive is None:
        interactive = sys.stdin.isatty()
    if local_config_path is None:
        from setforge.source import LOCAL_CONFIG_PATH

        local_config_path = LOCAL_CONFIG_PATH

    # The disposition path runs its own per-conflict capture handling
    # (_capture_disposition_file); disposition=None files capture live verbatim
    # minus host-local overlays. Per-tracked_file writeback below.
    overlay = host_local_sections_map or {}
    results: list[CaptureResult] = []
    resolved = resolve_profile(config, profile_name)
    for name in resolved.tracked_files:
        tracked_file = config.tracked_files[name]
        src = resolve_src(tracked_file, repo_root)
        dst = resolve_dst(tracked_file)
        # capture-back filter: names of host-local sections
        # injected by `install` (from local.yaml). The capture path
        # removes only these names from live-side text before merging
        # tracked sections; any host-local marker pair the user authored
        # directly in tracked carries through unchanged.
        host_local_names = frozenset(overlay.get(name, {}))
        for sub_name, sub_src, sub_dst in expand_tracked_file(name, src, dst):
            if tracked_file.disposition is not None:
                result = _capture_disposition_file(
                    sub_name,
                    sub_src,
                    sub_dst,
                    disposition=tracked_file.disposition,
                    profile=profile_name,
                    spans=tracked_file.spans,
                    tracked_file_id=name,
                    auto=auto,
                    interactive=interactive,
                    local_config_path=local_config_path,
                )
            else:
                # No-disposition preserve files can still carry host-local
                # OVERLAY spans (markerless deploy). Load the sidecar so
                # capture can excise each markerless body by its exact recorded
                # bytes before the tracked writeback — symmetric to the deploy
                # inject and to the disposition path's overlay excise.
                span_states = (
                    spans_store.get_states(profile_name, sub_name)
                    if tracked_file.spans
                    else {}
                )
                result = capture_tracked_file(
                    sub_src,
                    sub_dst,
                    host_local_section_names=host_local_names,
                    spans=tracked_file.spans,
                    span_states=span_states,
                    sub_name=sub_name,
                    tracked_file_id=name,
                    auto=auto,
                    interactive=interactive,
                    local_config_path=local_config_path,
                )
            results.append(
                CaptureResult(name=sub_name, action=result.action, reason=result.reason)
            )
    return results


def _capture_overlay_bodies(
    live_text: str,
    md_overlay: list[SpanEntry],
    span_states: dict[str, "spans_store.SpanState"],
    *,
    sub_name: str,
    tracked_file_id: str,
    auto: "CaptureAuto | None",
    interactive: bool,
    local_config_path: Path | None,
) -> str:
    """Return ``live_text`` with every overlay body excised (always body-free).

    Per overlay span: try the exact-bytes excise first. On a miss, detect a
    hand-edit near the anchor. A located edit routes to the keep/discard
    decision (``--auto`` map, else interactive prompt, else
    :class:`~setforge.errors.CaptureRequiresInteractive`); KEEP writes the
    edit into ``local.yaml`` (never tracked) before excising the located
    body, DISCARD just excises it (canonical re-imposed next deploy). The
    tracked write is body-free either way.

    Fail-closed on an unlocatable miss: when the exact-bytes excise misses
    AND no edit can be located near the anchor, the sidecar disambiguates.
    If a body WAS deployed at this anchor (``stored.last_deployed_body`` is
    non-empty) the hand-edited body cannot be proven excised — capturing
    would leak it into tracked, so we raise
    :class:`~setforge.errors.OverlayBodyUnlocatable` BEFORE any tracked
    write. Only a genuine no-deploy record (``stored is None`` or no stored
    body) is skipped as the clean first-deploy / absent case.
    """
    from setforge import overlay_body_wizard

    text = live_text
    for span in md_overlay:
        stored = span_states.get(span.anchor)
        excised, found = overlay_deploy.excise_overlay_bodies(text, [span], span_states)
        if found:
            text = excised
            continue
        edit = overlay_body_wizard.detect_overlay_body_edit(
            text, span, stored, tracked_file_id=tracked_file_id
        )
        if edit is None:
            # No exact match AND no locatable edit. Disambiguate via the
            # sidecar: if a body WAS deployed here, the (hand-edited)
            # host-local body is somewhere in `text` but unlocatable — we
            # CANNOT prove the tracked write is body-free, so FAIL CLOSED
            # before any tracked write rather than leak it. Only a genuine
            # no-deploy record (first deploy / absent) is safe to skip.
            if stored is not None and stored.last_deployed_body:
                raise OverlayBodyUnlocatable(sub_name=sub_name, anchor=span.anchor)
            continue
        choice = overlay_body_wizard.require_interactive_or_auto(
            auto, interactive, edit_count=1
        )
        if choice is None:
            choice = _prompt_overlay_edit(edit)
        if choice is overlay_body_wizard.OverlayEditChoice.SKIP:
            # Skip: leave the edited body in live AND in the tracked write?
            # No — the tracked write must stay body-free regardless, so we
            # still excise the located region; "skip" only defers the
            # local.yaml writeback decision to a later sync.
            text = overlay_body_wizard.excise_located_body(text, span.anchor, stored)
            continue
        if choice is overlay_body_wizard.OverlayEditChoice.KEEP:
            if local_config_path is None:
                raise CaptureRequiresInteractive(
                    "cannot keep a hand-edited overlay body without a "
                    "local.yaml path to write it into"
                )
            overlay_body_wizard.write_edited_body_to_local(
                edit, local_config_path=local_config_path
            )
        # KEEP and DISCARD both excise the located body from the tracked
        # write; KEEP additionally persisted the edit to local.yaml above.
        text = overlay_body_wizard.excise_located_body(text, span.anchor, stored)
    return text


def _prompt_overlay_edit(
    edit: "OverlayBodyEdit",
) -> "OverlayEditChoice":
    """Render the diff + read one keep/discard/skip choice for a hand-edited body."""
    import difflib

    from rich.syntax import Syntax

    from setforge import overlay_body_wizard
    from setforge.wizard import read_one_choice

    console = Console()
    diff = "".join(
        difflib.unified_diff(
            edit.canonical_body.splitlines(keepends=True),
            edit.live_body.splitlines(keepends=True),
            fromfile=f"local.yaml/{edit.tracked_file_id}{edit.anchor}",
            tofile=f"live{edit.anchor}",
        )
    )
    console.print(
        f"host-local overlay body hand-edited at {edit.anchor!r} "
        f"({edit.tracked_file_id}):"
    )
    if diff:
        console.print(Syntax(diff, "diff"))
    choice = read_one_choice(
        "   [k]eep edit (write to local.yaml) / [d]iscard / [s]kip: ",
        {"k", "d", "s"},
    )
    return {
        "k": overlay_body_wizard.OverlayEditChoice.KEEP,
        "d": overlay_body_wizard.OverlayEditChoice.DISCARD,
        "s": overlay_body_wizard.OverlayEditChoice.SKIP,
    }[choice]


def _capture_disposition_file(
    sub_name: str,
    src: Path,
    dst: Path,
    *,
    disposition: Disposition,
    profile: str,
    spans: list[SpanEntry] | None = None,
    tracked_file_id: str = "",
    auto: "CaptureAuto | None" = None,
    interactive: bool = False,
    local_config_path: Path | None = None,
) -> CaptureResult:
    """Capture one disposition-bearing tracked (sub-)file under the 3-way model.

    ``PINNED`` / ``FORKED`` never capture: tracked content stays as-is and
    the stored base is left untouched (a :class:`CaptureAction.SKIPPED`
    result carries the disposition as the skip reason).

    ``SHARED`` writes the live file's bytes to ``src`` and — ONLY after the
    tracked write returns cleanly — re-baselines the stored base to the
    same bytes via :func:`setforge.base_store.write_base`, converging
    ``base == tracked == live``. A base-write failure propagates; base
    lagging live is the safe failure direction.

    When ``spans`` is non-empty the SHARED writeback is NOT a verbatim
    live copy: every span region (BOTH pinned AND forked) is excluded
    (Invariant I2) — the existing TRACKED bytes are kept for those regions
    while the rest of the live file captures normally. This blocks a
    host-local span body from baking into the shared config repo, and
    governs the ``sync --auto=use-live`` drift-absorption path too (the
    same verbatim writeback this function performs). The re-baselined base
    is the SAME span-excluded bytes so the next merge has a consistent
    ancestor.

    ``sub_name`` is the ``expand_tracked_file`` synthetic id (the same
    stable per-profile ``file_id`` the install loop keys the base by), so
    install and sync share one base per file.
    """
    if disposition in (Disposition.PINNED, Disposition.FORKED):
        return CaptureResult(
            name=src.name,
            action=CaptureAction.SKIPPED,
            reason=f"disposition={disposition.value}",
        )
    # SHARED: capture live → tracked, then re-baseline the base.
    if not dst.exists():
        return CaptureResult(
            name=src.name, action=CaptureAction.SKIPPED, reason="live missing"
        )
    live_text = dst.read_text(encoding="utf-8")
    capture_text = live_text
    if spans:
        # Capture exclusion is TOTAL: keep tracked over live inside every
        # span region (Invariant I2). The existing tracked content is the
        # source of the kept-region bytes. Structural (yaml/json/jsonc) spans
        # restore tracked's VALUE at each dotted path; markdown spans splice
        # tracked's heading region — dispatch by file type so each flavor
        # takes its own exclusion path (B-S5).
        tracked_text = src.read_text(encoding="utf-8") if src.exists() else ""
        if disposition_merge.is_structural(dst):
            capture_text = disposition_merge.exclude_structural_spans_for_capture(
                live_text, tracked_text, spans, jsonc.is_jsonc_file(dst)
            )
        else:
            span_states = spans_store.get_states(profile, sub_name)
            # OVERLAY (markerless host-local body) spans OWN their excise:
            # strip the body by its exact recorded bytes BEFORE any tracked
            # write, never via the leaky exclude_spans_for_capture
            # tracked_loc-is-None pass-through. A body that was hand-edited
            # (no exact needle hit) routes to the overlay-body wizard
            # upstream; an ambiguous (>1) occurrence REFUSES the whole file.
            md_overlay = overlay_deploy.overlay_spans(spans)
            if md_overlay:
                capture_text = _capture_overlay_bodies(
                    capture_text,
                    md_overlay,
                    span_states,
                    sub_name=sub_name,
                    tracked_file_id=tracked_file_id,
                    auto=auto,
                    interactive=interactive,
                    local_config_path=local_config_path,
                )
            pinned_forked = [s for s in spans if s.kind is not SpanKind.OVERLAY]
            if pinned_forked:
                capture_text = spans_overlay.exclude_spans_for_capture(
                    capture_text, tracked_text, pinned_forked, span_states
                )
    result = _write_if_changed(src, capture_text)
    # Re-baseline AFTER the tracked write succeeded: write tracked first,
    # then base, so a base-write failure leaves base lagging (safe) rather
    # than ahead of tracked (corruption). Failure propagates.
    base_store.write_base(profile, sub_name, capture_text.encode("utf-8"))
    return result
