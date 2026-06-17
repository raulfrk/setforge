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

import dataclasses
import hashlib
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from setforge import deploy, spans_store, transitions
from setforge.anchors import Anchor
from setforge.cli import override
from setforge.cli._install_helpers import _load_validated_host_local_sections
from setforge.compare import resolve_dst, resolve_src
from setforge.config import (
    Config,
    TrackedFile,
    apply_host_local_tracked_file_overrides,
    load_config,
    resolve_profile,
)
from setforge.host_local_inject import _normalise_eol
from setforge.markdown_spans import _scan_headings
from setforge.overlay_deploy import _state_from_injection, overlay_spans
from setforge.overlay_inject import (
    OverlayAmbiguousError,
    canonical_body,
    excise_unique_needle,
)
from setforge.section_detect import (
    AnchorRefusal,
    DetectRegion,
    RegionKind,
    compute_detect_regions,
    propose_anchor,
)
from setforge.source import HostLocalSection, HostLocalSectionName
from setforge.spans import OverlaySpanPayload, SpanEntry, SpanKind, SpanSemantics
from setforge.spans_store import SpanState
from setforge.wizard import Snapshot

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
    """Return the **pristine** expected output for ``target`` (plan P1).

    The pristine baseline is the tracked source rendered with its host-local
    OVERLAY bodies injected and any shared markers maintained — the
    ``disposition=None`` verbatim+overlay path, FORCED regardless of the file's
    real disposition. This is live-INDEPENDENT, so the diff in
    :func:`compute_detect_regions` surfaces EVERY live edit that diverges from
    the shared template, not just the ones a disposition merge would clobber
    (a shared/forked merge keeps the user's edit, which would otherwise hide it).
    Already-carved pinned/forked regions are filtered separately by
    :func:`covered_by_span` so they do not re-surface (idempotency); already-
    carved overlays match here because their body is injected into the baseline.
    ``host_local`` is the per-file overlay map (loaded once by the caller,
    mirroring install/compare); ``None`` when the file declares no host-local
    section.
    """
    tf = target.tracked_file
    file_spans = tf.spans or []
    states = spans_store.get_states(profile, target.name) if file_spans else {}
    resolved = deploy.resolve_deploy(
        target.src,
        target.dst,
        host_local_sections=host_local,
        mode=tf.mode,
        disposition=None,  # forced: pristine tracked render, not the live merge
        spans=file_spans or None,
        span_states=states or None,
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


@dataclass(slots=True)
class CarvePlan:
    """One carve the wizard resolved, awaiting the atomic commit.

    ``anchor`` is dual-typed (plan P4): a structured :class:`Anchor` for an
    overlay (the splice point in the live file) or the markdown heading string
    (``'## X'``) for a pinned/forked span. ``body`` is the raw live bytes for an
    overlay (canonicalised in :func:`build_span`), ``None`` for pinned/forked.
    ``seed_state`` is the overlay's pre-seeded :class:`SpanState` (carrying
    ``last_deployed_body``); ``None`` for pinned/forked.
    """

    kind: str  # "overlay" | "pinned" | "forked"
    name: str
    anchor: Anchor | str
    body: str | None
    semantics: str
    seed_state: SpanState | None = None


def build_span(plan: CarvePlan) -> SpanEntry:
    """Build the :class:`SpanEntry` for a carve.

    OVERLAY: identity ``anchor`` is the section NAME; the structured splice
    anchor + canonicalised body ride the nested ``overlay`` payload (the
    dual-anchor model). PINNED/FORKED: ``anchor`` is the markdown heading
    string; no payload.
    """
    if plan.kind == "overlay":
        assert plan.body is not None, "overlay carve requires a body"
        return SpanEntry(
            anchor=plan.name,
            kind=SpanKind.OVERLAY,
            semantics=SpanSemantics(plan.semantics),
            overlay=OverlaySpanPayload(
                anchor=plan.anchor,  # type: ignore[arg-type]
                body=canonical_body(plan.body),
            ),
        )
    return SpanEntry(
        anchor=plan.anchor,  # type: ignore[arg-type]
        kind=SpanKind.PINNED if plan.kind == "pinned" else SpanKind.FORKED,
        semantics=SpanSemantics(plan.semantics),
    )


def seed_overlay_state(name: str, live: str, region: DetectRegion) -> SpanState:
    """Seed an overlay's :class:`SpanState` from the live region at carve time.

    Reuses the deploy-side :func:`_state_from_injection` so the seeded
    ``last_deployed_body`` (the canonical bytes) is the exact excise needle the
    first install / capture will look for — closing the data-loss round-trip gap
    (plan P5, the seed pitfall).
    """
    body = canonical_body(region.live_text)
    return _state_from_injection(
        name, _normalise_eol(live), region.live_start, region.live_end, body
    )


def _default_snapshot_base() -> Path:
    """The carve-wizard snapshot directory (mirrors ``sync``)."""
    return transitions.state_root() / "snapshots"


def commit_carves(
    profile: str,
    file_id: str,
    plans: list[CarvePlan],
    *,
    snapshot_base: Path,
) -> None:
    """Write every carve's span to ``local.yaml`` atomically (plan P5).

    All writes for one detect run sit inside a single :class:`Snapshot`; any
    exception restores ``local.yaml`` to its pre-commit bytes (no half-created
    span), mirroring :func:`setforge.section_promote.execute_promote_to_shared`.
    Overlay carves also reseed the spans sidecar's ``last_deployed_body``.
    """
    local_yaml = override._local_config_path()
    snap = Snapshot(files=[local_yaml], snapshot_base=snapshot_base)
    with snap:
        try:
            overlay_states: dict[str, SpanState] = {}
            for plan in plans:
                override._append_span_host_local(file_id, build_span(plan))
                if plan.kind == "overlay" and plan.seed_state is not None:
                    overlay_states[plan.name] = plan.seed_state
            if overlay_states:
                spans_store.set_states(profile, file_id, overlay_states)
            snap.discard()
        except BaseException:
            snap.restore()
            raise


def _ask(prompt: str) -> str:
    """Read one line of input (thin wrapper, monkeypatched in tests)."""
    from prompt_toolkit import prompt as _pt_prompt

    return str(_pt_prompt(prompt)).strip()


def render_diff(
    target: DetectTarget,
    regions: list[DetectRegion],
    live: str,
    console: Console,
) -> None:
    """Print the changed regions line-numbered (the v1 scrollable diff)."""
    live_lines = _normalise_eol(live).splitlines()
    console.print(
        f"{len(regions)} changed region(s) in {target.dst} "
        "(live vs expected deploy output)"
    )
    for idx, region in enumerate(regions, 1):
        console.print(
            f"  region {idx}: lines {region.live_start + 1}-{region.live_end} "
            f"({region.kind.value})"
        )
        for ln in range(region.live_start, region.live_end):
            if 0 <= ln < len(live_lines):
                console.print(f"   {ln + 1:>4} | {live_lines[ln]}")


def _merge_regions(
    a: DetectRegion, b: DetectRegion, live_lines: list[str]
) -> DetectRegion:
    """Merge two regions into one contiguous span (the wizard's ``extend``).

    The kind escalates to DIVERGENCE if either part diverges (a merged range
    that touches a tracked section can no longer be a pure insertion).
    """
    start = min(a.live_start, b.live_start)
    end = max(a.live_end, b.live_end)
    kind = (
        RegionKind.DIVERGENCE
        if RegionKind.DIVERGENCE in (a.kind, b.kind)
        else RegionKind.NEW_CONTENT
    )
    return DetectRegion(
        kind=kind,
        live_start=start,
        live_end=end,
        expected_start=min(a.expected_start, b.expected_start),
        expected_end=max(a.expected_end, b.expected_end),
        live_text="".join(live_lines[start:end]),
        expected_text=a.expected_text + b.expected_text,
    )


def _carve_one(
    target: DetectTarget,
    region: DetectRegion,
    live: str,
    expected: str,
    console: Console,
) -> CarvePlan | None:
    """Prompt NAME + SCOPE + KIND for one region; return a plan or ``None``.

    Returns ``None`` (skip-with-reason) on empty name, a non-host-local scope
    (detect targets host-local — D7), an unanchorable region
    (:class:`AnchorRefusal`), a divergence on a disposition-less file, or a
    non-unique overlay body (the data-loss uniqueness pre-flight).
    """
    name = _ask("  name: ")
    if not name:
        console.print("  empty name; skipping")
        return None
    # SCOPE is always asked, never auto-defaulted (data-loss pitfall).
    scope = _ask("  scope (host-local/shared): ").lower()
    if scope != "host-local":
        console.print(
            "  detect carves host-local only (shared → use 'section add'); skipping"
        )
        return None
    allowed = allowed_kinds(region, target)
    if not allowed:
        console.print(
            "  a divergence on a file with no disposition can't be pinned/forked; "
            "skipping"
        )
        return None
    kind = allowed[0] if len(allowed) == 1 else _ask(f"  kind ({'/'.join(allowed)}): ")
    kind = kind.lower()
    if kind not in allowed:
        console.print(f"  invalid kind {kind!r}; skipping")
        return None
    anchor = propose_anchor(region, live, expected)
    if isinstance(anchor, AnchorRefusal):
        console.print(f"  cannot anchor: {anchor.reason}; skipping")
        return None
    if kind == "overlay":
        body = region.live_text
        try:
            excise_unique_needle(_normalise_eol(live), [canonical_body(body)])
        except OverlayAmbiguousError:
            console.print(
                "  overlay body appears more than once in live; refusing; skipping"
            )
            return None
        return CarvePlan(
            kind="overlay",
            name=name,
            anchor=anchor,
            body=body,
            semantics="host-local",
            seed_state=seed_overlay_state(name, live, region),
        )
    return CarvePlan(
        kind=kind,
        name=name,
        anchor=pinned_anchor_string(region, live),
        body=None,
        semantics="host-local",
    )


def carve_wizard(
    target: DetectTarget,
    regions: list[DetectRegion],
    live: str,
    expected: str,
    console: Console,
) -> list[CarvePlan]:
    """Walk each region: ``carve`` (name/scope/kind), ``extend`` (merge the next
    region in), or ``skip`` (re-detected next run). Returns the carve plans."""
    live_lines = _normalise_eol(live).splitlines(keepends=True)
    work = list(regions)
    plans: list[CarvePlan] = []
    i = 0
    while i < len(work):
        region = work[i]
        action = _ask(
            f"region {i + 1} lines {region.live_start + 1}-{region.live_end} "
            "[carve/extend/skip]: "
        ).lower()
        if action == "skip":
            i += 1
            continue
        if action == "extend" and i + 1 < len(work):
            work[i] = _merge_regions(region, work[i + 1], live_lines)
            del work[i + 1]
            continue  # re-prompt the merged region
        if action != "carve":
            console.print("  unknown action; skipping")
            i += 1
            continue
        plan = _carve_one(target, region, live, expected, console)
        if plan is not None:
            plans.append(plan)
        i += 1
    return plans


def covered_by_span(region: DetectRegion, live: str, tf: TrackedFile) -> bool:
    """True when ``region`` is already protected by a pinned/forked span.

    A divergence whose enclosing heading anchor matches an existing
    pinned/forked span's anchor is already carved — the pristine baseline shows
    it as drift (the span is not applied there), so it must be subtracted to
    keep re-detect idempotent (plan P2; spec §2b step 3). Overlay-covered
    regions need no subtraction: their body is injected into the baseline, so
    they already match live.
    """
    pinned_forked = {
        s.anchor
        for s in (tf.spans or [])
        if s.kind in (SpanKind.PINNED, SpanKind.FORKED)
    }
    if not pinned_forked:
        return False
    try:
        anchor = pinned_anchor_string(region, live)
    except ValueError:
        return False
    return anchor in pinned_forked


def _overlay_body_needle(span: SpanEntry, states: dict[str, SpanState]) -> str:
    """The canonical bytes that locate ``span``'s body in the pristine baseline."""
    state = states.get(span.anchor)
    if state is not None and state.last_deployed_body:
        return state.last_deployed_body
    assert span.overlay is not None
    assert span.overlay.body is not None
    return canonical_body(span.overlay.body)


def _overlay_expected_range(expected: str, needle: str) -> tuple[int, int] | None:
    """Return the ``[start, end)`` LF-line range ``needle`` occupies in ``expected``.

    ``None`` when the needle is absent (the overlay was not injected here).
    """
    norm = _normalise_eol(expected)
    idx = norm.find(needle)
    if idx < 0:
        return None
    start = norm.count("\n", 0, idx)
    return start, start + needle.count("\n")


def _extract_live_body(live: str, expected: str, a: int, b: int) -> str:
    """Extract the WHOLE live body occupying expected lines ``[a, b)``.

    Prefix (``expected[:a]``) and suffix (``expected[b:]``) match live verbatim
    (only the body differs), so the live body is everything between the shared
    prefix and the shared suffix — captured whole, never truncated to just the
    changed lines (the multi-line data-loss trap).
    """
    exp_lines = _normalise_eol(expected).splitlines(keepends=True)
    live_lines = _normalise_eol(live).splitlines(keepends=True)
    tail = len(exp_lines) - b
    return "".join(live_lines[a : len(live_lines) - tail])


def _overlapping_overlay(
    region: DetectRegion,
    expected: str,
    spans: list[SpanEntry],
    states: dict[str, SpanState],
) -> tuple[SpanEntry, int, int] | None:
    """Return ``(span, a, b)`` for the overlay whose injected body in ``expected``
    overlaps ``region``, else ``None`` (the region is a fresh edit, not a
    re-capture)."""
    for span in overlay_spans(spans):
        rng = _overlay_expected_range(expected, _overlay_body_needle(span, states))
        if rng is None:
            continue
        a, b = rng
        if region.expected_start < b and region.expected_end > a:
            return span, a, b
    return None


def recapture_overlay(
    profile: str,
    file_id: str,
    span_name: str,
    new_body: str,
    *,
    snapshot_base: Path,
) -> None:
    """Update an existing overlay span's body from the live region (S5).

    Canonicalises ``new_body``, rewrites the ``local.yaml`` overlay body, and
    reseeds the sidecar ``last_deployed_body`` so the next deploy excises the NEW
    bytes (closing the byte-needle-drift leak). Atomic under a
    :class:`Snapshot`; raises :class:`KeyError` when no overlay span by that name
    exists.
    """
    canonical = canonical_body(new_body)
    local_yaml = override._local_config_path()
    snap = Snapshot(files=[local_yaml], snapshot_base=snapshot_base)
    with snap:
        try:
            data = override._load_local_data()
            block = override._local_tf_block(data, file_id)
            raw = block.get("spans")
            spans = list(raw) if isinstance(raw, list) else []
            found = False
            for entry in spans:
                if (
                    isinstance(entry, dict)
                    and entry.get("anchor") == span_name
                    and entry.get("kind") == "overlay"
                    and isinstance(entry.get("overlay"), dict)
                ):
                    entry["overlay"]["body"] = canonical
                    found = True
            if not found:
                raise KeyError(f"no overlay span named {span_name!r} for {file_id}")
            block["spans"] = spans
            override._dump_local_data(data)
            states = spans_store.get_states(profile, file_id)
            old = states.get(span_name)
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            if old is not None:
                new_state = dataclasses.replace(
                    old, last_deployed_body=canonical, fingerprint=digest
                )
            else:
                new_state = SpanState(
                    anchor=span_name,
                    fingerprint=digest,
                    prefix=[],
                    suffix=[],
                    position_hint_start_line=0,
                    position_hint_n_lines=canonical.count("\n"),
                    heading_level=0,
                    last_deployed_body=canonical,
                )
            spans_store.set_states(profile, file_id, {span_name: new_state})
            snap.discard()
        except BaseException:
            snap.restore()
            raise


def _split_regions(
    regions: list[DetectRegion],
    live: str,
    expected: str,
    spans: list[SpanEntry],
    states: dict[str, SpanState],
) -> tuple[dict[str, str], list[DetectRegion]]:
    """Partition regions into ``({overlay_name: new_body}, fresh_regions)``.

    A region overlapping an existing overlay's injected body is an EDIT to that
    overlay (S5 re-capture); everything else is a fresh carve.
    """
    recaptures: dict[str, str] = {}
    fresh: list[DetectRegion] = []
    for region in regions:
        match = _overlapping_overlay(region, expected, spans, states)
        if match is None:
            fresh.append(region)
            continue
        span, a, b = match
        recaptures.setdefault(span.anchor, _extract_live_body(live, expected, a, b))
    return recaptures, fresh


def _detect_one_target(
    profile: str,
    target: DetectTarget,
    host_local: dict[HostLocalSectionName, HostLocalSection] | None,
    console: Console,
) -> bool:
    """Run detect for one tracked_file; return True when it had drift."""
    if not target.dst.exists():
        return False
    live = target.dst.read_text(encoding="utf-8")
    expected = expected_deploy_text(profile, target, host_local)
    regions = [
        r
        for r in compute_detect_regions(live, expected)
        if not covered_by_span(r, live, target.tracked_file)
    ]
    if not regions:
        return False
    render_diff(target, regions, live, console)

    spans = target.tracked_file.spans or []
    states = spans_store.get_states(profile, target.name)
    recaptures, fresh = _split_regions(regions, live, expected, spans, states)

    wrote = False
    for span_name, new_body in recaptures.items():
        answer = _ask(f"overlay {span_name!r} body changed [update/skip]: ").lower()
        if answer != "update":
            continue
        recapture_overlay(
            profile,
            target.name,
            span_name,
            new_body,
            snapshot_base=_default_snapshot_base(),
        )
        console.print(f"updated overlay {span_name!r} in local.yaml")
        wrote = True

    plans = carve_wizard(target, fresh, live, expected, console)
    if plans:
        commit_carves(
            profile, target.name, plans, snapshot_base=_default_snapshot_base()
        )
        console.print(f"wrote {len(plans)} span(s) to local.yaml.")
        wrote = True
    if wrote:
        console.print(f"run: setforge install --profile={profile}")
    return True


def run_detect(*, config_path: Path, profile: str, tracked_file: str | None) -> None:
    """Top-level ``section detect`` entry point."""
    cfg = load_config(config_path)
    repo_root = config_path.resolve().parent
    resolved = resolve_profile(cfg, profile)
    # Fold local.yaml host-local overlay spans onto tracked_file.spans so the
    # expected-deploy computation injects them markerless exactly as install
    # does (mirrors compare; without this an already-carved overlay would
    # re-surface as drift on every re-detect).
    apply_host_local_tracked_file_overrides(cfg)
    overlay_map = _load_validated_host_local_sections(cfg, resolved, repo_root)
    console = Console()
    targets = _markdown_targets(cfg, profile, repo_root, tracked_file)
    any_drift = False
    for target in targets:
        if _detect_one_target(profile, target, overlay_map.get(target.name), console):
            any_drift = True
    if not any_drift:
        console.print("no changes detected — live matches expected deploy output")
