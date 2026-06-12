"""Drift compare for tracked → live deployments.

Every ``DRIFTED`` file carries a :class:`DriftClass` explaining the drift:

- ``expected`` — intentional host divergence: ``forked``/``pinned``
  disposition, or drift confined to pinned/forked spans (Invariant I13).
- ``stale`` — live still equals the stored base while tracked advanced;
  the next install fast-forwards live. Not flagged by ``compare --check``.
- ``unexpected`` — drift nothing above explains: what ``compare --check``
  flags for CI and what the install drift gate (``--auto-accept-*``)
  resolves. Also covers the clobber shape — span-only drift with NO
  stored byte base, where a first install would deploy tracked verbatim
  over the live span edits (run ``sync`` first).
- ``conflicted`` — reserved for forked-scalar span conflicts (the
  detection is not wired yet; see :func:`_classify_drifted` slot 1).

Orphan detection (:func:`detect_orphans`, :class:`OrphanEntry`) is a
separate axis surfaced alongside drift: live files setforge previously
deployed (per ``transitions/*/meta.json`` ``paths``) that are no longer
listed in any resolved tracked_files entry. The ``cleanup-orphans``
subcommand re-computes orphans under ``--apply`` and removes them.
"""

import difflib
import json
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from jinja2 import Template
from rich.table import Table
from ruamel.yaml import YAML

from setforge import (
    base_store,
    host_local_inject,
    jsonc,
    section_reconcile,
    spans_overlay,
)
from setforge.binaries import LOCAL_CONFIG_PATH
from setforge.config import (
    Config,
    Disposition,
    ResolvedProfile,
    TrackedFile,
    resolve_profile,
)
from setforge.errors import BaseStoreError
from setforge.paths import template_context
from setforge.source import HostLocalSection, HostLocalSectionName

if TYPE_CHECKING:
    from setforge.config import HostLocalTrackedFileOverride, LocalOverlayResolution
    from setforge.local_overlay import (
        OverlayOrigin,
        ResolvedExtension,
        ResolvedMarketplace,
        ResolvedPlugin,
    )

    # PEP 695 type alias for the three overlay resolution lists shape.
    # Defined under TYPE_CHECKING so the ``Resolved*`` forward refs
    # don't pay an import-time cost on every command boot (the names
    # are only consumed by checkers + IDEs, never instantiated at
    # runtime); the ``_counts`` helper in
    # :func:`_format_overlay_footer_summary` annotates with this alias
    # via a string forward ref.
    type _OverlayResolvedEntries = (
        list[ResolvedPlugin] | list[ResolvedExtension] | list[ResolvedMarketplace]
    )


class CompareStatus(StrEnum):
    UNCHANGED = "unchanged"
    DRIFTED = "drifted"
    MISSING = "missing"


class DriftClass(StrEnum):
    """Why a ``DRIFTED`` file drifted; see :func:`_classify_drifted`."""

    EXPECTED = "expected"
    UNEXPECTED = "unexpected"
    STALE = "stale"
    CONFLICTED = "conflicted"


_STALE_REASON = "tracked advanced since last install — install will update"

_CLOBBER_REASON = (
    "span edits present but no stored base — first install would "
    "overwrite; run sync first"
)


@dataclass(frozen=True, slots=True)
class FileCompare:
    """Per-file drift result from :func:`compare_profile`.

    ``disposition`` carries the tracked_file's :class:`~setforge.config.Disposition`
    value (``None`` for legacy preserve-based files). The derived property
    ``drift_is_expected`` is ``True`` when drift is classified as intentional
    host divergence — i.e., disposition is ``FORKED`` or ``PINNED`` AND the
    file is ``DRIFTED``. ``SHARED`` drift and all non-disposition-file drift
    is *not* expected (needs attention).
    """

    name: str
    status: CompareStatus
    diff: str
    mode_drift: bool = False
    """True when the tracked_file declares ``mode:`` and the live file's
    permission bits (via :func:`stat.S_IMODE`) differ. Always False when
    ``mode:`` is unset — the drift axis is opt-in per tracked_file.
    """
    live_mode: int | None = None
    """The live file's permission bits when ``mode:`` is declared, else ``None``.

    Populated alongside :attr:`mode_drift` so the install confirm plan can
    render the ``live → tracked`` mode transition. ``None`` when ``mode:``
    is unset (no mode axis to report).
    """
    tracked_mode: int | None = None
    """The tracked_file's declared ``mode:`` value, else ``None``.

    The mode the live file is reset to on deploy; paired with
    :attr:`live_mode` for the confirm-plan transition line.
    """
    disposition: Disposition | None = None
    """The :class:`~setforge.config.Disposition` of the source tracked_file,
    or ``None`` for files without a disposition (legacy preserve-* model).
    """
    span_only_drift: bool = False
    """True when this file carries sub-file spans AND every drifting region
    is confined to a pinned/forked span (Invariant I13). A SHARED file whose
    ONLY divergence lives inside its spans is intentional host divergence,
    not unsynced shared drift — so it must NOT render identically to a real
    shared-drift case. Always False for files without spans.
    """
    drift_class: DriftClass | None = None
    """Why the file drifted, per :func:`_classify_drifted`. ``None`` unless
    ``status`` is ``DRIFTED``.
    """
    reason: str | None = None
    """Human-readable note for the drift class (the summary table's ``Why``
    column). ``None`` when the class needs no elaboration.
    """
    forked_scalar_conflicts: list[str] = field(default_factory=list)
    """Forked-scalar span conflicts (``base ≠ live AND base ≠ tracked``).
    Always empty today — the CONFLICTED detection is not wired yet; the
    field reserves the ``--json`` schema slot.
    """

    @property
    def drift_is_expected(self) -> bool:
        """True when the file's drift is classified as intentionally expected.

        Drift is expected when the tracked_file's disposition is ``FORKED``
        or ``PINNED`` AND the file is ``DRIFTED``, OR when the file is
        ``DRIFTED`` but every diverging region is confined to a pinned/forked
        span (``span_only_drift``, Invariant I13). A ``SHARED`` file's
        non-span drift always needs attention; a ``None``-disposition file
        never uses this axis (returns False regardless of drift status).
        """
        if self.status is not CompareStatus.DRIFTED:
            return False
        if self.disposition in (Disposition.FORKED, Disposition.PINNED):
            return True
        return self.span_only_drift


