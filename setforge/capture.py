"""Capture: live → tracked.

The inverse of ``deploy.write_resolved_deploy``. Reads each profile tracked_file's
``dst`` (the live copy) and writes it back to ``src`` (the tracked copy). A
staged file promotes only its SHARED units; host-only units stay out of
``tracked/``.

Capture is no longer a silent absorb: the CLI shows the plan from
:func:`plan_capture`, resolves drift via ``--auto={use-live, keep-tracked}``,
and hands the confirmed plan to :func:`apply_capture`, so ``keep-tracked``
never reaches this module's writer.
"""

import stat
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from setforge import atomicio
from setforge.compare import expand_tracked_file, resolve_dst, resolve_src
from setforge.config import Config, ResolvedProfile
from setforge.errors import InvariantViolation, StructuredParseError
from setforge.reconcile import index_model
from setforge.reconcile import store as reconcile_store
from setforge.reconcile import structured_units as su_mod
from setforge.reconcile.hunks import Hunk
from setforge.reconcile.structured_units import KeyUnit
from setforge.reconcile.types import HunkClass, UnitRef, content_sha, file_id
from setforge.reconcile.unit_engine import engine_for


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


def _require_utf8(sub_name: str, *contents: bytes) -> None:
    try:
        for content in contents:
            content.decode("utf-8")
    except UnicodeDecodeError as err:
        raise InvariantViolation(
            f"staged file {sub_name!r} is not valid UTF-8 text"
        ) from err


@dataclass(frozen=True, slots=True)
class CaptureResult:
    name: str
    action: CaptureAction
    reason: str = ""
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StoreRecord:
    """The reconcile-store row a staged capture writes; ``base`` never advances."""

    base: bytes
    local: bytes
    hunks: list[dict[str, object]]
    drafts: dict[UnitRef, bytes]


@dataclass(frozen=True, slots=True)
class CaptureItem:
    """One file of a capture plan: what was read and exactly what is written.

    ``proposed`` is the tracked content to write (``None`` skips the file) and
    ``record`` the store row a staged file gets. ``tracked`` and ``entry`` are
    the tracked bytes and index row the decision was made from, so two plans
    compare equal only when nothing they depend on changed.
    """

    name: str
    src: Path
    dst: Path
    action: CaptureAction
    reason: str = ""
    warnings: tuple[str, ...] = ()
    proposed: bytes | None = None
    tracked: bytes | None = None
    entry: index_model.FileEntry | None = None
    record: StoreRecord | None = None
    store_update: bool = False


def _item(
    name: str,
    src: Path,
    dst: Path,
    proposed: bytes | None,
    *,
    reason: str = "",
    warnings: tuple[str, ...] = (),
    entry: index_model.FileEntry | None = None,
    record: StoreRecord | None = None,
    store_update: bool = False,
) -> CaptureItem:
    tracked = src.read_bytes() if src.exists() else None
    if proposed is None:
        action = CaptureAction.SKIPPED
    else:
        action = CaptureAction.NOOP if tracked == proposed else CaptureAction.UPDATED
    return CaptureItem(
        name=name,
        src=src,
        dst=dst,
        action=action,
        reason=reason,
        warnings=warnings,
        proposed=proposed,
        tracked=tracked,
        entry=entry,
        record=record,
        store_update=store_update,
    )


def _held_back_warnings(
    name: str,
    units: Sequence[Hunk] | Sequence[KeyUnit],
    noun: str,
) -> tuple[str, ...]:
    warnings: list[str] = []
    if any(unit.cls is HunkClass.PENDING for unit in units):
        warnings.append(
            f"{name}: unstaged local changes kept host-only — "
            f"run `setforge stage {name}` to share any of them"
        )
    # A classified unit whose content drifted is held at base, not promoted:
    # say so, so a shared unit that just left tracked/ is not a surprise.
    if any(unit.changed and unit.cls is HunkClass.SHARED for unit in units):
        warnings.append(
            f"{name}: a previously-staged {noun} changed and was kept host-only — "
            f"re-run `setforge stage {name}` to re-confirm it"
        )
    return tuple(warnings)


