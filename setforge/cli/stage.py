"""stage subcommand — per-hunk share/keep classification for plain files (A5).

``setforge stage <file>`` walks each base↔live diff hunk of a plain tracked
file and lets the host classify it SHARED (promote into the shared config on the
next ``sync``) or LOCAL (keep host-only). ``setforge stage --list`` is a
read-only per-file count of SHARED / LOCAL / PENDING hunks — it writes nothing.

The classifications are persisted into the reconcile index; the actual promotion
into ``tracked/`` happens on ``sync`` (see :func:`setforge.capture.capture_profile`).
"""

from __future__ import annotations

import stat
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Final

import typer
from rich.console import Console

from setforge import atomicio, operations
from setforge.cli import (
    _CONFIG_OPTION,
    _PROFILE_OPTION,
    _commit_invocation_state,
    _output_requested,
    _require_output_condition,
    _resolve_config_arg,
    app,
)
from setforge.cli._help_examples import STAGE_EXAMPLES
from setforge.cli._output import OutputContext, render
from setforge.compare import expand_tracked_file, resolve_dst, resolve_src
from setforge.config import (
    Config,
    ResolvedProfile,
    load_config,
    resolve_effective_profile,
)
from setforge.errors import InvariantViolation, StructuredParseError
from setforge.locking import mutation_locks
from setforge.reconcile import hunks as hunks_mod
from setforge.reconcile import index_model
from setforge.reconcile import store as reconcile_store
from setforge.reconcile import structured_units as su_mod
from setforge.reconcile.hunks import Hunk
from setforge.reconcile.merge import split_lines
from setforge.reconcile.structured_units import KeyUnit, StructuredFormat
from setforge.reconcile.types import (
    FileId,
    HunkClass,
    UnitKind,
    UnitRef,
    content_sha,
    file_id,
)
from setforge.scalar_merge import ABSENT
from setforge.ui.primitives import CANCEL, Button, Cancelled

if TYPE_CHECKING:
    from prompt_toolkit.styles import BaseStyle, Style

    from setforge.reconcile.share_draft import DraftResult


def button_bar[T](
    buttons: Sequence[Button[T]],
    *,
    title: str | None = None,
    body: str | list[tuple[str, str]] | None = None,
    initial: int = 0,
    style: BaseStyle | None = None,
) -> T | Cancelled:
    """Load the terminal widget on first interactive use."""
    from setforge.ui.widgets import button_bar as render

    return render(
        buttons,
        title=title,
        body=body,
        initial=initial,
        style=style,
    )


def _themed_style() -> Style:
    """Load prompt-toolkit styling on first interactive use."""
    from setforge.reconcile._claude_ui import _themed_style as build

    return build()


class _ShareDraftProxy:
    """Patchable lazy proxy for the two Claude drafting entry points."""

    def draft_hunk(
        self, region: bytes, *, display_path: str
    ) -> DraftResult | Cancelled:
        from setforge.reconcile.share_draft import draft_hunk

        return draft_hunk(region, display_path=display_path)

    def draft_key_unit(
        self, original: object, *, display_path: str, fmt: StructuredFormat
    ) -> DraftResult | Cancelled:
        from setforge.reconcile.share_draft import draft_key_unit

        return draft_key_unit(original, display_path=display_path, fmt=fmt)


share_draft = _ShareDraftProxy()


@dataclass(frozen=True, slots=True)
class FileStage:
    """One plain file's staged-capture view: its base/live and classified hunks."""

    sub_name: str
    fid: FileId
    src: Path
    dst: Path
    base: bytes
    live: bytes
    hunks: list[Hunk]


class _Quit:
    """Sentinel: the user asked to stop the walk (kept choices persist)."""


QUIT: Final = _Quit()


class _Menu(Enum):
    """Button sentinels for the Share / draft sub-menus (distinct from HunkClass)."""

    SHARE = "share"
    DRAFT = "draft"
    VERBATIM = "verbatim"


@dataclass(frozen=True, slots=True)
class Decision:
    """One hunk's staging outcome from the walk.

    ``cls`` is the class to record. ``draft`` carries the shareable bytes for a
    ``SHARED_DRAFTED`` hunk (``None`` otherwise). ``adopt`` is ``True`` when the
    host also wants their live region rewritten to the draft (no divergence) —
    applied as a batched live-rewrite at persist; the recorded class stays
    ``SHARED_DRAFTED`` either way (classification is live-independent by unit ID).
    """

    cls: HunkClass
    draft: bytes | None = None
    adopt: bool = False