@dataclass(frozen=True, slots=True)
class OrphanEntry:
    """One live path that setforge previously deployed but is no longer tracked.

    The ``path`` field is the absolute live path that ``cleanup-orphans``
    would remove. Captured from ``meta.json``'s ``paths`` field (the set
    of paths setforge actually touched on this host), cross-referenced
    against the resolved profile's current tracked_files. No re-tracking
    or migration heuristic — orphans are strictly "previously here, no
    longer in setforge.yaml."
    """

    path: Path


@dataclass(frozen=True, slots=True)
class CompareReport:
    entries: list[FileCompare]
    has_unexpected_drift: bool
    orphans: list[OrphanEntry] = field(default_factory=list)
    orphan_skipped_absent: int = 0
    orphan_skipped_source: int = 0


@dataclass(frozen=True, slots=True)
class OrphanDetection:
    """Result of :func:`detect_orphans`.

    ``orphans`` is the kept set (deployed dst paths that still exist on
    disk and are no longer tracked). ``skipped_absent`` /
    ``skipped_source`` tally the candidates the guards filtered out — a
    path no longer on disk, or a path that is a tracked SOURCE rather
    than a deployed dst — so the CLI can surface a transparency note.
    """

    orphans: list[OrphanEntry]
    skipped_absent: int = 0
    skipped_source: int = 0


def _norm(path: Path) -> Path:
    """Lexically normalize ``path`` (expand ``~``, collapse ``.``/``..``).

    Purely lexical via :func:`os.path.normpath` — NEVER resolves
    symlinks. A candidate orphan that is a symlink must reach ``unlink``
    un-dereferenced; resolving first would target the pointed-to file.
    Applied to BOTH sides of every orphan comparison so relative / ``~``
    / ``..`` aliasing cannot make a guard fail open.
    """
    return Path(os.path.normpath(path.expanduser()))


def _resolved_tracked_dsts(
    resolved: ResolvedProfile,
    config: Config,
    repo_root: Path,
    *,
    extra_ids: frozenset[str],
) -> set[Path]:
    """Resolved destination set for orphan exclusion.

    Combines the resolved profile's ``tracked_files`` with any
    ``extra_ids`` (the user's ``orphan_ignore`` list). Unknown ids in
    ``extra_ids`` are silently skipped — a user may have removed an
    ignore entry without cleaning the corresponding file; treating the
    id as still-tracked is the safer default.

    Directory-type tracked_files are expanded via
    :func:`expand_tracked_file` so every deployed CHILD dst joins the
    set — without this a directory tracked_file's children surface as
    orphans (touched-but-absent from the parent-only dst set). All paths
    are lexically normalized to match the candidate side.
    """
    names = list(resolved.tracked_files) + [
        name for name in extra_ids if name in config.tracked_files
    ]
    tracked_paths: set[Path] = set()
    for name in names:
        tracked_file = config.tracked_files[name]
        src = resolve_src(tracked_file, repo_root)
        dst = resolve_dst(tracked_file)
        for _, _, sub_dst in expand_tracked_file(name, src, dst):
            tracked_paths.add(_norm(sub_dst))
    return tracked_paths


def _tracked_source_paths(config: Config, repo_root: Path) -> set[Path]:
    """Resolved SRC path of every configured tracked_file, normalized.

    A source path can never be a legitimate orphan — orphans are by
    definition deployed dst copies. Built from ALL ``config.tracked_files``
    (not just the active profile): a stale ``meta.json`` may carry a src
    belonging to a tracked_file outside the resolved profile. This set is
    the backstop for a ``src`` that escapes ``repo_root/tracked`` via
    ``..`` (the field permits it).
    """
    return {_norm(resolve_src(tf, repo_root)) for tf in config.tracked_files.values()}


