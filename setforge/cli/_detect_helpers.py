"""Orchestration for ``setforge section detect`` (S4/S5).

The pure detect engine lives in :mod:`setforge.section_detect`
(``compute_detect_regions`` / ``propose_anchor``); this module does the
config/IO/wizard plumbing the typer command needs:

* resolve each markdown tracked_file's tracked-``src`` + live-``dst`` under a
  profile,
* compute the **live-independent** expected-deploy string
  (:func:`expected_deploy_text`) so hand-edited regions surface as drift,
* run the carve wizard and write host-local spans to ``local.yaml`` atomically.

Kept separate from :mod:`setforge.cli.section` (already large) per the plan's
file-placement decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from setforge import deploy, spans_store
from setforge.cli._install_helpers import (
    _load_validated_host_local_sections,
    _plan_disposition_base,
)
from setforge.compare import resolve_dst, resolve_src
from setforge.config import Config, TrackedFile, load_config, resolve_profile
from setforge.host_local_inject import _normalise_eol
from setforge.markdown_spans import _scan_headings
from setforge.section_detect import DetectRegion, RegionKind, compute_detect_regions
from setforge.source import HostLocalSection, HostLocalSectionName

_MARKDOWN_SUFFIXES: frozenset[str] = frozenset({".md", ".markdown"})


@dataclass(slots=True, frozen=True)
class DetectTarget:
    """One markdown tracked_file resolved for a detect run."""

    name: str
    tracked_file: TrackedFile
    src: Path
    dst: Path


def _markdown_targets(
    cfg: Config, profile: str, repo_root: Path, tracked_file: str | None
) -> list[DetectTarget]:
    """Resolve the markdown detect targets for ``profile``.

    ``tracked_file`` (a tracked_files key) narrows to one entry; ``None`` walks
    every markdown tracked_file in the resolved profile. Non-markdown entries
    are skipped silently (detect is markdown-only).
    """
    resolved = resolve_profile(cfg, profile)
    names = [tracked_file] if tracked_file else list(resolved.tracked_files)
    out: list[DetectTarget] = []
    for name in names:
        if name not in resolved.tracked_files:
            raise KeyError(name)
        tf = cfg.tracked_files[name]
        src = resolve_src(tf, repo_root)
        dst = resolve_dst(tf)
        if src.suffix.lower() not in _MARKDOWN_SUFFIXES:
            continue
        out.append(DetectTarget(name=name, tracked_file=tf, src=src, dst=dst))
    return out


def expected_deploy_text(
    profile: str,
    target: DetectTarget,
    host_local: dict[HostLocalSectionName, HostLocalSection] | None,
) -> str:
    """Return the **live-independent** expected deploy output for ``target``.

    ``live_text=""`` is the load-bearing choice (plan P1): it stops the
    disposition 3-way merge from absorbing the user's live hand-edits, so
    :func:`compute_detect_regions` surfaces them as drift rather than silently
    folding them into ``expected``. For the ``disposition=None`` markerless path
    the content is the tracked source with its overlay bodies injected — already
    live-independent. ``host_local`` is the per-file overlay map (loaded once by
    the caller, mirroring install/compare); ``None`` when the file declares no
    host-local section.
    """
    tf = target.tracked_file
    file_spans = tf.spans or []
    states = spans_store.get_states(profile, target.name) if file_spans else {}
    base_text: str | None = None
    if tf.disposition is not None:
        base_text = _plan_disposition_base(profile, target.name, target.dst).base_text
    resolved = deploy.resolve_deploy(
        target.src,
        target.dst,
        host_local_sections=host_local,
        mode=tf.mode,
        disposition=tf.disposition,
        base_text=base_text,
        spans=file_spans or None,
        span_states=states or None,
        live_text="",
    )
    return resolved.content


def allowed_kinds(region: DetectRegion, target: DetectTarget) -> list[str]:
    """KINDs the wizard may offer for ``region`` on ``target`` (plan P3).

    NEW_CONTENT → ``overlay`` only (a pinned/forked anchor would orphan — the
    content is absent from tracked). DIVERGENCE → ``pinned``/``forked``, but
    ONLY when the tracked_file declares a file-level ``disposition``: a
    pinned/forked span is consumed on the disposition merge path, and
    :func:`setforge.spans.validate_span_disposition` rejects one on a
    ``disposition=None`` file. A divergence on such a file yields ``[]`` (the
    wizard refuses that range with a reason).
    """
    if region.kind is RegionKind.NEW_CONTENT:
        return ["overlay"]
    if target.tracked_file.disposition is None:
        return []
    return ["pinned", "forked"]


def _enclosing_heading(live_n: str, live_start: int) -> tuple[int, str] | None:
    """Return ``(level, text)`` of the immediately-enclosing ATX heading.

    Mirrors :func:`setforge.section_detect.propose_anchor`'s scan: the nearest
    preceding fence-aware heading of any level. ``live_n`` MUST be EOL-normalised
    (``live_start`` indexes its ``splitlines`` like the detect engine's regions).
    """
    enclosing: tuple[int, str] | None = None
    for line_idx, level, htext in _scan_headings(live_n):
        if line_idx <= live_start:
            enclosing = (level, htext)
    return enclosing


def pinned_anchor_string(region: DetectRegion, live: str) -> str:
    """Rebuild the markdown heading anchor (``'#'*level + ' ' + text``) for a
    pinned/forked carve (plan P4).

    ``propose_anchor`` returns the heading TEXT only; pinned/forked span anchors
    are the full markdown heading string (the ``#`` run encodes the level), so
    re-derive the level from the enclosing heading. Raises :class:`ValueError`
    when the region has no enclosing heading (``propose_anchor`` would already
    have refused such a divergence).
    """
    enclosing = _enclosing_heading(_normalise_eol(live), region.live_start)
    if enclosing is None:
        raise ValueError("region has no enclosing heading to anchor a pinned span")
    level, htext = enclosing
    return "#" * level + " " + htext


def run_detect(*, config_path: Path, profile: str, tracked_file: str | None) -> None:
    """Top-level ``section detect`` entry point (skeleton — wizard lands in Task 5)."""
    cfg = load_config(config_path)
    repo_root = config_path.resolve().parent
    resolved = resolve_profile(cfg, profile)
    overlay_map = _load_validated_host_local_sections(cfg, resolved, repo_root)
    console = Console()
    targets = _markdown_targets(cfg, profile, repo_root, tracked_file)
    any_drift = False
    for target in targets:
        if not target.dst.exists():
            continue
        live = target.dst.read_text(encoding="utf-8")
        expected = expected_deploy_text(profile, target, overlay_map.get(target.name))
        regions = compute_detect_regions(live, expected)
        if not regions:
            continue
        any_drift = True
        console.print(
            f"{len(regions)} changed region(s) in {target.dst} "
            "(live vs expected deploy output)"
        )
    if not any_drift:
        console.print("no changes detected — live matches expected deploy output")