#: A per-hunk choose callback: ``(hunk, index, total) -> Decision | None | QUIT``
#: (``None`` = skip / leave the class unchanged).
type Choice = Callable[[Hunk, int, int], Decision | None | _Quit]


@dataclass(frozen=True, slots=True)
class WalkResult:
    """The walk's outcome, including the refs explicitly acted on."""

    hunks: list[Hunk]
    drafts: dict[UnitRef, bytes]
    adopt_refs: set[UnitRef]
    decided_refs: set[UnitRef]


@dataclass(frozen=True, slots=True)
class _PersistPlan:
    """A fully validated reconcile-store publication prepared under the lock."""

    local: bytes
    staged: bool
    hunks: list[dict[str, object]]
    drafts: dict[UnitRef, bytes]


def collect_stages(
    cfg: Config,
    resolved: ResolvedProfile,
    repo_root: Path,
    profile: str,
    *,
    only: str | None = None,
) -> list[FileStage]:
    """Classified-hunk view for each staged-eligible plain file. READ-ONLY.

    Eligibility mirrors the install reconcile gate: a plain tracked file,
    present live, a recorded merge base, and UTF-8 on both sides. ``only``
    filters to a single file by tracked-file name, sub-name, live path, or live
    basename. Writes nothing — safe for ``--list``.
    """
    stages: list[FileStage] = []
    for name in resolved.tracked_files:
        tracked_file = cfg.tracked_files[name]
        src = resolve_src(tracked_file, repo_root)
        dst = resolve_dst(tracked_file)
        for sub_name, sub_src, sub_dst in expand_tracked_file(name, src, dst):
            if su_mod.structured_format(sub_dst) is not None:
                continue  # structured files stage per-KEY (collect_structured_stages)
            if only is not None and only not in (
                name,
                sub_name,
                str(sub_dst),
                sub_dst.name,
            ):
                continue
            if not sub_dst.exists():
                continue
            fid = file_id(sub_name)
            base = reconcile_store.read_base(profile, fid)
            if base is None:
                continue  # not reconcile-managed (run `setforge install` first)
            entry = reconcile_store.read_index(profile).files.get(str(fid))
            stored = entry.hunks if entry is not None else []
            stored = index_model.require_unit_kind(stored, UnitKind.LINE)
            live = sub_dst.read_bytes()
            try:
                base.decode("utf-8")
                live.decode("utf-8")
            except UnicodeDecodeError:
                continue  # binary plain file — staging is text-only
            hunks = hunks_mod.classify(
                hunks_mod.extract_hunks(base, live),
                stored,
            )
            stages.append(FileStage(sub_name, fid, sub_src, sub_dst, base, live, hunks))
    return stages


@dataclass(frozen=True, slots=True)
class StructuredFileStage:
    """One structured file's staged-capture view: base/live + classified key-units."""

    sub_name: str
    fid: FileId
    src: Path
    dst: Path
    base: bytes
    live: bytes
    fmt: StructuredFormat
    units: list[KeyUnit]


def collect_structured_stages(
    cfg: Config,
    resolved: ResolvedProfile,
    repo_root: Path,
    profile: str,
    *,
    only: str | None = None,
) -> list[StructuredFileStage]:
    """Classified-key-unit view for each staged-eligible structured file. READ-ONLY.

    The structured analog of :func:`collect_stages`: a tracked file whose dst is a
    structured format (yaml/json), present live, with a recorded merge base, is
    parsed into per-KEY units (dotted path identity) classified against the stored
    index. An unparseable structured file is SKIPPED — it gets no interactive
    staging on either path (the line walk skips structured suffixes too); capture
    writes such a file back verbatim. Writes nothing.
    """
    stages: list[StructuredFileStage] = []
    for name in resolved.tracked_files:
        tracked_file = cfg.tracked_files[name]
        src = resolve_src(tracked_file, repo_root)
        dst = resolve_dst(tracked_file)
        for sub_name, sub_src, sub_dst in expand_tracked_file(name, src, dst):
            fmt = su_mod.structured_format(sub_dst)
            if fmt is None:
                continue
            if only is not None and only not in (
                name,
                sub_name,
                str(sub_dst),
                sub_dst.name,
            ):
                continue
            if not sub_dst.exists():
                continue
            fid = file_id(sub_name)
            base = reconcile_store.read_base(profile, fid)
            if base is None:
                continue  # not reconcile-managed (run `setforge install` first)
            entry = reconcile_store.read_index(profile).files.get(str(fid))
            stored = entry.hunks if entry is not None else []
            stored = index_model.require_unit_kind(stored, UnitKind.KEY)
            live = sub_dst.read_bytes()
            try:
                fresh = su_mod.extract_structured_units(base, live, fmt)
            except StructuredParseError:
                continue  # unparseable → no interactive staging; capture is verbatim
            units = su_mod.classify_structured(fresh, stored)
            stages.append(
                StructuredFileStage(
                    sub_name, fid, sub_src, sub_dst, base, live, fmt, units
                )
            )
    return stages


