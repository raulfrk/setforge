"""Drift compare for tracked → live deployments.

Two-axis classification:

- ``preserve_user_keys`` paths in YAML files mark drift that we *expect*
  (live overlays tracked on the next deploy, by design).
- Everything else is *unexpected* drift — what ``compare --check`` flags
  for CI and what Pillar 4's ``merge`` wizard exists to resolve.

Orphan detection (:func:`detect_orphans`, :class:`OrphanEntry`) is a
separate axis surfaced alongside drift: live files setforge previously
deployed (per ``transitions/*/meta.json`` ``paths``) that are no longer
listed in any resolved tracked_files entry. The ``cleanup-orphans``
subcommand re-computes orphans under ``--apply`` and removes them.
"""

import difflib
import io
import json
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from jinja2 import Template
from rich.table import Table
from ruamel.yaml import YAML

from setforge import host_local_inject, jsonc, section_reconcile, sections, yaml_merge
from setforge.binaries import LOCAL_CONFIG_PATH
from setforge.config import Config, ResolvedProfile, TrackedFile, resolve_profile
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


@dataclass(frozen=True, slots=True)
class FileCompare:
    name: str
    status: CompareStatus
    diff: str
    expected_drift_keys: list[str]
    unexpected_drift_keys: list[str]
    mode_drift: bool = False
    """True when the tracked_file declares ``mode:`` and the live file's
    permission bits (via :func:`stat.S_IMODE`) differ. Always False when
    ``mode:`` is unset — the drift axis is opt-in per tracked_file.
    """


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
    preserve_user_sections: bool = False,
    preserve_user_keys: list[str] | None = None,
    preserve_user_keys_deep: list[str] | None = None,
    host_local_sections: dict[HostLocalSectionName, HostLocalSection] | None = None,
) -> str:
    """Return the unified diff between ``src`` and ``dst``.

    When preservation is enabled the comparison renders the post-merge
    content (same merge sequence as :func:`setforge.deploy.copy_atomic`)
    so preserved drift never shows in the diff body.

    When ``host_local_sections`` is non-empty, the rendered ``src`` is
    augmented with the same host-local injection deploy would perform,
    so a live file that already received its host-local sections does
    NOT show up as drift (compare overlay-aware path).

    Fast path: with ``preserve_user_sections=True`` AND no host-local
    sections to inject, if every section's sha256 matches between src
    and dst AND the non-section content is byte-identical, the rendered
    merge would equal live — skip the splice + diff and return ``""``
    early. When host_local_sections is non-empty the fast path is
    skipped because the rendered src would carry MORE markers than the
    raw src.
    """
    if not dst.exists():
        return ""

    dst_text = dst.read_text(encoding="utf-8")
    if preserve_user_sections and not host_local_sections:
        src_text = src.read_text(encoding="utf-8")
        # Live side is parsed with allow_legacy=True so install's
        # pre-deploy compare step survives a pre-hash user file. The
        # compare CLI command surfaces a user-actionable error via
        # ``cli._refuse_legacy_live_markers`` BEFORE reaching here when
        # invoked directly; this branch is reached only from install's
        # drift gate, where lenience is correct.
        bodies_match = sections.hash_sections(src_text) == sections.hash_sections(
            dst_text, allow_legacy=True
        )
        template_matches = sections.strip_section_content(
            src_text, allow_legacy=True
        ) == sections.strip_section_content(dst_text, allow_legacy=True)
        if bodies_match and template_matches:
            return ""

    rendered_src = _render_with_merges(
        src,
        dst,
        preserve_user_sections,
        preserve_user_keys,
        preserve_user_keys_deep,
        dst_text=dst_text,
        host_local_sections=host_local_sections,
    )
    diff_lines = difflib.unified_diff(
        dst_text.splitlines(keepends=True),
        rendered_src.splitlines(keepends=True),
        fromfile=str(dst),
        tofile=str(src),
    )
    return "".join(diff_lines)