def _plan_staged(
    profile: str, sub_name: str, src: Path, dst: Path, entry: index_model.FileEntry
) -> CaptureItem:
    """Plan one staged file: only its SHARED units reach ``tracked/``.

    The tracked content is rebuilt from the recorded base plus the units still
    shared (never patched), so a demoted unit leaves ``tracked/`` by itself. A
    staged file fails closed when its base, live bytes, encoding, routing or
    identities cannot be reconciled; it never falls back to the whole-file
    writeback.
    """
    fid = file_id(sub_name)
    base = reconcile_store.read_base(profile, fid)
    if base is None:
        raise InvariantViolation(
            f"staged file {sub_name!r} has no recorded reconciliation base"
        )
    if not dst.is_file():
        raise InvariantViolation(f"staged file {sub_name!r} has no live file")
    try:
        live = dst.read_bytes()
    except OSError as err:
        raise InvariantViolation(
            f"staged file {sub_name!r} live bytes cannot be read: {err}"
        ) from err
    stored_drafts = reconcile_store.read_drafts(profile, fid)
    engine = engine_for(dst, base, entry.hunks)
    stored = index_model.require_unit_kind(entry.hunks, engine.kind)
    if engine.fmt is None:
        _require_utf8(sub_name, base, live)
    try:
        fresh = engine.extract(base, live)
    except StructuredParseError as err:  # raised by a structured format only
        raise InvariantViolation(
            f"staged file {sub_name!r} cannot be parsed as {engine.fmt}"
        ) from err
    units = engine.classify(fresh, stored)
    # A migrated legacy draft key is bound to its current unit here, and the
    # bound manifest is the one recorded, so the upgrade lands with the index row.
    drafts = engine.bind_drafts(units, stored_drafts)
    proposed = engine.reconstruct(base, live, units, drafts)
    _require_utf8(sub_name, proposed)
    rows = engine.serialize(units, src)
    warnings = _held_back_warnings(src.name, units, engine.noun)
    return _item(
        sub_name,
        src,
        dst,
        proposed,
        warnings=warnings,
        entry=entry,
        record=StoreRecord(base=base, local=live, hunks=rows, drafts=drafts),
        store_update=(
            not entry.present
            or entry.local_hash != content_sha(live)
            or entry.hunks != rows
            or stored_drafts != drafts
        ),
    )


def plan_capture(
    config: Config,
    profile_name: str,
    repo_root: Path,
    *,
    resolved: ResolvedProfile,
    ownership_authorized: Mapping[str, bool],
    auto: "CaptureAuto | None" = None,
) -> tuple[CaptureItem, ...]:
    """Decide every tracked write and store record of a capture; write nothing.

    ``ownership_authorized`` maps each tracked sub-file to whether this checkout
    holds its container claim; a staged file without it is refused. Every file
    is validated here, so a plan that exists has no invalid participant.
    """
    items: list[CaptureItem] = []
    for name in resolved.tracked_files:
        tracked_file = config.tracked_files[name]
        if tracked_file.tree is not None:
            raise InvariantViolation(
                f"managed tree {name!r} is one-way output and cannot be captured; "
                "edit its tracked source tree"
            )
        if tracked_file.generated is not None:
            raise InvariantViolation(
                f"generated tracked file {name!r} is one-way output and cannot "
                "be captured; edit its tracked template or host-input declaration"
            )
        src = resolve_src(tracked_file, repo_root)
        dst = resolve_dst(tracked_file)
        for sub_name, sub_src, sub_dst in expand_tracked_file(name, src, dst):
            entry = reconcile_store.read_index(profile_name).files.get(
                str(file_id(sub_name))
            )
            if entry is not None and entry.staged:
                if sub_dst.exists() and not ownership_authorized[sub_name]:
                    raise InvariantViolation(
                        f"staged file {sub_name!r} has no current container "
                        f"ownership claim; run `setforge stage {sub_name}` to adopt it"
                    )
                items.append(
                    _plan_staged(profile_name, sub_name, sub_src, sub_dst, entry)
                )
            elif not sub_dst.exists():
                items.append(
                    _item(sub_name, sub_src, sub_dst, None, reason="live missing")
                )
            else:
                if auto is not CaptureAuto.KEEP_TRACKED:
                    _refuse_unparseable_structured(sub_name, sub_src, sub_dst)
                items.append(
                    _item(sub_name, sub_src, sub_dst, sub_dst.read_bytes(), entry=entry)
                )
    return tuple(items)