#: A per-key-unit choose callback, mirroring :data:`Choice` for key-units.
type StructuredChoice = Callable[[KeyUnit, int, int], Decision | None | _Quit]


@dataclass(frozen=True, slots=True)
class StructuredWalkResult:
    """The structured walk's outcome, including explicitly decided refs."""

    units: list[KeyUnit]
    drafts: dict[UnitRef, bytes]
    adopt_refs: set[UnitRef]
    decided_refs: set[UnitRef]


def walk_structured(
    units: list[KeyUnit], choose: StructuredChoice
) -> StructuredWalkResult:
    """Apply one :class:`Decision` per key-unit, keyed by the unit's PATH.

    The structured analog of :func:`walk`: identical control flow, but a unit's
    identity is its dotted ``path`` (not a line ``unit_id``), so drafts and the
    adopt set use typed KEY references.
    """
    out = list(units)
    drafts: dict[UnitRef, bytes] = {}
    adopt_refs: set[UnitRef] = set()
    decided_refs: set[UnitRef] = set()
    for index, unit in enumerate(units):
        decision = choose(unit, index, len(units))
        if isinstance(decision, _Quit):
            break
        if decision is None:
            continue
        draft_hash = content_sha(decision.draft) if decision.draft is not None else None
        out[index] = replace(
            unit,
            cls=decision.cls,
            changed=False,
            confirmed_hash=unit.value_hash,
            draft_hash=draft_hash,
        )
        decided_refs.add(unit.ref)
        if decision.draft is not None:
            drafts[unit.ref] = decision.draft
        if decision.adopt:
            adopt_refs.add(unit.ref)
    return StructuredWalkResult(
        units=out,
        drafts=drafts,
        adopt_refs=adopt_refs,
        decided_refs=decided_refs,
    )


def _apply_structured(
    profile: str,
    stage: StructuredFileStage,
    result: StructuredWalkResult,
    *,
    config_dir: Path | None = None,
) -> None:
    """Persist a structured walk's classifications + drafts under ONE profile lock.

    Adopt-locally (rewriting host live to a structured draft) is a follow-up; this
    persists classifications against the unchanged live bytes. The lock spans the
    whole record here (and, once adopt-locally lands, its live write too) —
    mirroring :func:`_apply` so a concurrent install/sync cannot interleave.
    """
    with mutation_locks(config_dir=config_dir, profile=profile):
        operations.refuse_active(profile)
        locked_live = stage.dst.read_bytes()
        plan = _prepare_structured_persist(
            profile, stage, result, locked_live, observed_live=locked_live
        )
        _commit_persist(profile, stage.fid, stage.base, plan)


def _validate_structured_decisions(
    stage: StructuredFileStage,
    result: StructuredWalkResult,
    observed_live: bytes,
) -> None:
    """Refuse a decision whose key no longer uniquely has the value shown."""
    if not result.decided_refs:
        return
    observed = su_mod.extract_structured_units(stage.base, observed_live, stage.fmt)
    for ref in result.decided_refs:
        shown = [unit for unit in stage.units if unit.ref == ref]
        matches = [unit for unit in observed if unit.ref == ref]
        if (
            len(shown) != 1
            or len(matches) != 1
            or shown[0].value_hash != matches[0].value_hash
        ):
            raise InvariantViolation(
                f"staged unit {ref} changed after it was shown; run stage again"
            )


def _require_current_base(
    profile: str, fid: FileId, expected: bytes, *, display_name: str
) -> None:
    """Refuse a stage collected against a base that changed before publication."""
    if reconcile_store.read_base(profile, fid) != expected:
        raise InvariantViolation(
            f"recorded base for {display_name!r} changed after it was shown; "
            "run stage again"
        )


