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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from setforge import atomicio
from setforge.compare import expand_tracked_file, resolve_dst, resolve_src
from setforge.config import Config, ResolvedProfile
from setforge.errors import InvariantViolation, StructuredParseError
from setforge.reconcile import hunks as reconcile_hunks
from setforge.reconcile import index_model
from setforge.reconcile import store as reconcile_store
from setforge.reconcile import structured_units as su_mod
from setforge.reconcile.types import HunkClass, UnitKind, UnitRef, content_sha, file_id


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


def _preflight_staged_file(
    profile: str,
    sub_name: str,
    dst: Path,
    fmt: su_mod.StructuredFormat | None,
) -> bool:
    """Validate one participating file without writing; false when opted out."""
    fid = file_id(sub_name)
    entry = reconcile_store.read_index(profile).files.get(str(fid))
    if entry is None or not entry.staged:
        return False
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
    drafts = reconcile_store.read_drafts(profile, fid)
    if fmt is None:
        index_model.require_unit_kind(entry.hunks, UnitKind.LINE)
        _require_utf8(sub_name, base, live)
        hunks = reconcile_hunks.classify(
            reconcile_hunks.extract_hunks(base, live), entry.hunks
        )
        bound = reconcile_hunks.bind_drafts(hunks, drafts)
        reconcile_hunks.reconstruct(base, live, hunks, bound)
        return True
    index_model.require_unit_kind(entry.hunks, UnitKind.KEY)
    try:
        fresh = su_mod.extract_structured_units(base, live, fmt)
    except StructuredParseError as err:
        raise InvariantViolation(
            f"staged file {sub_name!r} cannot be parsed as {fmt.value}"
        ) from err
    units = su_mod.classify_structured(fresh, entry.hunks, fmt)
    su_mod.reconstruct_structured(base, live, units, drafts, fmt)
    return True


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


def plan_capture(  # noqa: C901 - one decision per route
    config: Config,
    profile_name: str,
    repo_root: Path,
    *,
    resolved: ResolvedProfile,
    ownership_authorized: Mapping[str, bool],
    auto: "CaptureAuto | None" = None,
) -> tuple[CaptureItem, ...]:
    """Decide every tracked write and store record of a capture; write nothing."""
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
            fmt = su_mod.structured_format(sub_dst)
            if not sub_dst.exists():
                _preflight_staged_file(profile_name, sub_name, sub_dst, fmt)
                items.append(
                    _item(sub_name, sub_src, sub_dst, None, reason="live missing")
                )
                continue
            fid = file_id(sub_name)
            entry = reconcile_store.read_index(profile_name).files.get(str(fid))
            if entry is not None and entry.staged:
                if not ownership_authorized[sub_name]:
                    raise InvariantViolation(
                        f"staged file {sub_name!r} has no current container "
                        f"ownership claim; run `setforge stage {sub_name}` to adopt it"
                    )
                _preflight_staged_file(profile_name, sub_name, sub_dst, fmt)
                base = reconcile_store.read_base(profile_name, fid)
                assert base is not None
                live = sub_dst.read_bytes()
                stored_drafts = reconcile_store.read_drafts(profile_name, fid)
                warnings: list[str] = []
                if fmt is None:
                    stored = index_model.require_unit_kind(entry.hunks, UnitKind.LINE)
                    line_units = reconcile_hunks.classify(
                        reconcile_hunks.extract_hunks(base, live), stored
                    )
                    drafts = reconcile_hunks.bind_drafts(line_units, stored_drafts)
                    proposed = reconcile_hunks.reconstruct(
                        base, live, line_units, drafts
                    )
                    _require_utf8(sub_name, proposed)
                    rows = reconcile_hunks.serialize(
                        line_units,
                        allow_relocation=sub_src.suffix.lower() in {".md", ".markdown"},
                    )
                    if any(unit.cls is HunkClass.PENDING for unit in line_units):
                        warnings.append(
                            f"{sub_src.name}: unstaged local changes kept host-only — "
                            f"run `setforge stage {sub_src.name}` to share any of them"
                        )
                    if any(
                        unit.changed and unit.cls is HunkClass.SHARED
                        for unit in line_units
                    ):
                        warnings.append(
                            f"{sub_src.name}: a previously-staged hunk changed and "
                            "was kept host-only — re-run "
                            f"`setforge stage {sub_src.name}` to re-confirm it"
                        )
                else:
                    stored = index_model.require_unit_kind(entry.hunks, UnitKind.KEY)
                    key_units = su_mod.classify_structured(
                        su_mod.extract_structured_units(base, live, fmt), stored, fmt
                    )
                    drafts = su_mod.bind_structured_drafts(key_units, stored_drafts)
                    proposed = su_mod.reconstruct_structured(
                        base, live, key_units, drafts, fmt
                    )
                    rows = su_mod.serialize_structured(key_units)
                    if any(unit.cls is HunkClass.PENDING for unit in key_units):
                        warnings.append(
                            f"{sub_src.name}: unstaged local changes kept host-only — "
                            f"run `setforge stage {sub_src.name}` to share any of them"
                        )
                    if any(
                        unit.changed and unit.cls is HunkClass.SHARED
                        for unit in key_units
                    ):
                        warnings.append(
                            f"{sub_src.name}: a previously-staged key changed and was "
                            "kept host-only — re-run "
                            f"`setforge stage {sub_src.name}` to re-confirm it"
                        )
                items.append(
                    _item(
                        sub_name,
                        sub_src,
                        sub_dst,
                        proposed,
                        warnings=tuple(warnings),
                        entry=entry,
                        record=StoreRecord(
                            base=base, local=live, hunks=rows, drafts=drafts
                        ),
                        store_update=(
                            not entry.present
                            or entry.local_hash != content_sha(live)
                            or entry.hunks != rows
                            or stored_drafts != drafts
                        ),
                    )
                )
                continue
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
    ``mkstemp`` default ride in via ``os.replace`` — silently demoting an
    executable hook (0o755) or a 0o644 config in the shared config repo and
    propagating that mode cross-host on the next deploy. On a fresh tracked
    file (``src`` absent) fall back to 0o644, the conventional non-executable
    default, rather than the 0600 mkstemp leftover.
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