def _refuse_unparseable_structured(sub_name: str, src: Path, dst: Path) -> None:
    """Refuse a live JSON/YAML that does not parse when the tracked one does."""
    fmt = su_mod.structured_format(dst)
    if fmt is None or not src.is_file() or not dst.is_file():
        return
    try:
        su_mod._load_model(src.read_bytes(), fmt)
    except StructuredParseError:
        return
    try:
        su_mod._load_model(dst.read_bytes(), fmt)
    except StructuredParseError as err:
        raise InvariantViolation(
            f"live file {dst} ({sub_name!r}) is not parseable {fmt.value}: {err}; "
            "refusing to overwrite the tracked copy. Fix the live file and re-run sync"
        ) from err


def _content_bytes(content: str | bytes) -> bytes:
    return content if isinstance(content, bytes) else content.encode("utf-8")


def _write_if_changed(src: Path, content: str | bytes) -> CaptureResult:
    """Write ``content`` to ``src`` unless it already matches; return action.

    Preserves the tracked file's existing permission bits across the atomic
    rewrite. ``atomic_write_text`` with no ``mode`` would let the 0600
    temp-file default ride in via ``os.replace`` — silently demoting an
    executable hook (0o755) or a 0o644 config in the shared config repo and
    propagating that mode cross-host on the next deploy. On a fresh tracked
    file (``src`` absent) fall back to 0o644, the conventional non-executable
    default, rather than the temp file's 0600.
    """
    src.parent.mkdir(parents=True, exist_ok=True)
    data = _content_bytes(content)
    if src.exists() and src.read_bytes() == data:
        return CaptureResult(name=src.name, action=CaptureAction.NOOP)
    mode = stat.S_IMODE(src.stat().st_mode) if src.exists() else 0o644
    atomicio.atomic_write_bytes(src, data, mode=mode)
    return CaptureResult(name=src.name, action=CaptureAction.UPDATED)


def apply_capture(
    profile_name: str, plan: Iterable[CaptureItem]
) -> list[CaptureResult]:
    """Write exactly what ``plan`` holds, in order; nothing is re-derived.

    Must run inside the lock the plan was made under. There is no rollback: a
    failure leaves the earlier items written. A staged file's store row keeps
    ``base`` as recorded (it advances only on ``install``) and records the full
    live bytes, whether or not the tracked bytes changed.
    """
    results: list[CaptureResult] = []
    for item in plan:
        action = item.action
        if item.proposed is not None:
            action = _write_if_changed(item.src, item.proposed).action
        if item.record is not None:
            # INV-8: the planned bytes are base plus exactly the promoted units;
            # hold the file now on disk to them before the store says so.
            if item.src.read_bytes() != item.proposed:
                raise InvariantViolation(
                    f"INV-8: tracked file {item.src} does not hold exactly the "
                    "planned shared content after the write"
                )
            reconcile_store.record(
                profile_name,
                file_id(item.name),
                base=item.record.base,
                local=item.record.local,
                staged=True,
                hunks=item.record.hunks,
                drafts=item.record.drafts,
            )
        results.append(
            CaptureResult(
                name=item.name,
                action=action,
                reason=item.reason,
                warnings=item.warnings,
            )
        )
    return results
