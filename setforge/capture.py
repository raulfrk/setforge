"""Capture: live → tracked.

The inverse of ``deploy.write_resolved_deploy``. Reads each profile tracked_file's
``dst`` (the live copy) and writes a host-state-stripped version back to
``src`` (the tracked copy): legacy ``host_local_sections`` marker pairs that
``install`` injected are name-scoped stripped (markers and body both removed).

Capture is no longer a silent absorb. When a tracked_file carries drift
between tracked and live, capture resolves it via
``--auto={use-live, keep-tracked}`` (``use-live`` absorbs the drift into
tracked, ``keep-tracked`` refuses it); the per-tracked_file writeback then
applies the host-state strip above.
"""

import json
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from rich.console import Console

from setforge import (
    atomicio,
)
from setforge import (
    user_section_markers as sections,
)
from setforge.compare import expand_tracked_file, resolve_dst, resolve_src
from setforge.config import Config, ResolvedProfile, resolve_profile
from setforge.errors import InvariantViolation, StructuredParseError
from setforge.reconcile import hunks as reconcile_hunks
from setforge.reconcile import index_model
from setforge.reconcile import store as reconcile_store
from setforge.reconcile import structured_units as su_mod
from setforge.reconcile.types import HunkClass, UnitKind, UnitRef, content_sha, file_id
from setforge.source import HostLocalSection, HostLocalSectionName


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
        try:
            base.decode("utf-8")
            live.decode("utf-8")
        except UnicodeDecodeError as err:
            raise InvariantViolation(
                f"staged file {sub_name!r} is not valid UTF-8 text"
            ) from err
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
    units = su_mod.classify_structured(fresh, entry.hunks)
    su_mod.reconstruct_structured(base, live, units, drafts, fmt)
    return True


@dataclass(frozen=True, slots=True)
class CaptureResult:
    name: str
    action: CaptureAction
    reason: str = ""
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CapturePreview:
    """Read-only projection of one file's exact capture outcome."""

    name: str
    src: Path
    dst: Path
    action: CaptureAction
    reason: str = ""
    warnings: tuple[str, ...] = ()
    proposed_hash: str | None = None
    tracked_hash: str | None = None
    live_hash: str | None = None
    base_hash: str | None = None
    index_hash: str | None = None
    drafts: tuple[tuple[str, str, str], ...] = ()
    route: str = "whole-file"
    store_update: bool = False


