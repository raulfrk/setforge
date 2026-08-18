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
from setforge.reconcile import hunks as reconcile_hunks
from setforge.reconcile import store as reconcile_store
from setforge.reconcile import structured_units as su_mod
from setforge.reconcile.types import FileId, HunkClass, file_id
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


@dataclass(frozen=True, slots=True)
class CaptureResult:
    name: str
    action: CaptureAction
    reason: str = ""
    warnings: tuple[str, ...] = ()


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
    Returns ``None`` (caller falls back to the verbatim writeback) when the file
    is not staged-eligible — no recorded base, live missing, non-UTF-8, OR no hunk
    yet classified SHARED/LOCAL (A5 staging is opt-in per file; an unstaged file
    keeps the legacy absorb behavior).
    """
    if not dst.exists():
        return None
    fid = file_id(sub_name)
    base = reconcile_store.read_base(profile, fid)
    if base is None:
        return None  # not reconcile-managed → legacy verbatim capture
    live = dst.read_bytes()
    try:
        base.decode("utf-8")
        live.decode("utf-8")
    except UnicodeDecodeError:
        return None  # text-only staging; a binary plain file stays verbatim
    hunks = _classify_live(profile, fid, base, live)
    # A5 staging is OPT-IN per file: until the host has classified at least one
    # hunk (SHARED, SHARED_DRAFTED, or LOCAL) via `setforge stage`, the file keeps
    # the legacy capture behavior (sync absorbs live drift into tracked). Falling
    # back here — rather than treating every unstaged hunk as PENDING-keep-local —
    # avoids silently changing sync's behavior for files no one has staged.
    staged = (HunkClass.SHARED, HunkClass.SHARED_DRAFTED, HunkClass.LOCAL)
    if not any(hunk.cls in staged for hunk in hunks):
        return None
    # The shareable-draft bytes for any SHARED_DRAFTED hunk (keyed by anchor);
    # reconstruct splices these in place of the host-specific live bytes, so
    # tracked gets the shareable text while live stays host-specific (the blessed
    # divergence). The drafts manifest is authored by the `stage` share-draft
    # sub-flow, never by capture — so record() below preserves it (drafts=None)
    # rather than rewriting it. When no manifest exists this is empty and
    # reconstruct degrades to the plain SHARED/LOCAL behavior.
    drafts = reconcile_store.read_drafts(profile, fid)
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
    # only on install). drafts=None preserves the existing manifest. Runs
    # unconditionally w.r.t. the tracked NOOP so a classification-only change
    # (e.g. PENDING→LOCAL) still persists.
    reconcile_store.record(
        profile,
        fid,
        base=base,
        local=live,
        hunks=reconcile_hunks.serialize(hunks),
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
    if any(hunk.changed for hunk in hunks):
        warnings.append(
            f"{src.name}: a previously-staged hunk changed and was kept host-only "
            f"— re-run `setforge stage {src.name}` to re-confirm it"
        )
    return CaptureResult(name=sub_name, action=result.action, warnings=tuple(warnings))


def _classify_live(
    profile: str, fid: FileId, base: bytes, live: bytes
) -> list[reconcile_hunks.Hunk]:
    """Freshly extract base↔live hunks and carry stored classifications by identity."""
    entry = reconcile_store.read_index(profile).files.get(str(fid))
    stored = entry.hunks if entry is not None else []
    return reconcile_hunks.classify(reconcile_hunks.extract_hunks(base, live), stored)


def _classify_live_structured(
    profile: str, fid: FileId, fresh: list[su_mod.KeyUnit]
) -> list[su_mod.KeyUnit]:
    """Carry stored KEY-unit classifications onto freshly-extracted units by path."""
    entry = reconcile_store.read_index(profile).files.get(str(fid))
    stored = entry.hunks if entry is not None else []
    return su_mod.classify_structured(fresh, stored)


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

    Must run inside the caller's ``profile_lock``. Returns ``None`` (caller falls
    back to the verbatim writeback) when not staged-eligible — no recorded base,
    live missing, unparseable structured content, OR no key yet classified
    SHARED/SHARED_DRAFTED/LOCAL (per-file opt-in, like the plain path).
    """
    if not dst.exists():
        return None
    fid = file_id(sub_name)
    base = reconcile_store.read_base(profile, fid)
    if base is None:
        return None  # not reconcile-managed → legacy verbatim capture
    live = dst.read_bytes()
    try:
        fresh = su_mod.extract_structured_units(base, live, fmt)
    except Exception:
        return None  # unparseable structured live → verbatim fallback
    units = _classify_live_structured(profile, fid, fresh)
    # A5 staging is OPT-IN per file (mirrors the plain path): until at least one
    # key is classified, the file keeps the legacy absorb behavior.
    staged = (HunkClass.SHARED, HunkClass.SHARED_DRAFTED, HunkClass.LOCAL)
    if not any(unit.cls in staged for unit in units):
        return None
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
        hunks=su_mod.serialize_structured(units),
    )
    warnings: list[str] = []
    if any(unit.cls is HunkClass.PENDING for unit in units):
        warnings.append(
            f"{src.name}: unstaged local changes kept host-only — run "
            f"`setforge stage {src.name}` to share any of them"
        )
    if any(unit.changed for unit in units):
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
            # A5: a plain reconcile file (no host-local overlay) with a recorded
            # base captures per-hunk — only the SHARED hunks promote into
            # tracked/. A structured (YAML/JSON/JSONC) file takes the per-KEY
            # analog instead (line-diffing a structured file would mis-stage it).
            # Both fall back to the verbatim writeback below when not
            # staged-eligible (no base / binary / unparseable).
            staged: CaptureResult | None = None
            if not host_local_names:
                fmt = su_mod.structured_format(sub_dst)
                if fmt is not None:
                    staged = _capture_staged_structured(
                        profile_name, sub_name, sub_src, sub_dst, fmt, auto=auto
                    )
                else:
                    staged = _capture_staged_plain(
                        profile_name, sub_name, sub_src, sub_dst, auto=auto
                    )
            if staged is not None:
                result = staged
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