def _prepare_structured_persist(
    profile: str,
    stage: StructuredFileStage,
    result: StructuredWalkResult,
    final_live: bytes,
    *,
    observed_live: bytes | None = None,
) -> _PersistPlan:
    """Build a structured publication after validating every persisted input.

    The structured analog of :func:`_persist`: same lost-update RMW (re-read +
    re-extract with the caller's lock held, overlay ONLY the paths the host
    explicitly decided), keyed by dotted ``path`` instead of line ``anchor``, using
    the structured extract/classify/serialize. base is UNCHANGED (sync/install own
    it); the drafts manifest is reconciled to exactly the surviving SHARED_DRAFTED
    set.
    """
    _require_current_base(profile, stage.fid, stage.base, display_name=stage.sub_name)
    _validate_structured_decisions(
        stage, result, final_live if observed_live is None else observed_live
    )
    walk_by_ref = {unit.ref: unit for unit in result.units}
    entry = reconcile_store.read_index(profile).files.get(str(stage.fid))
    stored = entry.hunks if entry is not None else []
    stored = index_model.require_unit_kind(stored, UnitKind.KEY)
    # Validate the current payload/index/draft quad before publishing a rewrite.
    reconcile_store.verify(profile, stage.fid)
    current = su_mod.classify_structured(
        su_mod.extract_structured_units(stage.base, final_live, stage.fmt),
        stored,
    )
    merged = [
        replace(
            u,
            cls=walk_by_ref[u.ref].cls,
            changed=False,
            confirmed_hash=u.value_hash,
            draft_hash=walk_by_ref[u.ref].draft_hash,
        )
        if u.ref in result.decided_refs
        else u
        for u in current
    ]
    pool = {**reconcile_store.read_drafts(profile, stage.fid), **result.drafts}
    drafts: dict[UnitRef, bytes] = {}
    for unit in merged:
        if unit.cls is not HunkClass.SHARED_DRAFTED:
            continue
        if unit.draft_hash is None or unit.ref not in pool:
            raise InvariantViolation(
                f"SHARED_DRAFTED unit {unit.ref} has no usable draft"
            )
        draft = pool[unit.ref]
        if content_sha(draft) != unit.draft_hash:
            raise InvariantViolation(
                f"draft bytes for {unit.ref} do not match the recorded draft_hash"
            )
        drafts[unit.ref] = draft
    return _PersistPlan(
        local=final_live,
        staged=(entry.staged if entry is not None else False)
        or bool(result.decided_refs),
        hunks=su_mod.serialize_structured(merged),
        drafts=drafts,
    )


def _persist_structured(
    profile: str,
    stage: StructuredFileStage,
    result: StructuredWalkResult,
    final_live: bytes,
    *,
    observed_live: bytes | None = None,
) -> None:
    """Prepare and record a structured walk while the caller holds the lock."""
    plan = _prepare_structured_persist(
        profile,
        stage,
        result,
        final_live,
        observed_live=observed_live,
    )
    _commit_persist(profile, stage.fid, stage.base, plan)


def counts(hunks: list[Hunk]) -> Counter[HunkClass]:
    """Tally hunks by class (SHARED / LOCAL / PENDING)."""
    return Counter(hunk.cls for hunk in hunks)


def walk(hunks: list[Hunk], choose: Choice) -> WalkResult:
    """Apply one :class:`Decision` per hunk, collecting drafts + the adopt set.

    ``choose(hunk, index, total)`` returns a :class:`Decision` to (re)classify,
    ``None`` to leave the hunk unchanged (skip / next), or :data:`QUIT` to stop
    early. Choices made before a QUIT are kept. A drafted decision records the
    hunk's ``draft_hash`` and stashes its bytes under a typed LINE reference; an
    ``adopt`` decision additionally marks that unit for the live-rewrite.
    """
    out = list(hunks)
    drafts: dict[UnitRef, bytes] = {}
    adopt_refs: set[UnitRef] = set()
    decided_refs: set[UnitRef] = set()
    for index, hunk in enumerate(hunks):
        decision = choose(hunk, index, len(hunks))
        if isinstance(decision, _Quit):
            break
        if decision is None:
            continue
        draft_hash = content_sha(decision.draft) if decision.draft is not None else None
        out[index] = replace(
            hunk,
            cls=decision.cls,
            changed=False,
            confirmed_hash=hunk.live_hash,
            draft_hash=draft_hash,
        )
        decided_refs.add(hunk.ref)
        if decision.draft is not None:
            drafts[hunk.ref] = decision.draft
        if decision.adopt:
            adopt_refs.add(hunk.ref)
    return WalkResult(
        hunks=out,
        drafts=drafts,
        adopt_refs=adopt_refs,
        decided_refs=decided_refs,
    )


def _hunk_preview(stage: FileStage, hunk: Hunk) -> str:
    """A small ±diff preview of one hunk for the button-bar body."""
    base_lines = split_lines(stage.base)
    live_lines = split_lines(stage.live)
    i1, i2 = hunk.base_span
    j1, j2 = hunk.live_span
    removed = [b"- " + line for line in base_lines[i1:i2]]
    added = [b"+ " + line for line in live_lines[j1:j2]]
    body = b"".join(removed + added).decode("utf-8", "replace")
    return body if len(body) <= 600 else body[:599] + "…"


