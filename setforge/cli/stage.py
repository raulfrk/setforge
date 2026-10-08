"""stage subcommand — per-unit share/keep classification of a tracked file (A5).

``setforge stage <file>`` walks each base↔live difference of a tracked file —
a line hunk of a plain file, a key of a YAML/JSON file — and lets the host
classify it SHARED (promote into the shared config on the next ``sync``) or
LOCAL (keep host-only). ``setforge stage --list`` is a read-only per-file count
of SHARED / LOCAL / PENDING units — it writes nothing.

The classifications are persisted into the reconcile index; the actual promotion
into ``tracked/`` happens on ``sync`` (see :func:`setforge.capture.plan_capture`).
"""

from __future__ import annotations

import stat
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

import typer

from setforge import atomicio, operations, transitions
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
from setforge.cli._output import OutputContext, make_console, render
from setforge.compare import expand_tracked_file, resolve_dst, resolve_src
from setforge.config import (
    Config,
    ResolvedProfile,
    load_config,
    resolve_effective_profile,
)
from setforge.errors import InvariantViolation, StructuredParseError
from setforge.file_ownership import (
    FileAction,
    FileDecision,
    decide_file,
    observe_file,
    publish_file_claim_locked,
)
from setforge.ownership import (
    OwnershipError,
    OwnershipStore,
    ResourceId,
    load_or_create_owner_id,
    read_owner_id,
    read_owner_id_locked,
    resolve_owner_common_dir,
)
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
from setforge.reconcile.unit_engine import UnitEngine, engine_for
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
    from setforge.ui.widgets import themed_style

    return themed_style()


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
class FileStage[U: (Hunk, KeyUnit)]:
    """One file's staged-capture view: its base/live and classified units."""

    sub_name: str
    fid: FileId
    src: Path
    dst: Path
    base: bytes
    live: bytes
    units: list[U]
    engine: UnitEngine[U]
    participating: bool = False
    ownership: FileDecision | None = None


def _file_ownership(repo_root: Path, dst: Path) -> FileDecision:
    """Build one read-only container decision for stage/list diagnostics."""
    observation = observe_file(dst)
    store = OwnershipStore()
    claim = store.read(observation.resource_id)
    try:
        owner_id = read_owner_id(repo_root)
    except OwnershipError:
        owner_id = claim.owner_id if claim is not None else UUID(int=0)
    return decide_file(observation, claim, owner_id=owner_id)


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


#: A per-unit choose callback: ``(unit, index, total) -> Decision | None | QUIT``
#: (``None`` = skip / leave the class unchanged).
type Choice[U: (Hunk, KeyUnit)] = Callable[[U, int, int], Decision | None | _Quit]


@dataclass(frozen=True, slots=True)
class WalkResult[U: (Hunk, KeyUnit)]:
    """The walk's outcome, including the refs explicitly acted on."""

    units: list[U]
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


def _declares_id(
    cfg: Config, resolved: ResolvedProfile, repo_root: Path, wanted: str
) -> bool:
    """Whether ``wanted`` is the name or a directory sub-name of a stageable file."""
    for name in resolved.tracked_files:
        tracked_file = cfg.tracked_files[name]
        if tracked_file.generated is not None or tracked_file.tree is not None:
            continue
        src = resolve_src(tracked_file, repo_root)
        dst = resolve_dst(tracked_file)
        if any(
            wanted in (name, sub_name)
            for sub_name, _sub_src, _sub_dst in expand_tracked_file(name, src, dst)
        ):
            return True
    return False