def _render_with_merges(
    src: Path,
    dst: Path,
    preserve_user_sections: bool,
    preserve_user_keys: list[str] | None,
    preserve_user_keys_deep: list[str] | None = None,
    *,
    dst_text: str,
    host_local_sections: dict[HostLocalSectionName, HostLocalSection] | None = None,
) -> str:
    """Render the post-merge tracked content that ``diff_file`` compares
    against the live ``dst_text``.

    Cache shape: takes ``dst_text`` as raw text (not pre-extracted
    sections) because ``diff_file`` upstream needs the raw text for
    its strip-template comparison (``strip_section_content(src) ==
    strip_section_content(dst_text)``) and for the ``difflib.unified_diff``
    input — so any parsed-shape cache would force ``diff_file`` to keep
    raw bytes around anyway. The symmetric deploy-side helper
    (:func:`setforge.deploy._compute_content`) caches the pre-extracted
    ``LiveSections`` instead because deploy has no strip-template need.
    See also: that function's docstring for the symmetric rationale.
    """
    shallow = preserve_user_keys or []
    deep = preserve_user_keys_deep or []
    if (shallow or deep) and jsonc.is_jsonc_file(src):
        tracked_text = src.read_text(encoding="utf-8")
        live_text = dst_text
        content = jsonc.overlay_user_keys(
            tracked_text, live_text, shallow, deep_key_names=deep
        )
    elif shallow or deep:
        yaml = YAML(typ="rt")
        with src.open("r", encoding="utf-8") as fh:
            src_doc = yaml.load(fh)
        with dst.open("r", encoding="utf-8") as fh:
            live_doc = yaml.load(fh)
        merged = yaml_merge.overlay(src_doc, live_doc, shallow, deep_key_paths=deep)
        buf = io.StringIO()
        yaml.dump(merged, buf)
        content = buf.getvalue()
    else:
        content = src.read_text(encoding="utf-8")

    if preserve_user_sections:
        # See ``diff_file`` above for the ``allow_legacy=True`` rationale.
        live_sections = sections.extract_sections(dst_text, allow_legacy=True)
        content = sections.merge_sections(content, live_sections)
        if host_local_sections:
            content = host_local_inject.inject_all(content, host_local_sections)
            content = section_reconcile.maintain_marker_hashes(content)
    return content