def _interactive_choice(stage: FileStage) -> Choice:
    """A button-bar-backed choose callback for the interactive walk."""
    style = _themed_style()

    def choose(hunk: Hunk, index: int, total: int) -> Decision | None | _Quit:
        flag = " (changed)" if hunk.changed else ""
        current = f"currently {hunk.cls.value}"
        result = button_bar(
            [
                Button("Share", _Menu.SHARE),
                Button("Keep local", HunkClass.LOCAL),
                Button("Skip", None),
                Button("Quit", QUIT),
            ],
            title=f"stage {stage.sub_name} — hunk {index + 1}/{total}: "
            f"{hunk.label}{flag}",
            body=f"{_hunk_preview(stage, hunk)}\n[{current}]",
            initial=0 if hunk.cls is not HunkClass.LOCAL else 1,
            style=style,
        )
        if result is CANCEL or isinstance(result, _Quit):
            return QUIT  # Esc / Ctrl-C / Quit stops the walk, keeping prior choices
        if result is None:
            return None  # Skip — leave the class unchanged
        if result is HunkClass.LOCAL:
            return Decision(HunkClass.LOCAL)
        return _share_submenu(stage, hunk, style)  # Share → how to share

    return choose


def _share_submenu(stage: FileStage, hunk: Hunk, style: Style) -> Decision | None:
    """The Share sub-menu: draft (Claude rewrite) / verbatim / skip.

    Returns a SHARED_DRAFTED :class:`Decision` (carrying the draft + adopt flag),
    a plain SHARED Decision (verbatim), or ``None`` (skip / back / draft-cancelled
    — the hunk is left unchanged). The draft session is constructed per-hunk inside
    :func:`share_draft.draft_hunk` and discarded on accept/cancel.
    """
    result = button_bar(
        [
            Button("Draft (Claude)", _Menu.DRAFT),
            Button("Verbatim", _Menu.VERBATIM),
            Button("Skip", None),
        ],
        title=f"share {hunk.label} — rewrite host-specific text?",
        body=(
            "Draft: Claude rewrites this region into a shareable version "
            "(then adopt it locally or keep your local bytes).\n"
            "Verbatim: share your live bytes as-is."
        ),
        initial=0,
        style=style,
    )
    if result is CANCEL or result is None:
        return None  # back / skip → leave unchanged
    if result is _Menu.VERBATIM:
        return Decision(HunkClass.SHARED)
    # Draft: hand the host-specific live region to Claude for a shareable rewrite.
    j1, j2 = hunk.live_span
    region = b"".join(split_lines(stage.live)[j1:j2])
    outcome = share_draft.draft_hunk(region, display_path=stage.sub_name)
    if outcome is CANCEL:
        return None  # draft cancelled → leave the hunk unchanged
    return Decision(HunkClass.SHARED_DRAFTED, draft=outcome.draft, adopt=outcome.adopt)


def _struct_counts(units: list[KeyUnit]) -> Counter[HunkClass]:
    """Tally key-units by class (SHARED / LOCAL / PENDING)."""
    return Counter(unit.cls for unit in units)


def _unit_preview(stage: StructuredFileStage, unit: KeyUnit) -> str:
    """A small base→live value preview of one key-unit for the button-bar body."""
    base_val = su_mod.value_preview(stage.base, unit.path, stage.fmt)
    live_val = su_mod.value_preview(stage.live, unit.path, stage.fmt)
    body = f"  {unit.path}:\n- {base_val}\n+ {live_val}"
    return body if len(body) <= 600 else body[:599] + "…"


def _structured_interactive_choice(stage: StructuredFileStage) -> StructuredChoice:
    """A button-bar-backed choose callback for the interactive structured walk."""
    style = _themed_style()

    def choose(unit: KeyUnit, index: int, total: int) -> Decision | None | _Quit:
        flag = " (changed)" if unit.changed else ""
        result = button_bar(
            [
                Button("Share", _Menu.SHARE),
                Button("Keep local", HunkClass.LOCAL),
                Button("Skip", None),
                Button("Quit", QUIT),
            ],
            title=f"stage {stage.sub_name} — key {index + 1}/{total}: "
            f"{unit.path}{flag}",
            body=f"{_unit_preview(stage, unit)}\n[currently {unit.cls.value}]",
            initial=0 if unit.cls is not HunkClass.LOCAL else 1,
            style=style,
        )
        if result is CANCEL or isinstance(result, _Quit):
            return QUIT
        if result is None:
            return None
        if result is HunkClass.LOCAL:
            return Decision(HunkClass.LOCAL)
        return _structured_share_submenu(stage, unit, style)  # _Menu.SHARE → how

    return choose