def _touched_paths_from_meta(transitions_dir: Path) -> set[Path]:
    """Aggregate the ``paths`` field across every ``meta.json`` on disk.

    Robust against malformed / unreadable meta.json files: a single bad
    record is skipped, not fatal. Missing ``transitions_dir`` (no
    install history yet) returns an empty set.
    """
    if not transitions_dir.exists():
        return set()
    touched: set[Path] = set()
    for meta_path in transitions_dir.glob("*/meta.json"):
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        raw_paths = payload.get("paths")
        if not isinstance(raw_paths, list):
            continue
        for raw in raw_paths:
            if isinstance(raw, str):
                touched.add(_norm(Path(raw)))
    return touched


def detect_orphans(
    resolved: ResolvedProfile,
    config: Config,
    transitions_dir: Path,
    repo_root: Path,
    *,
    ignored: frozenset[str] = frozenset(),
) -> OrphanDetection:
    """Find live files setforge previously deployed that no longer appear
    in ``resolved.tracked_files``.

    Walks every ``transitions_dir/*/meta.json`` ``paths`` field (the
    set of paths setforge actually touched on this host), subtracts the
    set of currently-resolved tracked destinations, then applies two
    guards so the result can never schedule a non-orphan for deletion:

    1. **Source guard** — a candidate under ``repo_root/tracked`` or
       equal to any tracked_file's resolved SRC is dropped. A source
       file is never a deployed dst, so it can never be an orphan;
       without this a stale ``meta.json`` that recorded src paths would
       list the config source of truth for deletion.
    2. **Existence gate** — a candidate no longer present on disk is
       dropped. Uses :func:`os.path.lexists` (lstat semantics) to match
       the apply path's ``_lstat_safe`` delete check, so a dangling
       symlink (still a real, deletable dir entry) is RETAINED while a
       fully-absent path is filtered. The report then equals exactly
       what ``--apply`` would delete.

    The source guard runs BEFORE the existence gate so a tracked source
    that happens to exist on disk is tallied as a source skip, not
    leaked through. ``ignored`` is a set of tracked_file IDs the user
    marked "keep orphan" via ``cleanup-orphans --ignore <id>``; their
    resolved destinations join the tracked set so they never surface.
    Returns an :class:`OrphanDetection` carrying the kept orphans and
    the per-guard skip tallies.
    """
    tracked_paths = _resolved_tracked_dsts(
        resolved, config, repo_root, extra_ids=ignored
    )
    touched_paths = _touched_paths_from_meta(transitions_dir)
    src_root = _norm(repo_root / "tracked")
    src_paths = _tracked_source_paths(config, repo_root)

    kept: list[OrphanEntry] = []
    skipped_absent = 0
    skipped_source = 0
    for path in sorted(touched_paths - tracked_paths, key=str):
        if path.is_relative_to(src_root) or path in src_paths:
            skipped_source += 1
            continue
        if not os.path.lexists(path):
            skipped_absent += 1
            continue
        kept.append(OrphanEntry(path=path))
    return OrphanDetection(
        orphans=kept,
        skipped_absent=skipped_absent,
        skipped_source=skipped_source,
    )