def _preview_result(
    name: str,
    src: Path,
    dst: Path,
    proposed: bytes | None,
    *,
    reason: str = "",
    warnings: tuple[str, ...] = (),
    live: bytes | None = None,
    base: bytes | None = None,
    entry: object | None = None,
    drafts: Mapping[UnitRef, bytes] | None = None,
    route: str = "whole-file",
    store_update: bool = False,
) -> CapturePreview:
    current = src.read_bytes() if src.exists() else None
    if proposed is None:
        action = CaptureAction.SKIPPED
    else:
        action = CaptureAction.NOOP if current == proposed else CaptureAction.UPDATED
    return CapturePreview(
        name=name,
        src=src,
        dst=dst,
        action=action,
        reason=reason,
        warnings=warnings,
        proposed_hash=content_sha(proposed) if proposed is not None else None,
        tracked_hash=content_sha(current) if current is not None else None,
        live_hash=content_sha(live) if live is not None else None,
        base_hash=content_sha(base) if base is not None else None,
        index_hash=(
            content_sha(
                json.dumps(
                    {
                        "present": getattr(entry, "present", None),
                        "local_hash": getattr(entry, "local_hash", None),
                        "staged": getattr(entry, "staged", None),
                        "hunks": getattr(entry, "hunks", None),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            if entry is not None
            else None
        ),
        drafts=tuple(
            sorted(
                (
                    ref.kind.value,
                    ref.identity,
                    content_sha(data),
                )
                for ref, data in (drafts or {}).items()
            )
        ),
        route=route,
        store_update=store_update,
    )


def preview_capture_profile(
    config: Config,
    profile_name: str,
    repo_root: Path,
    *,
    resolved: ResolvedProfile,
) -> tuple[CapturePreview, ...]:
    """Return the exact per-file capture projection without writing anything."""
    previews: list[CapturePreview] = []
    for name in resolved.tracked_files:
        tracked_file = config.tracked_files[name]
        src = resolve_src(tracked_file, repo_root)
        dst = resolve_dst(tracked_file)
        for sub_name, sub_src, sub_dst in expand_tracked_file(name, src, dst):
            fmt = su_mod.structured_format(sub_dst)
            if not sub_dst.exists():
                previews.append(
                    _preview_result(
                        sub_name,
                        sub_src,
                        sub_dst,
                        None,
                        reason="live missing",
                        route=fmt.value if fmt is not None else "whole-file",
                    )
                )
                continue
            fid = file_id(sub_name)
            entry = reconcile_store.read_index(profile_name).files.get(str(fid))
            if entry is not None and entry.staged:
                _preflight_staged_file(profile_name, sub_name, sub_dst, fmt)
                base = reconcile_store.read_base(profile_name, fid)
                assert base is not None
                live = sub_dst.read_bytes()
                stored_drafts = reconcile_store.read_drafts(profile_name, fid)
                drafts = stored_drafts
                warnings: list[str] = []
                if fmt is None:
                    stored = index_model.require_unit_kind(entry.hunks, UnitKind.LINE)
                    line_units = reconcile_hunks.classify(
                        reconcile_hunks.extract_hunks(base, live), stored
                    )
                    drafts = reconcile_hunks.bind_drafts(line_units, drafts)
                    proposed = reconcile_hunks.reconstruct(
                        base, live, line_units, drafts
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
                        su_mod.extract_structured_units(base, live, fmt), stored
                    )
                    proposed = su_mod.reconstruct_structured(
                        base, live, key_units, drafts, fmt
                    )
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
                previews.append(
                    _preview_result(
                        sub_name,
                        sub_src,
                        sub_dst,
                        proposed,
                        warnings=tuple(warnings),
                        live=live,
                        base=base,
                        entry=entry,
                        drafts=drafts,
                        route=fmt.value if fmt is not None else "line",
                        store_update=(
                            not entry.present
                            or entry.local_hash != content_sha(live)
                            or entry.hunks
                            != (
                                reconcile_hunks.serialize(line_units)
                                if fmt is None
                                else su_mod.serialize_structured(key_units)
                            )
                            or stored_drafts != drafts
                        ),
                    )
                )
                continue
            proposed = sub_dst.read_bytes()
            previews.append(
                _preview_result(
                    sub_name,
                    sub_src,
                    sub_dst,
                    proposed,
                    live=proposed,
                    entry=entry,
                    route=fmt.value if fmt is not None else "whole-file",
                )
            )
    return tuple(previews)


def capture_tracked_file(
    src: Path,
    dst: Path,
    *,
    host_local_section_names: frozenset[str] = frozenset(),
    auto: "CaptureAuto | None" = None,
) -> CaptureResult:
    """Write ``dst`` (live) back to ``src`` (tracked) for a disposition=None file.

    A ``disposition=None`` tracked_file deploys tracked verbatim, so capture is
    a wholesale live → tracked writeback — EXCEPT host-local content, which must
    never leak into the shared tracked source: legacy ``host_local_sections``
    marker pairs injected by ``install`` are name-scoped stripped via
    :func:`sections.strip_host_local_sections`.

    Returns :class:`CaptureResult.NOOP` when the resulting tracked content is
    byte-identical to the existing tracked file, or SKIPPED when live is absent.
    """
    if not dst.exists():
        return CaptureResult(
            name=src.name, action=CaptureAction.SKIPPED, reason="live missing"
        )

    content = dst.read_text(encoding="utf-8")
    # Drop legacy host-local marker pairs + bodies injected by install (via
    # local.yaml host_local_sections) before the writeback. Name-scoped to
    # ``host_local_section_names`` so a host-local marker the user authored
    # directly in tracked passes through unchanged.
    if host_local_section_names:
        content = sections.strip_host_local_sections(
            content, names=host_local_section_names, allow_legacy=True
        )
    if _keep_tracked_refuses(auto, src, content):
        return CaptureResult(
            name=src.name, action=CaptureAction.SKIPPED, reason="keep-tracked"
        )
    return _write_if_changed(src, content)


def _keep_tracked_refuses(auto: "CaptureAuto | None", src: Path, content: str) -> bool:
    """Return whether ``--auto=keep-tracked`` should refuse this writeback.

    ``keep-tracked`` is the drift-refusal resolution: when the would-be
    capture content diverges from the existing tracked source, the tracked
    bytes (and, for SHARED, the stored base) must be left untouched. A
    no-drift writeback (tracked already equals ``content``) is a NOOP either
    way, so only an actual divergence is refused.
    """
    if auto is not CaptureAuto.KEEP_TRACKED:
        return False
    if not src.exists():
        return False
    return src.read_text(encoding="utf-8") != content


def _write_if_changed(src: Path, content: str) -> CaptureResult:
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
    if src.exists() and src.read_text(encoding="utf-8") == content:
        return CaptureResult(name=src.name, action=CaptureAction.NOOP)
    mode = stat.S_IMODE(src.stat().st_mode) if src.exists() else 0o644
    atomicio.atomic_write_text(src, content, mode=mode)
    return CaptureResult(name=src.name, action=CaptureAction.UPDATED)


def _capture_staged_plain(
    profile: str,
    sub_name: str,
    src: Path,
    dst: Path,
    *,
    auto: "CaptureAuto | None",
) -> CaptureResult | None:
    """A5 staged capture for a plain reconcile file (RFC §9.3).

    Promotes ONLY the hunks the host classified SHARED into ``tracked/`` and
    keeps LOCAL/PENDING host-only. The tracked content is **reconstructed** from
    ``base`` + the still-SHARED hunks (never patched), so a SHARED→LOCAL demote
    un-captures automatically. The stored ``base`` is **left unchanged** — it
    advances only on ``install`` when new upstream is fetched, so a SHARED hunk
    stays a base↔live delta and therefore stays demotable; ``local`` records the
    full live bytes, so the store still round-trips live verbatim (INV-2).

    Must run inside the caller's ``profile_lock`` (like every capture helper).
    Returns ``None`` only when the file has not explicitly opted into staged
    reconciliation. A participating file fails closed when its recorded base,
    live bytes, encoding, routing, or identities cannot be reconciled; it never
    falls through to the verbatim writeback.
    """
    if not _preflight_staged_file(profile, sub_name, dst, None):
        return None
    fid = file_id(sub_name)
    base = reconcile_store.read_base(profile, fid)
    assert base is not None  # preflight established the participating store shape
    entry = reconcile_store.read_index(profile).files.get(str(fid))
    assert entry is not None
    assert entry.staged
    stored = index_model.require_unit_kind(entry.hunks, UnitKind.LINE)
    live = dst.read_bytes()
    hunks = reconcile_hunks.classify(reconcile_hunks.extract_hunks(base, live), stored)
    # Bind any migrated v1 draft key to its unique fresh v2 unit before splicing
    # the shareable bytes in place of host-specific live bytes. Capture passes the
    # bound manifest to record() so a legacy payload is upgraded before the v2
    # index row lands (index-last), preserving the blessed divergence atomically.
    drafts = reconcile_hunks.bind_drafts(
        hunks, reconcile_store.read_drafts(profile, fid)
    )
    new_text = reconcile_hunks.reconstruct(base, live, hunks, drafts).decode("utf-8")
    if _keep_tracked_refuses(auto, src, new_text):
        return CaptureResult(
            name=sub_name, action=CaptureAction.SKIPPED, reason="keep-tracked"
        )
    result = _write_if_changed(src, new_text)
    # INV-8: the bytes now ON DISK in tracked/ must be exactly the shared/drafted
    # set (base + promoted SHARED live + spliced SHARED_DRAFTED drafts). Read back
    # the post-write content so this verifies the actual write, not the value we
    # just computed.
    reconcile_hunks.assert_stage_fidelity(base, live, src.read_bytes(), hunks, drafts)
    # Persist full live + classifications; base is UNCHANGED on sync (it advances
    # only on install). Passing the bound map atomically upgrades a v1 draft key
    # alongside the v2 hunk row. Runs
    # unconditionally w.r.t. the tracked NOOP so a classification-only change
    # (e.g. PENDING→LOCAL) still persists.
    reconcile_store.record(
        profile,
        fid,
        base=base,
        local=live,
        staged=True,
        hunks=reconcile_hunks.serialize(hunks),
        drafts=drafts,
    )
    warnings: list[str] = []
    if any(hunk.cls is HunkClass.PENDING for hunk in hunks):
        warnings.append(
            f"{src.name}: unstaged local changes kept host-only — run "
            f"`setforge stage {src.name}` to share any of them"
        )
    # A previously-classified hunk whose content drifted is held at base (NOT
    # auto-promoted, NOT silently kept) — tell the host so a shared hunk that
    # just dropped out of tracked/ is not a surprise.
    if any(hunk.changed and hunk.cls is HunkClass.SHARED for hunk in hunks):
        warnings.append(
            f"{src.name}: a previously-staged hunk changed and was kept host-only "
            f"— re-run `setforge stage {src.name}` to re-confirm it"
        )
    return CaptureResult(name=sub_name, action=result.action, warnings=tuple(warnings))


def _capture_staged_structured(
    profile: str,
    sub_name: str,
    src: Path,
    dst: Path,
    fmt: su_mod.StructuredFormat,
    *,
    auto: "CaptureAuto | None",
) -> CaptureResult | None:
    """A5b staged capture for a structured (YAML/JSON/JSONC) reconcile file.

    The per-KEY analog of :func:`_capture_staged_plain`: promotes ONLY the keys the
    host classified SHARED (live value) or SHARED_DRAFTED (shareable draft value)
    into ``tracked/`` and keeps LOCAL/PENDING host-only. tracked is
    **reconstructed** through the model from ``base`` + the surviving promoted set
    (never text-patched), so a SHARED→LOCAL demote drops the key's value from
    tracked automatically (INV-8). The stored ``base`` is **left unchanged** (it
    advances only on ``install``); ``local`` records the full live bytes, so the
    store still round-trips live verbatim (INV-2).

    Must run inside the caller's ``profile_lock``. Returns ``None`` only when the
    file has not explicitly opted into staged reconciliation. A participating
    file fails closed when its recorded base, live bytes, parse, routing, or
    identities cannot be reconciled; it never falls through to verbatim capture.
    """
    if not _preflight_staged_file(profile, sub_name, dst, fmt):
        return None
    fid = file_id(sub_name)
    base = reconcile_store.read_base(profile, fid)
    assert base is not None  # preflight established the participating store shape
    entry = reconcile_store.read_index(profile).files.get(str(fid))
    assert entry is not None
    assert entry.staged
    stored = index_model.require_unit_kind(entry.hunks, UnitKind.KEY)
    live = dst.read_bytes()
    fresh = su_mod.extract_structured_units(base, live, fmt)
    units = su_mod.classify_structured(fresh, stored)
    drafts = reconcile_store.read_drafts(profile, fid)
    new_text = su_mod.reconstruct_structured(base, live, units, drafts, fmt).decode(
        "utf-8"
    )
    if _keep_tracked_refuses(auto, src, new_text):
        return CaptureResult(
            name=sub_name, action=CaptureAction.SKIPPED, reason="keep-tracked"
        )
    result = _write_if_changed(src, new_text)
    # INV-8: the bytes now ON DISK must be exactly the promoted set; read back the
    # post-write content so this verifies the actual write.
    su_mod.assert_stage_fidelity_structured(
        base, live, src.read_bytes(), units, drafts, fmt
    )
    reconcile_store.record(
        profile,
        fid,
        base=base,
        local=live,
        staged=True,
        hunks=su_mod.serialize_structured(units),
        drafts=drafts,
    )
    warnings: list[str] = []
    if any(unit.cls is HunkClass.PENDING for unit in units):
        warnings.append(
            f"{src.name}: unstaged local changes kept host-only — run "
            f"`setforge stage {src.name}` to share any of them"
        )
    if any(unit.changed and unit.cls is HunkClass.SHARED for unit in units):
        warnings.append(
            f"{src.name}: a previously-staged key changed and was kept host-only "
            f"— re-run `setforge stage {src.name}` to re-confirm it"
        )
    return CaptureResult(name=sub_name, action=result.action, warnings=tuple(warnings))


def capture_profile(
    config: Config,
    profile_name: str,
    repo_root: Path,
    *,
    setforge_yaml_path: Path,
    auto: CaptureAuto | None = None,
    snapshot_base: Path | None = None,
    console: Console | None = None,
    resolved: ResolvedProfile | None = None,
    host_local_sections_map: (
        Mapping[str, dict[HostLocalSectionName, HostLocalSection]] | None
    ) = None,
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
    resolved:
        Pre-resolved effective profile supplied by the CLI. When omitted,
        preserve the domain API's tracked-config-only behavior for callers that
        intentionally resolve overlays themselves.

    Raises
    ------
    KeyboardInterrupt
        Propagated from the wizard when the user cancels mid-prompt;
        the CLI layer renders the cancellation and exits 130.
    """
    # The disposition path runs its own per-conflict capture handling
    # (_capture_disposition_file); disposition=None files capture live verbatim
    # minus host-local overlays. Per-tracked_file writeback below.
    overlay = host_local_sections_map or {}
    results: list[CaptureResult] = []
    effective = (
        resolved if resolved is not None else resolve_profile(config, profile_name)
    )
    work: list[tuple[str, Path, Path, frozenset[HostLocalSectionName]]] = []
    for name in effective.tracked_files:
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
            work.append((sub_name, sub_src, sub_dst, host_local_names))

    # Validate every participating file before the first tracked/store write. A
    # later invalid participant can therefore never leave earlier files captured.
    participating: set[str] = set()
    for sub_name, _sub_src, sub_dst, host_local_names in work:
        # Legacy local.yaml overlays are injected as marker pairs outside the
        # unit model. Keep their historical name-scoped strip path authoritative;
        # otherwise a staged SHARED unit could promote the injected host body.
        if host_local_names:
            continue
        fmt = su_mod.structured_format(sub_dst)
        if _preflight_staged_file(profile_name, sub_name, sub_dst, fmt):
            participating.add(sub_name)

    for sub_name, sub_src, sub_dst, host_local_names in work:
        fmt = su_mod.structured_format(sub_dst)
        if sub_name in participating and not host_local_names:
            if fmt is not None:
                result = _capture_staged_structured(
                    profile_name, sub_name, sub_src, sub_dst, fmt, auto=auto
                )
            else:
                result = _capture_staged_plain(
                    profile_name, sub_name, sub_src, sub_dst, auto=auto
                )
            assert result is not None
        else:
            result = capture_tracked_file(
                sub_src,
                sub_dst,
                host_local_section_names=host_local_names,
                auto=auto,
            )
        results.append(
            CaptureResult(
                name=sub_name,
                action=result.action,
                reason=result.reason,
                warnings=result.warnings,
            )
        )
    return results