def _structured_share_submenu(
    stage: StructuredFileStage, unit: KeyUnit, style: Style
) -> Decision | None:
    """The structured Share sub-menu: draft (Claude rewrite) / verbatim / skip.

    The key-unit sibling of :func:`_share_submenu`. Returns a SHARED_DRAFTED
    :class:`Decision` (carrying the type-confined scalar draft + adopt flag), a
    plain SHARED Decision (verbatim live value), or ``None`` (skip / back /
    draft-cancelled — the unit is left unchanged). The draft is bounded to a
    same-type scalar inside :func:`share_draft.draft_key_unit`; the live value at
    the unit's path is read as the type anchor.
    """
    result = button_bar(
        [
            Button("Draft (Claude)", _Menu.DRAFT),
            Button("Verbatim", _Menu.VERBATIM),
            Button("Skip", None),
        ],
        title=f"share {unit.path} — rewrite host-specific value?",
        body=(
            "Draft: Claude rewrites this value into a shareable scalar (same type); "
            "your local value stays — only the shareable scalar is promoted.\n"
            "Verbatim: share your live value as-is."
        ),
        initial=0,
        style=style,
    )
    if result is CANCEL or result is None:
        return None  # back / skip → leave unchanged
    if result is _Menu.VERBATIM:
        return Decision(HunkClass.SHARED)
    # Draft: hand the live scalar to Claude for a shareable, type-confined rewrite.
    original = su_mod.value_at(stage.live, unit.path, stage.fmt)
    if original is ABSENT:
        # The host deleted this leaf live — there is no scalar to generalise, and
        # an absent type-anchor would re-prompt forever. Leave the unit unchanged.
        return None
    # Adopt-locally is not wired for structured drafts (live-rewrite is a follow-up),
    # so the key-unit draft is keep-local only — never advertise or return adopt.
    outcome = share_draft.draft_key_unit(
        original, display_path=stage.sub_name, fmt=stage.fmt
    )
    if outcome is CANCEL:
        return None  # draft cancelled → leave the unit unchanged
    return Decision(HunkClass.SHARED_DRAFTED, draft=outcome.draft)


def _adopt_live(stage: FileStage, result: WalkResult) -> bytes:
    """Splice each adopted hunk's draft into the live bytes (the Adopt rewrite).

    Returns ``stage.live`` unchanged when nothing was adopted. Each adopted region
    is replaced by its draft; every other region (including a keep-mine-local
    drafted hunk, whose live stays host-specific) passes through verbatim. Because
    a ``SHARED_DRAFTED`` hunk is matched by unit ID (unchanged by the
    rewrite), re-extraction after the rewrite re-identifies it cleanly.
    """
    if not result.adopt_refs:
        return stage.live
    live_lines = split_lines(stage.live)
    adopted = sorted(
        (h for h in result.hunks if h.ref in result.adopt_refs),
        key=lambda h: h.live_span[0],
    )
    out: list[bytes] = []
    cursor = 0
    for hunk in adopted:
        j1, j2 = hunk.live_span
        out.extend(live_lines[cursor:j1])
        out.append(result.drafts[hunk.ref])
        cursor = j2
    out.extend(live_lines[cursor:])
    return b"".join(out)


def _apply(
    profile: str,
    stage: FileStage,
    result: WalkResult,
    *,
    config_dir: Path | None = None,
) -> None:
    """Apply the walk under ONE profile lock: rewrite live for any Adopt (atomic,
    captured mode), then persist the classifications + drafts.

    The live write and the index record share a single lock span — mirroring how
    install/sync/revert hold the lock across their whole mutating region — so a
    concurrent install/sync cannot land between the write and the record and leave
    a live tree whose bytes no longer match the classifications persisted here.
    """
    with mutation_locks(config_dir=config_dir, profile=profile):
        operations.refuse_active(profile)
        locked_live = stage.dst.read_bytes()
        if result.adopt_refs and locked_live != stage.live:
            raise InvariantViolation(
                f"staged file {stage.sub_name!r} changed after it was shown; "
                "run stage again"
            )
        final_live = _adopt_live(stage, result) if result.adopt_refs else locked_live
        plan = _prepare_persist(
            profile, stage, result, final_live, observed_live=locked_live
        )
        if final_live != locked_live:
            # stat() (follow) so a symlinked dst keeps its target's mode, not the
            # link's.
            mode = stat.S_IMODE(stage.dst.stat().st_mode)
            atomicio.atomic_write_bytes(stage.dst, final_live, mode=mode)
        _commit_persist(profile, stage.fid, stage.base, plan)