def classify_yaml_drift(
    src: Path,
    dst: Path,
    preserve_user_keys: list[str],
    preserve_user_keys_deep: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Return ``(expected, unexpected)`` JSONPath-lite paths where ``src``
    and ``dst`` diverge.

    A diverged path is *expected* iff covered by some entry in
    ``preserve_user_keys`` (shallow whole-leaf overlay, exact match or
    a parent path with ``[*]``/``[]``) OR by some entry in
    ``preserve_user_keys_deep`` (deep-merge overlay; any sub-path
    beneath a deep entry classifies as expected because deploy
    reconciles them at deep-merge time). Everything else is *unexpected*.
    """
    yaml = YAML(typ="rt")
    with src.open("r", encoding="utf-8") as fh:
        src_doc = yaml.load(fh)
    with dst.open("r", encoding="utf-8") as fh:
        live_doc = yaml.load(fh)

    diverged_paths = _diff_paths(src_doc, live_doc)
    preserve_prefixes = [_to_prefix(p) for p in preserve_user_keys]
    preserve_prefixes.extend(_to_prefix(p) for p in preserve_user_keys_deep or [])

    expected: list[str] = []
    unexpected: list[str] = []
    for path in diverged_paths:
        formatted = _format_path(path)
        if any(_is_prefix(prefix, path) for prefix in preserve_prefixes):
            expected.append(formatted)
        else:
            unexpected.append(formatted)
    return expected, unexpected


def _to_prefix(preserve_path: str) -> tuple[str, ...]:
    tokens = yaml_merge._parse_path(preserve_path)
    return tuple(name for _, name in tokens)


def _is_prefix(prefix: tuple[str, ...], path: tuple) -> bool:
    if len(path) < len(prefix):
        return False
    for prefix_step, path_step in zip(prefix, path, strict=False):
        if isinstance(path_step, int):
            return False
        if path_step != prefix_step:
            return False
    return True


def _diff_paths(src: object, live: object, prefix: tuple = ()) -> list[tuple]:
    if isinstance(src, Mapping) and isinstance(live, Mapping):
        diffs: list[tuple] = []
        for key in set(src) | set(live):
            if key not in src or key not in live:
                diffs.append((*prefix, key))
                continue
            diffs.extend(_diff_paths(src[key], live[key], (*prefix, key)))
        return diffs
    if isinstance(src, list) and isinstance(live, list):
        diffs = []
        for i in range(max(len(src), len(live))):
            if i >= len(src) or i >= len(live):
                diffs.append((*prefix, i))
                continue
            diffs.extend(_diff_paths(src[i], live[i], (*prefix, i)))
        return diffs
    if src != live:
        return [prefix]
    return []


def _format_path(path: tuple) -> str:
    out: list[str] = []
    for i, step in enumerate(path):
        if isinstance(step, int):
            out.append(f"[{step}]")
        elif i == 0:
            out.append(str(step))
        else:
            out.append(f".{step}")
    return "".join(out) or "<root>"


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
    host_local_sections: dict[HostLocalSectionName, HostLocalSection] | None = None,
) -> tuple[FileCompare, bool]:
    # Symlink-aware tracked_files dispatch FIRST: ``Path.exists()`` returns
    # False on a dangling symlink, which would otherwise misclassify the
    # case as MISSING. ``_compare_symlinked`` probes ``is_symlink()`` first
    # so dangling links surface as DRIFTED (target drift / broken link)
    # rather than MISSING.
    if tracked_file.symlink is not None:
        return _compare_symlinked(
            name, src, dst, tracked_file, host_local_sections=host_local_sections
        )

    if not dst.exists():
        return (
            FileCompare(
                name=name,
                status=CompareStatus.MISSING,
                diff="",
                expected_drift_keys=[],
                unexpected_drift_keys=[],
            ),
            True,
        )

    diff = diff_file(
        src,
        dst,
        preserve_user_sections=tracked_file.preserve_user_sections,
        preserve_user_keys=tracked_file.preserve_user_keys or None,
        preserve_user_keys_deep=tracked_file.preserve_user_keys_deep or None,
        host_local_sections=host_local_sections,
    )

    expected_keys: list[str] = []
    unexpected_keys: list[str] = []
    if tracked_file.preserve_user_keys or tracked_file.preserve_user_keys_deep:
        if jsonc.is_jsonc_file(src):
            expected_keys, unexpected_keys = jsonc.classify_jsonc_drift(
                src.read_text(encoding="utf-8"),
                dst.read_text(encoding="utf-8"),
                tracked_file.preserve_user_keys,
                deep_key_names=tracked_file.preserve_user_keys_deep,
            )
        else:
            expected_keys, unexpected_keys = classify_yaml_drift(
                src,
                dst,
                tracked_file.preserve_user_keys,
                preserve_user_keys_deep=tracked_file.preserve_user_keys_deep,
            )

    mode_drift = False
    if tracked_file.mode is not None:
        # lstat (not stat) for symlink-posture parity with snapshots.py:
        # a symlink dst reports drift on the LINK's own mode, never the
        # target's — setforge never deploys through a live symlink.
        live_mode = stat.S_IMODE(dst.lstat().st_mode)
        mode_drift = live_mode != tracked_file.mode

    is_drifted = (
        bool(diff) or bool(expected_keys) or bool(unexpected_keys) or mode_drift
    )
    status = CompareStatus.DRIFTED if is_drifted else CompareStatus.UNCHANGED

    return (
        FileCompare(
            name=name,
            status=status,
            diff=diff,
            expected_drift_keys=expected_keys,
            unexpected_drift_keys=unexpected_keys,
            mode_drift=mode_drift,
        ),
        bool(diff) or mode_drift,
    )


def _compare_symlinked(
    name: str,
    src: Path,
    dst: Path,
    tracked_file: TrackedFile,
    *,
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
    if not dst.is_symlink():
        if dst.exists():
            return (
                FileCompare(
                    name=name,
                    status=CompareStatus.DRIFTED,
                    diff=(
                        f"expected symlink to {expected!r}, found regular file at {dst}"
                    ),
                    expected_drift_keys=[],
                    unexpected_drift_keys=[],
                ),
                True,
            )
        return (
            FileCompare(
                name=name,
                status=CompareStatus.MISSING,
                diff="",
                expected_drift_keys=[],
                unexpected_drift_keys=[],
            ),
            True,
        )
    actual = os.readlink(dst)
    if actual != expected:
        return (
            FileCompare(
                name=name,
                status=CompareStatus.DRIFTED,
                diff=(f"symlink target drift at {dst}: {actual!r} != {expected!r}"),
                expected_drift_keys=[],
                unexpected_drift_keys=[],
            ),
            True,
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
        preserve_user_sections=tracked_file.preserve_user_sections,
        preserve_user_keys=tracked_file.preserve_user_keys or None,
        preserve_user_keys_deep=tracked_file.preserve_user_keys_deep or None,
        host_local_sections=host_local_sections,
    )
    if target_diff:
        return (
            FileCompare(
                name=name,
                status=CompareStatus.DRIFTED,
                diff=target_diff,
                expected_drift_keys=[],
                unexpected_drift_keys=[],
            ),
            True,
        )

    return (
        FileCompare(
            name=name,
            status=CompareStatus.UNCHANGED,
            diff="",
            expected_drift_keys=[],
            unexpected_drift_keys=[],
        ),
        False,
    )


def render_preserve_user_keys_overlay_block(
    config: Config, resolved: ResolvedProfile
) -> list[str]:
    """Build mockup-B compare-output lines for the preserve_user_keys overlay.

    Returns an empty list when no tracked_file in the resolved profile
    carries any FROM_LOCAL_YAML or REMOVED_VIA_LOCAL entry — the
    overlay block is suppressed when local.yaml introduces no change.
    Otherwise returns the verbatim lines mockup B specifies (SPEC 8
    spec lines 109-118): a top-level ``=== applying host overlay`` header,
    a count line, then one indented block per affected tracked_file
    with one provenance-tagged row per key.

    Pure function — the caller (compare/install CLI) prints each line
    so test fixtures can assert on string content directly.
    """
    from setforge.preserved_keys import (
        KeyOrigin,
        display_tag,
        has_local_yaml_overlay,
    )

    affected: list[tuple[str, TrackedFile]] = []
    for name in resolved.tracked_files:
        tf = config.tracked_files[name]
        if has_local_yaml_overlay(tf.preserve_user_keys_resolved):
            affected.append((name, tf))
    if not affected:
        return []

    lines: list[str] = []
    lines.append("=== applying host overlay (~/.config/setforge/local.yaml) ===")
    plural = "s" if len(affected) != 1 else ""
    lines.append(f"tracked_files overlays: {len(affected)} file{plural} affected")
    for name, tf in affected:
        lines.append(f"  {name}:")
        lines.append("    preserve_user_keys effective set:")
        for key in tf.preserve_user_keys_resolved:
            match key.origin:
                case KeyOrigin.FROM_LOCAL_YAML:
                    marker = "+"
                case KeyOrigin.REMOVED_VIA_LOCAL:
                    # Unicode minus sign (mockup B uses U+2212), keeps
                    # the column-width parity with the + and = markers
                    # for the multi-line rendering.
                    marker = "−"  # noqa: RUF001 — U+2212 MINUS SIGN per mockup B.
                case KeyOrigin.FROM_PROFILE:
                    marker = "="
            lines.append(f"      {marker} {key.key}  {display_tag(key)}")
    return lines


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


def compare_summary_table(report: CompareReport) -> Table:
    """Build a rich :class:`~rich.table.Table` summarising the compare report.

    One row per ``DRIFTED`` entry with columns: ``file``, ``expected drift``,
    ``unexpected drift``. Expected-drift counts render in dim cyan; unexpected
    in bold red when > 0.
    """
    table = Table(title="Drift Summary", show_header=True, header_style="bold")
    table.add_column("file")
    table.add_column("expected drift", justify="right")
    table.add_column("unexpected drift", justify="right")

    for entry in report.entries:
        if entry.status != CompareStatus.DRIFTED:
            continue
        exp_count = len(entry.expected_drift_keys)
        unexp_count = len(entry.unexpected_drift_keys)
        exp_str = f"[dim cyan]{exp_count}[/dim cyan]"
        if unexp_count > 0:
            unexp_str = f"[bold red]{unexp_count}[/bold red]"
        else:
            unexp_str = str(unexp_count)
        table.add_row(entry.name, exp_str, unexp_str)

    return table