def load_ignored_orphans() -> frozenset[str]:
    """Return the set of tracked_file IDs flagged "keep orphan".

    Reads ``orphan_ignore: [<id>, ...]`` from
    :data:`setforge.binaries.LOCAL_CONFIG_PATH`. Returns an empty
    frozenset when the file is absent, the key is missing, or the
    payload is malformed (best-effort posture — a corrupt local.yaml
    must not turn orphan-detection into a hard failure on every
    ``compare``).
    """
    if not LOCAL_CONFIG_PATH.exists():
        return frozenset()
    yaml = YAML(typ="safe")
    try:
        data = yaml.load(LOCAL_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        # Best-effort: a corrupt local.yaml must not turn orphan-detection
        # into a hard failure on every ``compare``. The host-local config
        # has its own validation path via :func:`load_host_local_config`
        # for cases where strictness matters; orphan-ignore is advisory.
        return frozenset()
    if not isinstance(data, dict):
        return frozenset()
    raw = data.get("orphan_ignore")
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(item for item in raw if isinstance(item, str))


def resolve_src(tracked_file: TrackedFile, repo_root: Path) -> Path:
    """Resolve a tracked_file's ``src`` (relative to ``tracked/``) to an
    absolute path inside the repo."""
    return repo_root / "tracked" / tracked_file.src


def resolve_dst(tracked_file: TrackedFile) -> Path:
    """Resolve a tracked_file's ``dst`` template (if any) to an absolute path
    via Jinja2 + ``~`` expansion."""
    raw = tracked_file.dst
    if tracked_file.template:
        raw = Template(raw).render(**template_context())
    return Path(raw).expanduser()


def diff_file(
    src: Path,
    dst: Path,
    *,
    host_local_sections: dict[HostLocalSectionName, HostLocalSection] | None = None,
) -> str:
    """Return the unified diff between ``src`` (tracked) and ``dst`` (live).

    For a ``disposition=None`` tracked_file the deployed content is ``src``
    verbatim, so the comparison is a plain unified diff. When
    ``host_local_sections`` is non-empty the rendered ``src`` is augmented with
    the same legacy host-local marker injection deploy performs, so a live file
    that already received its host-local sections does NOT show as drift.
    """
    if not dst.exists():
        return ""

    dst_text = dst.read_text(encoding="utf-8")
    rendered_src = src.read_text(encoding="utf-8")
    if host_local_sections:
        rendered_src = host_local_inject.inject_all(rendered_src, host_local_sections)
        rendered_src = section_reconcile.maintain_marker_hashes(rendered_src)
    diff_lines = difflib.unified_diff(
        dst_text.splitlines(keepends=True),
        rendered_src.splitlines(keepends=True),
        fromfile=str(dst),
        tofile=str(src),
    )
    return "".join(diff_lines)


def expand_tracked_file(
    name: str, src: Path, dst: Path
) -> list[tuple[str, Path, Path]]:
    """Expand a tracked_file into ``(name, src_file, dst_file)`` triples.

    Plain files yield a single triple; directories yield one triple per
    contained file with a ``name/relpath`` synthetic name.
    """
    if src.is_dir():
        triples: list[tuple[str, Path, Path]] = []
        for file in sorted(src.rglob("*")):
            if file.is_file():
                rel = file.relative_to(src)
                triples.append((f"{name}/{rel}", file, dst / rel))
        return triples
    return [(name, src, dst)]


def compare_profile(
    config: Config,
    profile_name: str,
    repo_root: Path,
    *,
    transitions_dir: Path | None = None,
    ignored: frozenset[str] = frozenset(),
    host_local_sections: (
        Mapping[str, dict[HostLocalSectionName, HostLocalSection]] | None
    ) = None,
) -> CompareReport:
    """Build a :class:`CompareReport` for every tracked_file in the resolved profile.

    When ``transitions_dir`` is provided, also detects orphans (live
    files setforge previously deployed but no longer tracked) via
    :func:`detect_orphans`. ``ignored`` is the set of tracked_file IDs
    flagged "keep orphan" via ``cleanup-orphans --ignore`` (stored in
    ``~/.config/setforge/local.yaml``). When ``transitions_dir`` is
    ``None`` the orphans list is empty — preserves the pre-orphan call
    shape for callers that don't have a transitions dir handy.

    ``host_local_sections`` is the validated local.yaml overlay shaped
    ``{tracked_file_id: {section_name: HostLocalSection}}`` (SPEC 1).
    When provided, the per-tracked_file overlay is threaded
    into :func:`diff_file` so a live file that already received its
    host-local sections does NOT show up as drift, AND the post-merge
    rendered ``src`` mirrors what ``setforge install`` would actually
    deploy (overlay-aware compare). The CLI surface
    (:func:`setforge.cli.compare.compare`) loads + validates the map
    via :func:`setforge.cli._install_helpers._load_validated_host_local_sections`
    before passing it in; callers that don't carry an overlay (e.g.
    the orphan-detection and status commands) pass ``None`` and get the
    pre-host-local behavior.

    Overlay contract (SPEC 2): this function re-resolves
    the profile via :func:`resolve_profile` and intentionally discards
    any :func:`apply_local_overlay` mutations to
    ``resolved.claude_plugins`` or ``resolved.extensions.include`` that
    callers may have applied upstream. That's safe today because compare
    only iterates ``resolved.tracked_files`` — a field the overlay never
    touches. If compare ever starts reading plugin / extension lists
    (e.g. to surface overlay-tagged drift in the report), this
    re-resolution MUST be replaced by accepting a pre-resolved
    :class:`ResolvedProfile` parameter so the overlay's mutations
    survive.
    """
    resolved = resolve_profile(config, profile_name)
    entries: list[FileCompare] = []
    has_unexpected = False
    overlay = host_local_sections or {}

    for name in resolved.tracked_files:
        tracked_file = config.tracked_files[name]
        src = resolve_src(tracked_file, repo_root)
        dst = resolve_dst(tracked_file)
        host_local = overlay.get(name) or None

        for sub_name, sub_src, sub_dst in expand_tracked_file(name, src, dst):
            entry, sub_unexpected = _compare_one(
                sub_name,
                sub_src,
                sub_dst,
                tracked_file,
                profile=profile_name,
                host_local_sections=host_local,
            )
            entries.append(entry)
            if sub_unexpected:
                has_unexpected = True

    orphans: list[OrphanEntry] = []
    skipped_absent = 0
    skipped_source = 0
    if transitions_dir is not None:
        detection = detect_orphans(
            resolved, config, transitions_dir, repo_root, ignored=ignored
        )
        orphans = detection.orphans
        skipped_absent = detection.skipped_absent
        skipped_source = detection.skipped_source

    return CompareReport(
        entries=entries,
        has_unexpected_drift=has_unexpected,
        orphans=orphans,
        orphan_skipped_absent=skipped_absent,
        orphan_skipped_source=skipped_source,
    )


def _compare_one(
    name: str,
    src: Path,
    dst: Path,
    tracked_file: TrackedFile,
    *,
    profile: str | None = None,
    host_local_sections: dict[HostLocalSectionName, HostLocalSection] | None = None,
) -> tuple[FileCompare, bool]:
    # Symlink-aware tracked_files dispatch FIRST: ``Path.exists()`` returns
    # False on a dangling symlink, which would otherwise misclassify the
    # case as MISSING. ``_compare_symlinked`` probes ``is_symlink()`` first
    # so dangling links surface as DRIFTED (target drift / broken link)
    # rather than MISSING.
    if tracked_file.symlink is not None:
        return _compare_symlinked(
            name,
            src,
            dst,
            tracked_file,
            profile=profile,
            host_local_sections=host_local_sections,
        )

    disposition = tracked_file.disposition

    if not dst.exists():
        return (
            FileCompare(
                name=name,
                status=CompareStatus.MISSING,
                diff="",
                disposition=disposition,
            ),
            True,
        )

    diff = diff_file(
        src,
        dst,
        host_local_sections=host_local_sections,
    )

    mode_drift = False
    live_mode: int | None = None
    tracked_mode: int | None = None
    if tracked_file.mode is not None:
        # lstat (not stat) for symlink-posture parity with snapshots.py:
        # a symlink dst reports drift on the LINK's own mode, never the
        # target's — setforge never deploys through a live symlink.
        live_mode = stat.S_IMODE(dst.lstat().st_mode)
        tracked_mode = tracked_file.mode
        mode_drift = live_mode != tracked_file.mode

    is_drifted = bool(diff) or mode_drift
    status = CompareStatus.DRIFTED if is_drifted else CompareStatus.UNCHANGED

    span_only_drift = _span_only_drift(src, dst, tracked_file) if diff else False

    entry = FileCompare(
        name=name,
        status=status,
        diff=diff,
        mode_drift=mode_drift,
        live_mode=live_mode,
        tracked_mode=tracked_mode,
        disposition=disposition,
        span_only_drift=span_only_drift,
    )
    return _classify_entry(entry, profile=profile, src=src, dst=dst)


def _classify_entry(
    entry: FileCompare,
    *,
    profile: str | None,
    src: Path,
    dst: Path,
    probe_stale: bool = True,
) -> tuple[FileCompare, bool]:
    """Attach the drift class to a ``DRIFTED`` entry and derive its
    unexpected flag.

    Non-``DRIFTED`` entries pass through with ``drift_class=None`` and an
    unexpected flag of ``False`` (a MISSING entry never reaches here — its
    caller returns the existing ``True`` contract directly).
    """
    if entry.status is not CompareStatus.DRIFTED:
        return entry, False
    drift_class, reason = _classify_drifted(
        entry, profile=profile, src=src, dst=dst, probe_stale=probe_stale
    )
    entry = replace(entry, drift_class=drift_class, reason=reason)
    is_unexpected = drift_class in (DriftClass.UNEXPECTED, DriftClass.CONFLICTED)
    return entry, is_unexpected


def _classify_drifted(
    entry: FileCompare,
    *,
    profile: str | None,
    src: Path,
    dst: Path,
    probe_stale: bool = True,
) -> tuple[DriftClass, str | None]:
    """Classify a ``DRIFTED`` entry; first matching slot wins.

    ``probe_stale=False`` skips the base-store reads (slots 2 + 3) for
    entries whose ``src``/``dst`` byte comparison is meaningless (symlink
    metadata drift). ``profile=None`` (direct unit-scope calls) also
    skips them — the stored base is keyed by profile.
    """
    # Slot 1 — CONFLICTED: a forked-scalar span where base ≠ live AND
    # base ≠ tracked. Detection not wired yet; a follow-up populates
    # ``forked_scalar_conflicts`` and short-circuits here.
    # Slot 2 — UNEXPECTED (clobber): span-only drift with no stored byte
    # base. The base-absent install path deploys tracked verbatim
    # (disposition_merge's first-run branch) and does not honor every
    # span override, so the live span edits are at clobber risk until a
    # sync stores a base. A PINNED disposition is exempt — install never
    # overwrites its live file.
    if (
        probe_stale
        and profile is not None
        and entry.span_only_drift
        and entry.disposition is not Disposition.PINNED
        and _base_absent(profile, entry.name)
    ):
        return DriftClass.UNEXPECTED, _CLOBBER_REASON
    # Slot 3 — STALE: live still equals the stored base while tracked
    # advanced; the next install fast-forwards live.
    if probe_stale and profile is not None and _is_stale(profile, entry.name, src, dst):
        return DriftClass.STALE, _STALE_REASON
    # Slot 4 — EXPECTED: intentional host divergence (forked/pinned
    # disposition, or drift confined to pinned/forked spans).
    if entry.drift_is_expected:
        return DriftClass.EXPECTED, None
    # Slot 5 — UNEXPECTED: drift nothing above explains.
    return DriftClass.UNEXPECTED, None


def _base_absent(profile: str, file_id: str) -> bool:
    """True when NO byte base is stored for ``(profile, file_id)``.

    State-aware but crash-free, mirroring :func:`_is_stale`: a torn or
    failing base-store read is NOT absence — it degrades to ``False`` so
    the entry classifies deterministically via the later slots instead of
    over-reporting clobber risk on a transient read error.
    """
    try:
        return base_store.read_base(profile, file_id) is None
    except (BaseStoreError, OSError):
        return False


def _is_stale(profile: str, file_id: str, src: Path, dst: Path) -> bool:
    """True when live (``dst``) still equals the stored base while tracked
    (``src``) advanced — the stale-deploy shape where the next install
    fast-forwards live.

    State-aware but crash-free: any base-store or filesystem read error
    degrades to ``False`` (the entry then classifies via the later slots).
    The read is not locked against a concurrent install — single-user CLI;
    the read-once race is accepted.
    """
    try:
        base = base_store.read_base(profile, file_id)
    except (BaseStoreError, OSError):
        return False
    if base is None:
        return False
    try:
        live = dst.read_bytes()
        tracked = src.read_bytes()
    except OSError:
        return False
    return live == base and tracked != base


def _span_only_drift(src: Path, dst: Path, tracked_file: TrackedFile) -> bool:
    """True when the live↔tracked drift is confined to pinned/forked spans.

    Replaces every span region in the live bytes with the tracked bytes and,
    if the result equals tracked, the only divergence lived inside spans —
    intentional host divergence, not unsynced shared drift (Invariant I13).
    Dispatches by file type so each span flavor mirrors its own capture
    exclusion path: markdown heading spans via
    :func:`setforge.spans_overlay.exclude_spans_for_capture`; structural
    (yaml/json/jsonc dotted-path) spans via
    :func:`setforge.disposition_merge.exclude_structural_spans_for_capture`.
    False when the file declares no spans, is neither markdown nor structural,
    or has drift outside a span.
    """
    from setforge import disposition_merge, overlay_deploy
    from setforge.overlay_inject import OverlayAmbiguousError
    from setforge.spans import SpanKind

    if not tracked_file.spans:
        return False
    try:
        tracked_text = src.read_text(encoding="utf-8")
        live_text = dst.read_text(encoding="utf-8")
    except OSError:
        return False
    if disposition_merge.is_structural(src):
        # Warnings are a capture-surface concern; compare only needs the
        # excluded text for the equality probe.
        excluded, _ = disposition_merge.exclude_structural_spans_for_capture(
            live_text, tracked_text, tracked_file.spans, jsonc.is_jsonc_file(src)
        )
    elif src.suffix.lower() in {".md", ".markdown"}:
        # Excise the markerless OVERLAY bodies first (by their exact recorded
        # bytes) so a host-local body present in live but absent from tracked
        # is treated as expected span-confined drift, not spurious DRIFTED.
        # The canonical body is the needle; no per-host state is consulted
        # here (compare is offline / state-free), so a body that was
        # hand-edited away from canonical falls through to non-span drift.
        md_overlay = overlay_deploy.overlay_spans(tracked_file.spans)
        body_free_live = live_text
        if md_overlay:
            try:
                body_free_live, _ = overlay_deploy.excise_overlay_bodies(
                    live_text, md_overlay, {}
                )
            except OverlayAmbiguousError:
                return False
        pinned_forked = [
            s for s in tracked_file.spans if s.kind is not SpanKind.OVERLAY
        ]
        excluded = spans_overlay.exclude_spans_for_capture(
            body_free_live, tracked_text, pinned_forked, {}
        )
    else:
        return False
    return excluded == tracked_text


def _compare_symlinked(
    name: str,
    src: Path,
    dst: Path,
    tracked_file: TrackedFile,
    *,
    profile: str | None = None,
    host_local_sections: dict[HostLocalSectionName, HostLocalSection] | None = None,
) -> tuple[FileCompare, bool]:
    """Classify a symlink-deployed tracked_file's live state.

    Probes ``is_symlink()`` BEFORE ``exists()`` to avoid misclassifying
    a dangling symlink (``exists()`` returns False on broken links) as
    MISSING — the existing-bug surface symlink-compare fixes.

    Four drift shapes count as DRIFTED (returns ``(entry, True)``):

    - ``dst`` is a regular file (not a symlink) but exists: user
      replaced setforge's symlink with their own content.
    - ``dst`` is a symlink whose ``os.readlink`` does not match the
      declared :attr:`TrackedFile.symlink` (raw string) — target drift.
    - ``dst`` is a correct symlink but the target file's CONTENT has
      drifted from ``src`` — surfaced via :func:`diff_file` against the
      expanded target path. Broken links (target absent) are silent
      here because :func:`diff_file` returns ``""`` when ``dst`` doesn't
      exist.
    - ``dst`` is a correct symlink to a non-existent target (broken
      link): classified UNCHANGED. The link metadata matches what
      setforge installed; the user separately removed the target.

    MISSING is reserved for "no symlink, no regular file at dst" — the
    deploy hasn't happened (or was removed) and there is no user file
    in the way.
    """
    expected = tracked_file.symlink
    if expected is None:  # caller-side invariant; defensive narrow.
        raise AssertionError(
            "_compare_symlinked called with tracked_file.symlink == None"
        )
    disposition = tracked_file.disposition
    if not dst.is_symlink():
        if dst.exists():
            entry = FileCompare(
                name=name,
                status=CompareStatus.DRIFTED,
                diff=(f"expected symlink to {expected!r}, found regular file at {dst}"),
                disposition=disposition,
            )
            return _classify_entry(
                entry, profile=profile, src=src, dst=dst, probe_stale=False
            )
        return (
            FileCompare(
                name=name,
                status=CompareStatus.MISSING,
                diff="",
                disposition=disposition,
            ),
            True,
        )
    actual = os.readlink(dst)
    if actual != expected:
        entry = FileCompare(
            name=name,
            status=CompareStatus.DRIFTED,
            diff=(f"symlink target drift at {dst}: {actual!r} != {expected!r}"),
            disposition=disposition,
        )
        return _classify_entry(
            entry, profile=profile, src=src, dst=dst, probe_stale=False
        )

    # Link metadata is correct; probe the target's CONTENT for drift.
    # ``diff_file`` returns ``""`` when its second argument does not
    # exist, so a broken link (target absent) naturally lands UNCHANGED
    # here — the link itself is still what setforge installed. Reuse
    # the type-narrowed ``expected`` (str, non-None at this point) so
    # mypy sees a clean ``str`` argument to ``Path(...)`` rather than
    # the still-Optional ``tracked_file.symlink``.
    target_path = Path(expected).expanduser()
    target_diff = diff_file(
        src,
        target_path,
        host_local_sections=host_local_sections,
    )
    if target_diff:
        entry = FileCompare(
            name=name,
            status=CompareStatus.DRIFTED,
            diff=target_diff,
            disposition=disposition,
        )
        return _classify_entry(
            entry, profile=profile, src=src, dst=dst, probe_stale=False
        )

    return (
        FileCompare(
            name=name,
            status=CompareStatus.UNCHANGED,
            diff="",
            disposition=disposition,
        ),
        False,
    )


def render_host_local_tracked_file_overrides_block(
    overrides: "Mapping[str, HostLocalTrackedFileOverride]",
) -> list[str]:
    """Build compare-output lines for host-local
    ``mode`` / ``dst`` / ``symlink_target`` overrides.

    Returns an empty list when ``overrides`` is empty — the block
    is suppressed when local.yaml introduces no overlay-fields override.
    Otherwise returns one line per tracked_file with one
    bracketed provenance tag per overridden field, mirroring the
    SPEC 2 ``[from local.yaml]`` style:

    - ``[host-local mode=0o755]`` for a chmod override
    - ``[host-local dst=/etc/foo]`` for a destination retarget
    - ``[host-local symlink → /usr/local/foo]`` for a symlink install

    Pure function — the caller prints each line so test fixtures can
    assert on string content directly. Sort by tracked_file id so
    the output is stable across local.yaml mapping insertion order.
    """
    if not overrides:
        return []

    lines: list[str] = []
    lines.append("=== applying host overlay (~/.config/setforge/local.yaml) ===")
    plural = "s" if len(overrides) != 1 else ""
    lines.append(
        f"tracked_files host-local overrides: {len(overrides)} file{plural} affected"
    )
    for tf_id in sorted(overrides):
        override = overrides[tf_id]
        tags: list[str] = []
        if override.mode is not None:
            tags.append(f"[host-local mode={override.mode:#o}]")
        if override.dst is not None:
            tags.append(f"[host-local dst={override.dst}]")
        if override.symlink_target is not None:
            # U+2192 RIGHTWARDS ARROW per the field's "→" convention,
            # mirroring the SPEC 2 U+2212 minus-sign discipline: one
            # Unicode glyph carries the renderer's semantics across
            # every output sink.
            tags.append(f"[host-local symlink → {override.symlink_target}]")
        lines.append(f"  {tf_id}: {' '.join(tags)}")
    return lines


def render_local_overlay_block(
    config: Config, resolution: "LocalOverlayResolution"
) -> list[str]:
    """Build SPEC 2 compare-output lines for the plugin/ext/mp overlay.

    Returns an empty list when no axis has any entries OR no axis has a
    local-overlay-affected entry — the host-overlay summary footer is
    only emitted when local.yaml introduced at least one change.

    Renders one section per axis (Claude plugins / VSCode extensions /
    Marketplaces) with the per-entry provenance tags inline, then a
    final ``[Host overlay summary: ...]`` line carrying the
    +adds/-removes counts per axis (Q9 Shape A from SPEC 2).

    Pure function — the caller (``setforge compare`` CLI) prints each
    line so test fixtures can assert on string content directly. The
    SPEC 2 mockup uses the U+2212 minus sign for the remove tag; this
    function routes the literal through
    :func:`setforge.local_overlay.display_tag` (SoT for the wording).
    """
    from setforge.local_overlay import (
        has_local_overlay,
    )

    any_overlay = (
        has_local_overlay(resolution.plugins)
        or has_local_overlay(resolution.extensions)
        or has_local_overlay(resolution.marketplaces)
    )
    if not any_overlay:
        return []

    lines: list[str] = []
    _emit_overlay_section(
        lines,
        header="Claude plugins:",
        entries=[(e.value, e.origin) for e in resolution.plugins],
        format_value=lambda v: v,
    )
    _emit_overlay_section(
        lines,
        header="VSCode extensions:",
        entries=[(e.value, e.origin) for e in resolution.extensions],
        format_value=lambda v: v,
    )
    _emit_overlay_section(
        lines,
        header="Marketplaces:",
        entries=[(e.value, e.origin) for e in resolution.marketplaces],
        format_value=lambda v: _format_marketplace_value(config, v),
    )

    summary = _format_overlay_footer_summary(resolution)
    if lines and summary:
        lines.append(summary)
    return lines


def _emit_overlay_section(
    lines: list[str],
    *,
    header: str,
    entries: "list[tuple[str, OverlayOrigin]]",
    format_value: "Callable[[str], str]",
) -> None:
    """Append SPEC 2's per-axis block to ``lines`` when ``entries`` has any rows.

    Suppresses the section entirely when ``entries`` is empty so an
    axis untouched by both profile and local.yaml does not surface a
    bare header. Mockup line shapes:

    - ``+ value [from local.yaml]`` for LOCAL_ADD.
    - ``+ value`` (no tag) for PROFILE.
    - U+2212 prefix + value + remove tag for LOCAL_REMOVE.
    """
    from setforge.local_overlay import OverlayOrigin, display_tag

    if not entries:
        return
    lines.append("")
    lines.append(header)
    for value, origin in entries:
        formatted = format_value(value)
        tag = display_tag(origin)
        marker = chr(0x2212) if origin is OverlayOrigin.LOCAL_REMOVE else "+"
        suffix = f" {tag}" if tag else ""
        lines.append(f"  {marker} {formatted}{suffix}")


def _format_marketplace_value(config: Config, name: str) -> str:
    """Render a marketplace entry as ``name {source: ..., repo|path: ...}``.

    Pulls source details from ``cfg.marketplaces`` (mutated in place by
    the loader to include local-added marketplaces). Drops to bare
    ``name`` when the marketplace key is absent (defensive — should
    not happen post-mutation, but keeps the renderer total).
    """
    mp = config.marketplaces.get(name)
    if mp is None:
        return name
    if mp.repo is not None:
        return f"{name} {{source: {mp.source.value}, repo: {mp.repo}}}"
    return f"{name} {{source: {mp.source.value}, path: {mp.path}}}"


def _format_overlay_footer_summary(
    resolution: "LocalOverlayResolution",
) -> str | None:
    """Return the SPEC 2 ``[Host overlay summary: ...]`` line, or ``None``.

    Returns ``None`` when no axis carries any LOCAL_ADD / LOCAL_REMOVE
    entry (the caller suppresses the line). Per-axis counts render as
    ``plugins N+/M-`` / ``extensions N+/M-`` / ``marketplaces N+/M-``
    with the minus character at U+2212 (decimal 8722) for column-width
    parity with the per-row remove markers.
    """
    from setforge.local_overlay import OverlayOrigin

    def _counts(entries: "_OverlayResolvedEntries") -> tuple[int, int]:
        adds = sum(1 for e in entries if e.origin is OverlayOrigin.LOCAL_ADD)
        rems = sum(1 for e in entries if e.origin is OverlayOrigin.LOCAL_REMOVE)
        return adds, rems

    p_add, p_rem = _counts(resolution.plugins)
    e_add, e_rem = _counts(resolution.extensions)
    m_add, m_rem = _counts(resolution.marketplaces)
    if not (p_add or p_rem or e_add or e_rem or m_add or m_rem):
        return None
    minus = chr(0x2212)
    return (
        f"[Host overlay summary: "
        f"plugins {p_add}+/{p_rem}{minus}; "
        f"extensions {e_add}+/{e_rem}{minus}; "
        f"marketplaces {m_add}+/{m_rem}{minus} via local.yaml]"
    )


_DRIFT_CLASS_STYLES: dict[DriftClass, str] = {
    DriftClass.EXPECTED: "dim cyan",
    DriftClass.STALE: "yellow",
    DriftClass.UNEXPECTED: "bold red",
    DriftClass.CONFLICTED: "bold red",
}


def compare_summary_table(report: CompareReport) -> Table:
    """Build a rich :class:`~rich.table.Table` summarising the compare report.

    One row per ``DRIFTED`` entry with columns ``File`` / ``Disposition`` /
    ``Class`` / ``Why``. ``Class`` is the entry's :class:`DriftClass`
    (expected in dim cyan, stale in yellow, unexpected/conflicted in bold
    red); ``Why`` carries the class's reason note when it has one.
    """
    table = Table(title="Drift Summary", show_header=True, header_style="bold")
    table.add_column("File")
    table.add_column("Disposition")
    table.add_column("Class")
    table.add_column("Why")

    for entry in report.entries:
        if entry.status != CompareStatus.DRIFTED:
            continue
        disposition = entry.disposition.value if entry.disposition is not None else ""
        if entry.drift_class is not None:
            style = _DRIFT_CLASS_STYLES[entry.drift_class]
            class_str = f"[{style}]{entry.drift_class.value}[/{style}]"
        else:
            class_str = ""
        table.add_row(entry.name, disposition, class_str, entry.reason or "")

    return table