def _validate_line_decisions(
    stage: FileStage,
    result: WalkResult,
    observed_live: bytes,
) -> None:
    """Refuse a decision whose unit no longer uniquely has the bytes shown."""
    if not result.decided_refs:
        return
    observed = hunks_mod.extract_hunks(stage.base, observed_live)
    for ref in result.decided_refs:
        shown = [hunk for hunk in stage.hunks if hunk.ref == ref]
        matches = [hunk for hunk in observed if hunk.ref == ref]
        if (
            len(shown) != 1
            or len(matches) != 1
            or shown[0].live_hash != matches[0].live_hash
        ):
            raise InvariantViolation(
                f"staged unit {ref} changed after it was shown; run stage again"
            )


def _prepare_persist(
    profile: str,
    stage: FileStage,
    result: WalkResult,
    final_live: bytes,
    *,
    observed_live: bytes | None = None,
) -> _PersistPlan:
    """Build a line-unit publication after validating every persisted input.

    The walk read + classified the index at collect time, OUTSIDE the lock; a
    naive whole-list overwrite here would drop any classification a concurrent
    ``sync`` committed in between. Instead, re-read the index (the caller's lock
    still held), re-extract the (post-Adopt) base/live, and overlay ONLY the units
    the host explicitly decided (class changed from collect time, OR a draft
    attached) — so a unit the host skipped keeps whatever the concurrent writer
    left, while the host's explicit choices win. base is UNCHANGED (sync/install
    own it).

    The drafts manifest is reconciled to EXACTLY the surviving ``SHARED_DRAFTED``
    set: prior drafts are kept, this walk's are added, and any whose hunk demoted
    away is pruned — so a demote never leaves an orphan manifest entry.

    ``decided_refs`` records button actions directly, so choosing the same class
    is a real re-confirmation while Skip/QUIT remain passive. Each decided ref is
    revalidated against the live bytes observed under the caller's lock before it
    can update its fingerprint.
    """
    _require_current_base(profile, stage.fid, stage.base, display_name=stage.sub_name)
    _validate_line_decisions(
        stage, result, final_live if observed_live is None else observed_live
    )
    walk_by_ref = {hunk.ref: hunk for hunk in result.hunks}
    entry = reconcile_store.read_index(profile).files.get(str(stage.fid))
    stored = entry.hunks if entry is not None else []
    stored = index_model.require_unit_kind(stored, UnitKind.LINE)
    reconcile_store.verify(profile, stage.fid)
    current = hunks_mod.classify(
        hunks_mod.extract_hunks(stage.base, final_live),
        stored,
    )
    merged = [
        replace(
            h,
            cls=walk_by_ref[h.ref].cls,
            changed=False,
            confirmed_hash=h.live_hash,
            draft_hash=walk_by_ref[h.ref].draft_hash,
        )
        if h.ref in result.decided_refs
        else h
        for h in current
    ]
    pool = {
        **hunks_mod.bind_drafts(
            current, reconcile_store.read_drafts(profile, stage.fid)
        ),
        **result.drafts,
    }
    drafts: dict[UnitRef, bytes] = {}
    for hunk in merged:
        if hunk.cls is not HunkClass.SHARED_DRAFTED:
            continue
        if hunk.draft_hash is None or hunk.ref not in pool:
            raise InvariantViolation(
                f"SHARED_DRAFTED unit {hunk.ref} has no usable draft"
            )
        draft = pool[hunk.ref]
        if content_sha(draft) != hunk.draft_hash:
            raise InvariantViolation(
                f"draft bytes for {hunk.ref} do not match the recorded draft_hash"
            )
        drafts[hunk.ref] = draft
    return _PersistPlan(
        local=final_live,
        staged=(entry.staged if entry is not None else False)
        or bool(result.decided_refs),
        hunks=hunks_mod.serialize(merged),
        drafts=drafts,
    )


def _commit_persist(profile: str, fid: FileId, base: bytes, plan: _PersistPlan) -> None:
    """Publish one already validated reconcile-store plan."""
    reconcile_store.record(
        profile,
        fid,
        base=base,
        local=plan.local,
        staged=plan.staged,
        hunks=plan.hunks,
        drafts=plan.drafts,
    )