def _collect(
    cfg: Config,
    resolved: ResolvedProfile,
    repo_root: Path,
    profile: str,
    kind: UnitKind,
    *,
    only: str | None,
    include_ownership: bool,
) -> list[FileStage[Any]]:
    """Classified-unit view for each staged-eligible file of one unit kind.

    READ-ONLY — safe for ``--list``. Eligibility mirrors the install reconcile
    gate: a tracked file whose engine (:func:`engine_for`) stages as ``kind``,
    present live, with a recorded merge base, and UTF-8 text that its engine can
    read. A binary file, or a live file its key engine cannot parse, gets no unit
    staging; capture writes it back verbatim.
    ``only`` filters to a single file: by tracked-file name or sub-name when one
    declares it, otherwise by live path or live basename.
    """
    by_id = only is not None and _declares_id(cfg, resolved, repo_root, only)
    stages: list[FileStage[Any]] = []
    for name in resolved.tracked_files:
        tracked_file = cfg.tracked_files[name]
        if tracked_file.generated is not None or tracked_file.tree is not None:
            continue
        src = resolve_src(tracked_file, repo_root)
        dst = resolve_dst(tracked_file)
        for sub_name, sub_src, sub_dst in expand_tracked_file(name, src, dst):
            if kind is UnitKind.KEY and su_mod.structured_format(sub_dst) is None:
                continue
            if only is not None and only not in (
                (name, sub_name) if by_id else (str(sub_dst), sub_dst.name)
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
            engine = engine_for(sub_dst, base, stored)
            if engine.kind is not kind:
                continue
            stored = index_model.require_unit_kind(stored, kind)
            live = sub_dst.read_bytes()
            try:
                base.decode("utf-8")
                live.decode("utf-8")
                fresh = engine.extract(base, live)
            except (UnicodeDecodeError, StructuredParseError):
                continue
            stages.append(
                FileStage(
                    sub_name,
                    fid,
                    sub_src,
                    sub_dst,
                    base,
                    live,
                    engine.classify(fresh, stored),
                    engine,
                    entry.staged if entry is not None else False,
                    _file_ownership(repo_root, sub_dst) if include_ownership else None,
                )
            )
    return stages


def collect_stages(
    cfg: Config,
    resolved: ResolvedProfile,
    repo_root: Path,
    profile: str,
    *,
    only: str | None = None,
    include_ownership: bool = True,
) -> list[FileStage[Hunk]]:
    """The line-hunk stages: each staged-eligible file staged by line."""
    return _collect(
        cfg,
        resolved,
        repo_root,
        profile,
        UnitKind.LINE,
        only=only,
        include_ownership=include_ownership,
    )


def collect_structured_stages(
    cfg: Config,
    resolved: ResolvedProfile,
    repo_root: Path,
    profile: str,
    *,
    only: str | None = None,
    include_ownership: bool = True,
) -> list[FileStage[KeyUnit]]:
    """The key-unit stages: each staged-eligible file staged by key."""
    return _collect(
        cfg,
        resolved,
        repo_root,
        profile,
        UnitKind.KEY,
        only=only,
        include_ownership=include_ownership,
    )


@dataclass(frozen=True, slots=True)
class StageSummary:
    """One immutable, command-neutral staging classification summary."""

    name: str
    participating: bool
    shared: int
    shared_promotable: int
    drafted: int
    reconfirm_required: int
    local: int
    pending: int
    blockers: tuple[str, ...]
    ownership: str

    def to_dict(self) -> dict[str, Any]:
        """Return the stable stage JSON schema and key order."""
        return {
            "name": self.name,
            "participating": self.participating,
            "shared": self.shared,
            "shared_promotable": self.shared_promotable,
            "drafted": self.drafted,
            "reconfirm_required": self.reconfirm_required,
            "local": self.local,
            "pending": self.pending,
            "blockers": list(self.blockers),
            "ownership": self.ownership,
        }


def _require_current_base(
    profile: str, fid: FileId, expected: bytes, *, display_name: str
) -> None:
    """Refuse a stage collected against a base that changed before publication."""
    if reconcile_store.read_base(profile, fid) != expected:
        raise InvariantViolation(
            f"recorded base for {display_name!r} changed after it was shown; "
            "run stage again"
        )


def counts[U: (Hunk, KeyUnit)](units: list[U]) -> Counter[HunkClass]:
    """Tally units by class (SHARED / LOCAL / PENDING)."""
    return Counter(unit.cls for unit in units)


def walk[U: (Hunk, KeyUnit)](units: list[U], choose: Choice[U]) -> WalkResult[U]:
    """Apply one :class:`Decision` per unit, collecting drafts + the adopt set.

    ``choose(unit, index, total)`` returns a :class:`Decision` to (re)classify,
    ``None`` to leave the unit unchanged (skip / next), or :data:`QUIT` to stop
    early. Choices made before a QUIT are kept. A drafted decision records the
    unit's ``draft_hash`` and stashes its bytes under the unit's typed reference;
    an ``adopt`` decision additionally marks that unit for the live-rewrite.
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
            confirmed_hash=unit.content_hash,
            draft_hash=draft_hash,
        )
        decided_refs.add(unit.ref)
        if decision.draft is not None:
            drafts[unit.ref] = decision.draft
        if decision.adopt:
            adopt_refs.add(unit.ref)
    return WalkResult(
        units=out,
        drafts=drafts,
        adopt_refs=adopt_refs,
        decided_refs=decided_refs,
    )


def _interactive_choice[U: (Hunk, KeyUnit)](stage: FileStage[U]) -> Choice[U]:
    """A button-bar-backed choose callback for the interactive walk."""
    style = _themed_style()
    engine = stage.engine

    def choose(unit: U, index: int, total: int) -> Decision | None | _Quit:
        flag = " (changed)" if unit.changed else ""
        result = button_bar(
            [
                Button("Share", _Menu.SHARE),
                Button("Keep local", HunkClass.LOCAL),
                Button("Skip", None),
                Button("Quit", QUIT),
            ],
            title=f"stage {stage.sub_name} — {engine.noun} {index + 1}/{total}: "
            f"{unit.label}{flag}",
            body=f"{engine.preview(stage.base, stage.live, unit)}\n"
            f"[currently {unit.cls.value}]",
            initial=0 if unit.cls is not HunkClass.LOCAL else 1,
            style=style,
        )
        if result is CANCEL or isinstance(result, _Quit):
            return QUIT  # Esc / Ctrl-C / Quit stops the walk, keeping prior choices
        if result is None:
            return None  # Skip — leave the class unchanged
        if result is HunkClass.LOCAL:
            return Decision(HunkClass.LOCAL)
        return _share_submenu(stage, unit, style)  # Share → how to share

    return choose


#: Share sub-menu wording per unit kind: what a draft rewrites, and the options.
_SHARE_WORDING: Final = {
    UnitKind.LINE: (
        "text",
        "Draft: Claude rewrites this region into a shareable version "
        "(then adopt it locally or keep your local bytes).\n"
        "Verbatim: share your live bytes as-is.",
    ),
    UnitKind.KEY: (
        "value",
        "Draft: Claude rewrites this value into a shareable scalar (same type); "
        "your local value stays — only the shareable scalar is promoted.\n"
        "Verbatim: share your live value as-is.",
    ),
}


def _share_submenu[U: (Hunk, KeyUnit)](
    stage: FileStage[U], unit: U, style: Style
) -> Decision | None:
    """The Share sub-menu: draft (Claude rewrite) / verbatim / skip.

    Returns a SHARED_DRAFTED :class:`Decision` (carrying the draft + adopt flag),
    a plain SHARED Decision (verbatim), or ``None`` (skip / back / draft-cancelled
    — the unit is left unchanged). A hunk's draft rewrites its live region; a key
    unit's draft is bounded to a scalar of the live value's type inside
    :func:`share_draft.draft_key_unit`, which never offers adopt. The draft
    session is constructed per unit and discarded on accept/cancel.
    """
    what, body = _SHARE_WORDING[stage.engine.kind]
    result = button_bar(
        [
            Button("Draft (Claude)", _Menu.DRAFT),
            Button("Verbatim", _Menu.VERBATIM),
            Button("Skip", None),
        ],
        title=f"share {unit.label} — rewrite host-specific {what}?",
        body=body,
        initial=0,
        style=style,
    )
    if result is CANCEL or result is None:
        return None  # back / skip → leave unchanged
    if result is _Menu.VERBATIM:
        return Decision(HunkClass.SHARED)
    if isinstance(unit, Hunk):
        j1, j2 = unit.live_span
        region = b"".join(split_lines(stage.live)[j1:j2])
        outcome = share_draft.draft_hunk(region, display_path=stage.sub_name)
    else:
        fmt = stage.engine.fmt
        assert fmt is not None
        original = su_mod.value_at(stage.live, unit.path, fmt)
        if original is ABSENT:
            # The host deleted this leaf live — there is no scalar to generalise, and
            # an absent type-anchor would re-prompt forever. Leave the unit unchanged.
            return None
        outcome = share_draft.draft_key_unit(
            original, display_path=stage.sub_name, fmt=fmt
        )
    if outcome is CANCEL:
        return None  # draft cancelled → leave the unit unchanged
    return Decision(HunkClass.SHARED_DRAFTED, draft=outcome.draft, adopt=outcome.adopt)


def _adopt_live(stage: FileStage[Hunk], result: WalkResult[Hunk]) -> bytes:
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
        (h for h in result.units if h.ref in result.adopt_refs),
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
    stage: FileStage[Any],
    result: WalkResult[Any],
    *,
    config_dir: Path | None = None,
    config_path: Path | None = None,
    owner_id: UUID | None = None,
) -> None:
    """Apply the walk under ONE profile lock: rewrite live for any Adopt (atomic,
    captured mode), then persist the classifications + drafts.

    The live write and the index record share a single lock span — mirroring how
    install/sync/revert hold the lock across their whole mutating region — so a
    concurrent install/sync cannot land between the write and the record and leave
    a live tree whose bytes no longer match the classifications persisted here.

    Only an engine that supports adopt rewrites live; key units persist their
    classifications against the unchanged live bytes.
    """
    adopting = stage.engine.supports_adopt and bool(result.adopt_refs)
    if (
        stage.ownership is not None
        and stage.ownership.action is FileAction.TRANSFER
        and adopting
    ):
        raise InvariantViolation(
            "transfer ownership before adopting live content; no changes applied"
        )
    identity_dir = (
        resolve_owner_common_dir(config_dir)
        if owner_id is not None and config_dir is not None
        else None
    )
    with operations.transaction(
        resources=owner_id is not None,
        config_identity_dir=identity_dir,
        config_dir=config_dir,
        target_roots=(stage.dst.parent,),
        profile=profile,
        recover=(profile, "stage"),
    ) as mutation_guards:
        if owner_id is not None and config_dir is not None:
            identity_guard = (
                mutation_guards.config_identity if mutation_guards is not None else None
            )
            if identity_guard is None:
                raise InvariantViolation("config owner identity lock was not acquired")
            if (
                read_owner_id_locked(config_dir, identity_guard.directory_fd)
                != owner_id
            ):
                raise InvariantViolation(
                    "config owner identity changed after confirmation; retry stage"
                )
        locked_live = stage.dst.read_bytes()
        if adopting and locked_live != stage.live:
            raise InvariantViolation(
                f"staged file {stage.sub_name!r} changed after it was shown; "
                "run stage again"
            )
        final_live = _adopt_live(stage, result) if adopting else locked_live
        plan = _prepare_persist(
            profile, stage, result, final_live, observed_live=locked_live
        )
        if owner_id is None:
            if final_live != locked_live:
                mode = stat.S_IMODE(stage.dst.stat().st_mode)
                atomicio.atomic_write_bytes(stage.dst, final_live, mode=mode)
            _commit_persist(profile, stage.fid, stage.base, plan)
        else:
            _commit_owned_persist(
                profile,
                stage,
                plan,
                owner_id=owner_id,
                config_dir=config_dir,
                config_path=config_path,
                refresh_claim=(
                    stage.ownership is not None
                    and stage.ownership.action is FileAction.ADOPT
                )
                or bool(result.decided_refs),
                live_payload=final_live if final_live != locked_live else None,
            )


def _store_snapshots(
    profile: str, fid: FileId
) -> tuple[transitions.StateSnapshotEntry, ...]:
    """Capture one reconcile entry in index-last restoration order."""
    return (
        transitions.snapshot_store_state(
            transitions.SnapshotStore.BASE, profile, str(fid)
        ),
        *transitions.reconcile_file_snapshots(profile, str(fid)),
        transitions.snapshot_store_state(
            transitions.SnapshotStore.INDEX, profile, profile
        ),
    )


def _validate_stage_transfer_declaration(
    config_path: Path | None,
    profile: str,
    stage: FileStage[Any],
) -> None:
    """Prove the locked config still declares this exact staged resource."""
    if config_path is None:
        return  # Private unit seams may apply a fully constructed stage directly.
    cfg = load_config(config_path)
    repo_root = config_path.resolve().parent
    resolved = resolve_effective_profile(cfg, profile, repo_root).resolved
    declared: set[tuple[str, ResourceId]] = set()
    for name in resolved.tracked_files:
        tracked = cfg.tracked_files[name]
        source = resolve_src(tracked, repo_root)
        destination = resolve_dst(tracked)
        declared.update(
            (sub_name, observe_file(sub_dst).resource_id)
            for sub_name, _sub_src, sub_dst in expand_tracked_file(
                name, source, destination
            )
        )
    expected = stage.ownership
    if (
        expected is None
        or (
            stage.sub_name,
            expected.observation.resource_id,
        )
        not in declared
    ):
        raise InvariantViolation(
            f"tracked file declaration for {stage.sub_name!r} changed; run stage again"
        )


def _commit_owned_persist(
    profile: str,
    stage: FileStage[Any],
    plan: _PersistPlan,
    *,
    owner_id: UUID,
    config_dir: Path | None,
    config_path: Path | None = None,
    refresh_claim: bool = True,
    live_payload: bytes | None = None,
) -> None:
    """Publish a metadata claim and its reconcile record as one recovery unit."""
    expected = stage.ownership
    if expected is None or expected.action is FileAction.HOLD:
        raise InvariantViolation("stage ownership decision was not usable")
    if expected.action is FileAction.TRANSFER:
        _validate_stage_transfer_declaration(config_path, profile, stage)
    observed = observe_file(stage.dst)
    store = OwnershipStore()
    current = store.read(observed.resource_id)
    locked = decide_file(observed, current, owner_id=owner_id)
    if locked != expected:
        raise InvariantViolation(
            f"ownership inputs for {stage.sub_name!r} changed after confirmation; "
            "run stage again"
        )
    if not refresh_claim:
        _commit_persist(profile, stage.fid, stage.base, plan)
        return
    claim_path = store.claim_path(observed.resource_id)
    paths = (claim_path, *((stage.dst,) if live_payload is not None else ()))
    journal = operations.prepare(
        command="stage",
        profile=profile,
        config_dir=config_dir,
        resources_lock=True,
        paths=paths,
        state_snapshots=_store_snapshots(profile, stage.fid),
    )
    journal = operations.begin_checkpoint(
        journal,
        name="file-adoption",
        kind=operations.CheckpointKind.REVERSIBLE,
        paths=paths,
        restore_state=True,
        restore_transitions=False,
        adapters=(),
    )
    if live_payload is not None:
        mode = stat.S_IMODE(stage.dst.stat().st_mode)
        atomicio.atomic_write_bytes(stage.dst, live_payload, mode=mode)
        observed = observe_file(stage.dst)
        locked = decide_file(observed, current, owner_id=owner_id)
    transfer: transitions.OwnershipTransferDelta | None = None
    if expected.action is FileAction.TRANSFER:
        before = expected.claim
        if before is None:
            raise InvariantViolation("stage transfer lost its current claim")
        after = store.transfer_locked(
            before.resource_id,
            expected_owner=before.owner_id,
            new_owner=owner_id,
            expected_generation=before.generation,
            declaration_refs=(f"tracked_files.{stage.sub_name}",),
        )
        transfer = transitions.OwnershipTransferDelta(before, after)
    else:
        publish_file_claim_locked(
            store,
            locked,
            owner_id=owner_id,
            declaration_ref=f"tracked_files.{stage.sub_name}",
            acquisition=(
                "adopted-external"
                if expected.action is FileAction.ADOPT
                else "observed-local"
            ),
        )
    _commit_persist(profile, stage.fid, stage.base, plan)
    journal = operations.finish_checkpoint(journal)
    if transfer is not None:
        journal = operations.begin_checkpoint(
            journal,
            name="transition-record",
            kind=operations.CheckpointKind.REVERSIBLE,
            paths=(),
            restore_state=False,
            restore_transitions=True,
            adapters=(),
        )
        transitions.write_transition(
            transitions.make_meta(transitions.TransitionCommand.STAGE, profile),
            {},
            {},
            None,
            ownership_transfers=(transfer,),
        )
        journal = operations.finish_checkpoint(journal)
    operations.complete(journal)


def _validate_decisions[U: (Hunk, KeyUnit)](
    stage: FileStage[U],
    result: WalkResult[U],
    observed_live: bytes,
) -> None:
    """Refuse a decision whose unit no longer uniquely has the content shown."""
    if not result.decided_refs:
        return
    observed = stage.engine.extract(stage.base, observed_live)
    for ref in result.decided_refs:
        shown = [unit for unit in stage.units if unit.ref == ref]
        matches = [unit for unit in observed if unit.ref == ref]
        if (
            len(shown) != 1
            or len(matches) != 1
            or shown[0].content_hash != matches[0].content_hash
        ):
            raise InvariantViolation(
                f"staged unit {ref} changed after it was shown; run stage again"
            )


def _prepare_persist[U: (Hunk, KeyUnit)](
    profile: str,
    stage: FileStage[U],
    result: WalkResult[U],
    final_live: bytes,
    *,
    observed_live: bytes | None = None,
) -> _PersistPlan:
    """Build a publication after validating every persisted input.

    The walk read + classified the index at collect time, OUTSIDE the lock; a
    naive whole-list overwrite here would drop any classification a concurrent
    ``sync`` committed in between. Instead, re-read the index (the caller's lock
    still held), re-extract the (post-Adopt) base/live, and overlay ONLY the units
    the host explicitly decided — so a unit the host skipped keeps whatever the
    concurrent writer left, while the host's explicit choices win. base is
    UNCHANGED (sync/install own it).

    The drafts manifest is reconciled to EXACTLY the surviving ``SHARED_DRAFTED``
    set: prior drafts are kept, this walk's are added, and any whose unit demoted
    away is pruned — so a demote never leaves an orphan manifest entry.

    ``decided_refs`` records button actions directly, so choosing the same class
    is a real re-confirmation while Skip/QUIT remain passive. Each decided ref is
    revalidated against the live bytes observed under the caller's lock before it
    can update its fingerprint.
    """
    engine = stage.engine
    _require_current_base(profile, stage.fid, stage.base, display_name=stage.sub_name)
    _validate_decisions(
        stage, result, final_live if observed_live is None else observed_live
    )
    walk_by_ref = {unit.ref: unit for unit in result.units}
    entry = reconcile_store.read_index(profile).files.get(str(stage.fid))
    stored = entry.hunks if entry is not None else []
    stored = index_model.require_unit_kind(stored, engine.kind)
    # Validate the current payload/index/draft quad before publishing a rewrite.
    reconcile_store.verify(profile, stage.fid)
    current = engine.classify(engine.extract(stage.base, final_live), stored)
    merged = [
        replace(
            unit,
            cls=walk_by_ref[unit.ref].cls,
            changed=False,
            confirmed_hash=unit.content_hash,
            draft_hash=walk_by_ref[unit.ref].draft_hash,
        )
        if unit.ref in result.decided_refs
        else unit
        for unit in current
    ]
    pool = {
        **engine.bind_drafts(current, reconcile_store.read_drafts(profile, stage.fid)),
        **result.drafts,
    }
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
    if engine.kind is UnitKind.KEY:
        # Only a key publication can still be unbuildable here: conflicting
        # parent/child intents, or a draft that is not a scalar of its key's type.
        engine.reconstruct(stage.base, final_live, merged, drafts)
    return _PersistPlan(
        local=final_live,
        staged=(entry.staged if entry is not None else False)
        or bool(result.decided_refs),
        hunks=engine.serialize(merged, stage.src),
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


def _refuse_generated_stage_target(
    cfg: Config, resolved: ResolvedProfile, file: str
) -> None:
    """Refuse staging when ``file`` names generated one-way output.

    A tracked file whose ID is ``file`` wins, so another file's live name equal to
    it does not make the selector one-way output.
    """
    if any(
        file == name
        and cfg.tracked_files[name].generated is None
        and cfg.tracked_files[name].tree is None
        for name in resolved.tracked_files
    ):
        return
    matched = any(
        (
            cfg.tracked_files[name].generated is not None
            or cfg.tracked_files[name].tree is not None
        )
        and file
        in {
            name,
            str(resolve_dst(cfg.tracked_files[name])),
            resolve_dst(cfg.tracked_files[name]).name,
        }
        for name in resolved.tracked_files
    )
    if matched:
        raise typer.BadParameter(
            f"{file!r} is one-way output and cannot be staged; edit its tracked "
            "template, source tree, or host-input declaration"
        )


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

    _refuse_generated_stage_target(cfg, resolved, file)

    staged: list[FileStage[Any]] = [
        *collect_stages(cfg, resolved, repo_root, profile, only=file),
        *collect_structured_stages(cfg, resolved, repo_root, profile, only=file),
    ]
    if not staged:
        typer.secho(
            f"{file}: nothing to stage — no local changes over a recorded base "
            f"(run `setforge install --profile={profile}` first if it is new)",
            err=True,
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=0)

    console = make_console()
    for item in staged:
        if not item.units:
            continue
        owner_id = _confirm_file_ownership(item, repo_root)
        result = walk(item.units, _interactive_choice(item))
        _apply(
            profile,
            item,
            result,
            config_dir=repo_root,
            config_path=config,
            owner_id=owner_id,
        )
        tally = counts(result.units)
        drafted = tally[HunkClass.SHARED_DRAFTED]
        drafted_note = f"  {drafted} drafted" if drafted else ""
        console.print(
            f"{item.sub_name}: "
            f"{tally[HunkClass.SHARED]} shared{drafted_note}  "
            f"{tally[HunkClass.LOCAL]} local  "
            f"{tally[HunkClass.PENDING]} pending"
        )


def _confirm_file_ownership(stage: FileStage[Any], repo_root: Path) -> UUID | None:
    """Confirm a missing container claim separately from unit decisions."""
    decision = stage.ownership
    if decision is None:
        raise InvariantViolation("stage did not collect a file ownership decision")
    if decision.action is FileAction.HOLD:
        raise InvariantViolation(f"cannot stage {stage.sub_name!r}: {decision.detail}")
    if decision.action is FileAction.TRANSFER:
        if decision.claim is None:
            raise InvariantViolation("file ownership transfer lost its source claim")
        try:
            receiver_owner = read_owner_id(repo_root)
        except OwnershipError as exc:
            raise InvariantViolation(
                "file ownership transfer requires a Git-backed config identity"
            ) from exc
        typer.echo(
            "file ownership transfer: "
            f"{decision.observation.resource_id.canonical()} "
            f"{decision.claim.owner_id} -> {receiver_owner} "
            f"({decision.observation.locator})"
        )
        if not typer.confirm(
            "Transfer this exact claim without changing its bytes?", default=False
        ):
            raise InvariantViolation(
                "file ownership transfer declined; no changes applied"
            )
        return receiver_owner
    if decision.action is FileAction.ADOPT and not typer.confirm(
        f"Manage existing tracked file {stage.sub_name!r} without changing its bytes?",
        default=False,
    ):
        raise InvariantViolation("file ownership adoption declined; no changes applied")
    try:
        return load_or_create_owner_id(repo_root)
    except OwnershipError:
        typer.secho(
            "warning: staged file remains without durable ownership because "
            "the configuration is not Git-backed",
            err=True,
            fg=typer.colors.YELLOW,
        )
        return None


def summarize_stages(
    stages: Sequence[FileStage[Any]],
    structured: Sequence[FileStage[Any]] = (),
) -> tuple[StageSummary, ...]:
    """Summarize staging units without rendering or mutating their stores."""

    def summarize(
        name: str,
        units: list[Hunk] | list[KeyUnit],
        participating: bool,
        ownership: FileDecision | None,
    ) -> StageSummary:
        tally = Counter(unit.cls for unit in units)
        reconfirm = sum(unit.changed and unit.cls is HunkClass.SHARED for unit in units)
        promotable = sum(
            not unit.changed and unit.cls is HunkClass.SHARED for unit in units
        )
        pending = tally[HunkClass.PENDING]
        blockers: list[str] = []
        if reconfirm:
            blockers.append(
                f"{reconfirm} shared unit(s) changed: run `setforge stage {name}` "
                "to re-confirm"
            )
        if pending:
            blockers.append(
                f"{pending} pending unit(s): run `setforge stage {name}` to classify"
            )
        ownership_status = (
            ownership.action.value if ownership is not None else "unknown"
        )
        if ownership is not None and ownership.action in {
            FileAction.ADOPT,
            FileAction.HOLD,
        }:
            blockers.append(f"container ownership: {ownership.detail}")
        return StageSummary(
            name=name,
            participating=participating,
            # Schema v1 compatibility: ``shared`` remains the total durable
            # SHARED classification count.  The additive fields below explain
            # which of those rows are currently promotable versus blocked on
            # explicit re-confirmation.
            shared=tally[HunkClass.SHARED],
            shared_promotable=promotable,
            drafted=tally[HunkClass.SHARED_DRAFTED],
            reconfirm_required=reconfirm,
            local=tally[HunkClass.LOCAL],
            pending=pending,
            blockers=tuple(blockers),
            ownership=ownership_status,
        )

    return tuple(
        summarize(stage.sub_name, stage.units, stage.participating, stage.ownership)
        for stage in (*stages, *structured)
    )


def _render_list(
    ctx_obj: OutputContext | None,
    stages: list[FileStage[Hunk]],
    struct: list[FileStage[KeyUnit]] | None = None,
) -> None:
    """Render durable participation and capture-actionability diagnostics."""
    summaries = summarize_stages(stages, struct or ())
    data = [summary.to_dict() for summary in summaries]

    def _human() -> None:
        console = make_console()
        if not data:
            console.print("no staged-eligible files with local changes")
            return
        for row in data:
            console.print(
                f"{row['name']}: "
                f"participating={str(row['participating']).lower()}  "
                f"{row['shared_promotable']} shared-promotable  "
                f"{row['drafted']} drafted  {row['reconfirm_required']} "
                f"reconfirm-required  {row['local']} local  {row['pending']} pending"
                f"  ownership={row['ownership']}"
            )
            for blocker in row["blockers"]:
                console.print(f"  blocked: {blocker}")

    render(ctx_obj, "stage", data, human_fn=_human)