def _persist(
    profile: str,
    stage: FileStage,
    result: WalkResult,
    final_live: bytes,
    *,
    observed_live: bytes | None = None,
) -> None:
    """Prepare and record a line-unit walk while the caller holds the lock."""
    plan = _prepare_persist(
        profile,
        stage,
        result,
        final_live,
        observed_live=observed_live,
    )
    _commit_persist(profile, stage.fid, stage.base, plan)


@app.command(epilog=STAGE_EXAMPLES)
def stage(
    ctx: typer.Context,
    file: str = typer.Argument(
        None, help="Tracked file to stage (name or live path). Omit with --list."
    ),
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    list_only: bool = typer.Option(
        False, "--list", help="Read-only: per-file share/local/pending hunk counts."
    ),
) -> None:
    """Classify a plain file's local changes per hunk: share upstream or keep local."""
    _require_output_condition(
        ctx.obj,
        supported=list_only,
        command="stage without --list",
    )
    if _output_requested(ctx.obj):
        _commit_invocation_state(ctx)
    config = _resolve_config_arg(config)
    cfg = load_config(config)
    repo_root = config.resolve().parent
    resolved = resolve_effective_profile(cfg, profile, repo_root).resolved

    if list_only:
        stages = collect_stages(cfg, resolved, repo_root, profile)
        struct = collect_structured_stages(cfg, resolved, repo_root, profile)
        _render_list(ctx.obj, stages, struct)
        return

    if file is None:
        raise typer.BadParameter("stage requires a FILE argument (or use --list)")
    if not sys.stdin.isatty():
        typer.secho(
            "stage is interactive; run `setforge stage --list` to inspect "
            "hunk classes non-interactively",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=2)

    stages = collect_stages(cfg, resolved, repo_root, profile, only=file)
    struct = collect_structured_stages(cfg, resolved, repo_root, profile, only=file)
    if not stages and not struct:
        typer.secho(
            f"{file}: nothing to stage — no local changes over a recorded base "
            f"(run `setforge install --profile={profile}` first if it is new)",
            err=True,
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=0)

    console = Console()
    for stage_item in stages:
        if not stage_item.hunks:
            continue
        result = walk(stage_item.hunks, _interactive_choice(stage_item))
        _apply(profile, stage_item, result, config_dir=repo_root)
        tally = counts(result.hunks)
        drafted = tally[HunkClass.SHARED_DRAFTED]
        drafted_note = f"  {drafted} drafted" if drafted else ""
        console.print(
            f"{stage_item.sub_name}: "
            f"{tally[HunkClass.SHARED]} shared{drafted_note}  "
            f"{tally[HunkClass.LOCAL]} local  "
            f"{tally[HunkClass.PENDING]} pending"
        )
    for struct_item in struct:
        if not struct_item.units:
            continue
        sresult = walk_structured(
            struct_item.units, _structured_interactive_choice(struct_item)
        )
        _apply_structured(profile, struct_item, sresult, config_dir=repo_root)
        stally = _struct_counts(sresult.units)
        sdrafted = stally[HunkClass.SHARED_DRAFTED]
        sdrafted_note = f"  {sdrafted} drafted" if sdrafted else ""
        console.print(
            f"{struct_item.sub_name}: "
            f"{stally[HunkClass.SHARED]} shared{sdrafted_note}  "
            f"{stally[HunkClass.LOCAL]} local  "
            f"{stally[HunkClass.PENDING]} pending"
        )


def _render_list(
    ctx_obj: OutputContext | None,
    stages: list[FileStage],
    struct: list[StructuredFileStage] | None = None,
) -> None:
    """Render the read-only per-file class table (line hunks + structured keys)."""
    data = [
        {
            "name": stage.sub_name,
            "shared": counts(stage.hunks)[HunkClass.SHARED],
            "local": counts(stage.hunks)[HunkClass.LOCAL],
            "pending": counts(stage.hunks)[HunkClass.PENDING],
        }
        for stage in stages
    ]
    data += [
        {
            "name": s.sub_name,
            "shared": _struct_counts(s.units)[HunkClass.SHARED],
            "local": _struct_counts(s.units)[HunkClass.LOCAL],
            "pending": _struct_counts(s.units)[HunkClass.PENDING],
        }
        for s in (struct or [])
    ]

    def _human() -> None:
        console = Console()
        if not data:
            console.print("no staged-eligible files with local changes")
            return
        for row in data:
            console.print(
                f"{row['name']}: "
                f"{row['shared']} shared  {row['local']} local  "
                f"{row['pending']} pending"
            )

    render(ctx_obj, "stage", data, human_fn=_human)
