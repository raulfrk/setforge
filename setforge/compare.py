"""Drift compare for tracked → live deployments.

Every ``DRIFTED`` file carries a :class:`DriftClass` explaining the drift:

- ``expected`` — intentional host divergence: a reconcile-staged file whose
  tracked side holds exactly the shared set while local edits are kept
  host-only.
- ``stale`` — live still equals the stored base while tracked advanced;
  the next install fast-forwards live. Not flagged by ``compare --check``.
- ``unexpected`` — drift nothing above explains: what ``compare --check``
  flags for CI and what the install drift gate (``--auto-accept-*``)
  resolves.

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
)
from setforge.binaries import LOCAL_CONFIG_PATH
from setforge.config import (
    Config,
    ResolvedProfile,
    TrackedFile,
    resolve_and_expand,
)
from setforge.errors import BaseStoreError, ConfigError
from setforge.file_ownership import FileAction, decide_file, observe_file
from setforge.generated import rendered_source
from setforge.home_confinement import is_outside_home, warn_outside_home_dst
from setforge.ownership import OwnershipError, OwnershipStore, read_owner_id
from setforge.paths import template_context
from setforge.source import HostLocalSection, HostLocalSectionName

if TYPE_CHECKING:
    from setforge.config import HostLocalTrackedFileOverride, LocalOverlayResolution
    from setforge.overlay_provenance import (
        OverlayOrigin,
        ResolvedExtension,
        ResolvedMarketplace,
        ResolvedPlugin,
    )
    from setforge.reconcile.structured_units import StructuredFormat

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


_STALE_REASON = "tracked advanced since last install — install will update"

_STAGED_REASON = (
    "staged: tracked holds exactly the shared set; local edits kept host-only"
)


@dataclass(frozen=True, slots=True)
class FileCompare:
    """Per-file drift result from :func:`compare_profile`.

    The derived property ``drift_is_expected`` is ``True`` when drift is
    classified as intentional host divergence (today: only via the reconcile
    engine's per-unit staging). All other drift is *not* expected (needs
    attention).
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
    span_only_drift: bool = False
    """Vestigial after the spans retirement — always ``False`` (no tracked-side
    spans remain to confine drift to). Retained on the record so downstream
    consumers keep a stable shape.
    """
    drift_class: DriftClass | None = None
    """Why the file drifted, per :func:`_classify_drifted`. ``None`` unless
    ``status`` is ``DRIFTED``.
    """
    reason: str | None = None
    """Human-readable note for the drift class (the summary table's ``Why``
    column). ``None`` when the class needs no elaboration.
    """

    @property
    def drift_is_expected(self) -> bool:
        """True when the file's drift is classified as intentionally expected.

        After the disposition/spans retirement this axis is no longer driven by
        a file-level disposition; the reconcile engine classifies expected
        (staged) drift directly in :func:`_classify_drifted`, so this property
        stays ``False`` for every file.
        """
        return False


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
    orphan_skipped_unmanaged: int = 0
    orphan_skipped_host_local: int = 0


@dataclass(frozen=True, slots=True)
class OrphanDetection:
    """Result of :func:`detect_orphans`.

    ``orphans`` is the kept set (deployed dst paths that still exist on
    disk and are no longer tracked). ``skipped_absent`` /
    ``skipped_source`` / ``skipped_unmanaged`` tally the candidates the
    guards filtered out — a path no longer on disk, a path that is a
    tracked SOURCE rather than a deployed dst, or a path outside every
    currently-managed destination root (e.g. an unrelated config under
    a retired profile, or stray ``/tmp`` scratch) — so the CLI can
    surface a transparency note.
    """

    orphans: list[OrphanEntry]
    skipped_absent: int = 0
    skipped_source: int = 0
    skipped_unmanaged: int = 0
    skipped_host_local: int = 0


def _norm(path: Path) -> Path:
    """Lexically normalize ``path`` (expand ``~``, collapse ``.``/``..``).

    Purely lexical via :func:`os.path.normpath` — NEVER resolves
    symlinks. A candidate orphan that is a symlink must reach ``unlink``
    un-dereferenced; resolving first would target the pointed-to file.
    Applied to BOTH sides of every orphan comparison so relative / ``~``
    / ``..`` aliasing cannot make a guard fail open.
    """
    return Path(os.path.normpath(path.expanduser()))


# Generic shared destination roots that must NEVER count as a managed
# orphan-scope root. Climbing a tracked dst's ancestors (see
# :func:`_managed_dst_roots`) stops before any of these, so an unrelated
# path living under, e.g., ``~/.config`` or ``/tmp`` is never pulled into
# orphan scope. This denylist is the backstop against the orphan
# over-reach class — a path setforge deployed under a now-retired config,
# or stray ``/tmp`` scratch, must not resurface as a deletable orphan.
# Named constant (not inlined) so the set is auditable in one place.
GENERIC_DST_ROOTS: frozenset[Path] = frozenset(
    _norm(Path(raw))
    for raw in (
        "~",
        "~/.config",
        "~/.local",
        "~/.local/state",
        "~/.local/share",
        "~/.cache",
        "/tmp",
        "/var",
        "/var/tmp",
        "/etc",
        "/usr",
        "/opt",
        "/",
    )
)


# Host-local files setforge WRITES itself (the local.yaml overlay via
# ``ensure_local_config_stub``; the ``~/.claude/additional-content.md`` stub)
# but NEVER deploys from a tracked source. Such a file can land in a
# transition's touched paths AND under a managed dst root — e.g. ``local.yaml``
# lives in ``~/.config/setforge/``, which a sibling tracked dst
# (``claude-canary.sh``) makes a managed root — yet reaping it destroys the
# user's host-local state. The root-level GENERIC_DST_ROOTS denylist cannot
# catch it (its directory is a legitimate managed root), so it is excluded here
# at file granularity. See setforge/binaries.py for LOCAL_CONFIG_PATH.
HOST_LOCAL_FILES: frozenset[Path] = frozenset(
    _norm(p)
    for p in (
        LOCAL_CONFIG_PATH,
        Path.home() / ".claude" / "additional-content.md",
    )
)


def _host_local_files(config: Config) -> frozenset[Path]:
    """Every file setforge WRITES but never deploys from a tracked source.

    The :data:`HOST_LOCAL_FILES` floor (``local.yaml`` + the
    ``additional-content.md`` stub, bootstrapped independently of any profile)
    UNIONED with every profile's ``bootstrap`` dst — each ``touch``ed by
    :func:`setforge.deploy.bootstrap_local` and recorded in the transition
    ledger, yet never a tracked deployment (e.g. ``~/.claude/header.md``).
    Unioned across ALL profiles (like :func:`_managed_dst_roots`) so a
    bootstrap stub left by a retired sibling profile is excluded too. Reaping
    any of these via ``cleanup-orphans`` is data loss.
    """
    paths = set(HOST_LOCAL_FILES)
    for profile in config.profiles.values():
        paths.update(_norm(p) for p in profile.bootstrap)
    return frozenset(paths)


def _managed_dst_roots(config: Config, repo_root: Path) -> set[Path]:
    """Directories setforge currently deploys into — the orphan scope.

    For every tracked_file dst in the WHOLE config (all profiles,
    directory-expanded via :func:`expand_tracked_file`), walk its
    ancestor directories and collect each one until a generic shared root
    (:data:`GENERIC_DST_ROOTS`) or the filesystem root is reached. A
    candidate orphan is kept only if it lives under one of these roots
    (see :func:`detect_orphans`).

    This is what stops the over-reach: a path setforge deployed under a
    now-retired config (e.g. ``~/.config/worktrunk/config.toml`` left by
    an old profile) reaches a current tracked dst only via the generic
    ``~/.config`` ancestor — which is never added — so it is out of
    scope, while a file removed from a still-managed tree (e.g.
    ``~/.claude/skills/<gone>/SKILL.md``, sharing the managed ``~/.claude``
    ancestor with surviving dsts) still surfaces. Built from ALL
    ``config.tracked_files`` (not just the resolved profile) so a path
    deployed by a sibling profile is still recognized as managed.
    """
    roots: set[Path] = set()
    for name, tracked_file in config.tracked_files.items():
        src = resolve_src(tracked_file, repo_root)
        dst = resolve_dst(tracked_file)
        for _, _, sub_dst in expand_tracked_file(name, src, dst):
            for ancestor in _norm(sub_dst).parents:
                if ancestor in GENERIC_DST_ROOTS or ancestor == ancestor.parent:
                    break
                roots.add(ancestor)
    return roots


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
    belonging to a tracked_file outside the resolved profile. A ``src``
    that escapes ``repo_root/tracked`` is refused by :func:`resolve_src`
    before it can reach this set.
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
    set of currently-resolved tracked destinations, then applies three
    guards so the result can never schedule a non-orphan for deletion:

    1. **Source guard** — a candidate under ``repo_root/tracked`` or
       equal to any tracked_file's resolved SRC is dropped. A source
       file is never a deployed dst, so it can never be an orphan;
       without this a stale ``meta.json`` that recorded src paths would
       list the config source of truth for deletion.
    2. **Managed-scope guard** — a candidate that does not live under any
       currently-managed destination root (:func:`_managed_dst_roots`) is
       dropped. The transition ``paths`` ledger records every path any
       command touched — installs, migrations (which rewrite the source
       manifest), and scratch paths from local test/dogfood runs (e.g.
       under ``/tmp``) — so ledger-membership alone is
       too broad. Restricting to managed roots is what keeps the source
       manifest, configs left by retired profiles, and stray ``/tmp``
       paths out of the WOULD-delete list while still surfacing a file
       removed from a still-managed tree.
    3. **Existence gate** — a candidate no longer present on disk is
       dropped. Uses :func:`os.path.lexists` (lstat semantics) to match
       the apply path's ``_lstat_safe`` delete check, so a dangling
       symlink (still a real, deletable dir entry) is RETAINED while a
       fully-absent path is filtered. The report then equals exactly
       what ``--apply`` would delete.

    The source and managed-scope guards run BEFORE the existence gate so
    an excluded path that happens to exist on disk is tallied as a skip,
    not leaked through. Containment uses :func:`Path.is_relative_to`
    (component-wise — ``/foo`` never matches ``/foobar``) over lexically
    normalized paths on both sides; no ``resolve()`` is applied, matching
    how setforge records paths. ``ignored`` is a set of tracked_file IDs
    the user marked "keep orphan" via ``cleanup-orphans --ignore <id>``;
    their resolved destinations join the tracked set so they never
    surface. Returns an :class:`OrphanDetection` carrying the kept
    orphans and the per-guard skip tallies.
    """
    tracked_paths = _resolved_tracked_dsts(
        resolved, config, repo_root, extra_ids=ignored
    )
    touched_paths = _touched_paths_from_meta(transitions_dir)
    src_root = _norm(repo_root / "tracked")
    src_paths = _tracked_source_paths(config, repo_root)
    managed_roots = _managed_dst_roots(config, repo_root)

    host_local = _host_local_files(config)
    kept: list[OrphanEntry] = []
    skipped_absent = 0
    skipped_source = 0
    skipped_unmanaged = 0
    skipped_host_local = 0
    for path in sorted(touched_paths - tracked_paths, key=str):
        if _norm(path) in host_local:
            # setforge-written host-local state (the local.yaml/additional-content
            # stubs + every profile's bootstrap dst) — never a tracked
            # deployment, excluded up front so a file that happens to live under
            # a managed root is never reaped. Data-loss guard; see
            # _host_local_files.
            skipped_host_local += 1
            continue
        if path.is_relative_to(src_root) or path in src_paths:
            skipped_source += 1
            continue
        if not any(path.is_relative_to(root) for root in managed_roots):
            skipped_unmanaged += 1
            continue
        if not os.path.lexists(path):
            skipped_absent += 1
            continue
        kept.append(OrphanEntry(path=path))
    return OrphanDetection(
        orphans=kept,
        skipped_absent=skipped_absent,
        skipped_source=skipped_source,
        skipped_unmanaged=skipped_unmanaged,
        skipped_host_local=skipped_host_local,
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
    absolute path inside the repo.

    Refuses a ``src`` that escapes ``tracked/`` — via an absolute path or
    a ``..`` climb — because the pre-deploy gitleaks sweep scans only
    ``tracked/``, so an out-of-tree src would deploy content the secrets
    sweep never saw. Mirrors the bundle-component guard in
    :func:`setforge.config._gate_component_paths`.
    """
    tracked_root = (repo_root / "tracked").resolve()
    resolved_src = (tracked_root / tracked_file.src).resolve()
    if resolved_src != tracked_root and tracked_root not in resolved_src.parents:
        raise ConfigError(
            f"tracked_file src {tracked_file.src} resolves to {resolved_src} "
            f"— outside {tracked_root}; a src outside tracked/ bypasses the "
            "gitleaks secrets sweep."
        )
    return repo_root / "tracked" / tracked_file.src


def resolve_dst(tracked_file: TrackedFile) -> Path:
    """Resolve a tracked_file's ``dst`` template (if any) to an absolute path
    via Jinja2 + ``~`` expansion."""
    raw = tracked_file.dst
    if tracked_file.template:
        raw = Template(raw).render(**template_context())
    return Path(raw).expanduser()


def warn_if_dst_outside_home(tracked_file: TrackedFile, dst: Path) -> None:
    """Warn — do NOT refuse — on an out-of-$HOME dst (self-trust: still deploys)."""
    if tracked_file.allow_outside_home:
        return
    home = Path.home().resolve()
    resolved = dst.resolve()
    if is_outside_home(resolved, home):
        warn_outside_home_dst(
            label="tracked_file",
            raw_dst=tracked_file.dst,
            resolved=resolved,
            home=home,
            silence_hint="set allow_outside_home: true on this tracked_file",
        )


def diff_file(
    src: Path,
    dst: Path,
    tracked_file: TrackedFile | None = None,
) -> str:
    """Return the unified diff between ``src`` (tracked) and ``dst`` (live).

    For a ``disposition=None`` tracked_file the deployed content is ``src``
    verbatim, so the comparison is a plain unified diff.
    """
    if not dst.exists():
        return ""

    dst_text = dst.read_text(encoding="utf-8")
    rendered_src = rendered_source(
        src, tracked_file.generated if tracked_file is not None else None
    )
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
    ownership_authorized: Mapping[str, bool] | None = None,
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
    ``{tracked_file_id: {section_name: HostLocalSection}}`` (SPEC 1). It is
    threaded through to :func:`_compare_one` for caller symmetry but no longer
    alters the diff: host-local content is now owned by the reconcile engine,
    so :func:`diff_file` performs a plain verbatim comparison. The CLI surface
    (:func:`setforge.cli.compare.compare`) loads + validates the map
    via :func:`setforge.cli._install_helpers._load_validated_host_local_sections`
    before passing it in; callers that don't carry an overlay (e.g.
    the orphan-detection and status commands) pass ``None``.

    The CLI resolves the effective profile first, mutating ``config`` with
    host-local tracked-file paths. This helper re-expands the tracked-file list
    idempotently and reads those already-effective definitions; plugin and
    extension lists are irrelevant to its file-only comparison.
    """
    resolved = resolve_and_expand(config, profile_name, repo_root)
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
                ownership_authorized=(
                    ownership_authorized.get(sub_name, False)
                    if ownership_authorized is not None
                    else _has_file_authority(repo_root, sub_dst)
                ),
            )
            entries.append(entry)
            if sub_unexpected:
                has_unexpected = True

    orphans: list[OrphanEntry] = []
    skipped_absent = 0
    skipped_source = 0
    skipped_unmanaged = 0
    skipped_host_local = 0
    if transitions_dir is not None:
        detection = detect_orphans(
            resolved, config, transitions_dir, repo_root, ignored=ignored
        )
        orphans = detection.orphans
        skipped_absent = detection.skipped_absent
        skipped_source = detection.skipped_source
        skipped_unmanaged = detection.skipped_unmanaged
        skipped_host_local = detection.skipped_host_local

    return CompareReport(
        entries=entries,
        has_unexpected_drift=has_unexpected,
        orphans=orphans,
        orphan_skipped_absent=skipped_absent,
        orphan_skipped_source=skipped_source,
        orphan_skipped_unmanaged=skipped_unmanaged,
        orphan_skipped_host_local=skipped_host_local,
    )


def file_authorization_map(
    config: Config, resolved: ResolvedProfile, repo_root: Path
) -> dict[str, bool]:
    """Project container authority before callers acquire profile locks."""
    try:
        owner_id = read_owner_id(repo_root)
    except OwnershipError:
        legacy = not (repo_root / ".git").exists()
        return {
            sub_name: legacy
            for name in resolved.tracked_files
            for sub_name, _src, _dst in expand_tracked_file(
                name,
                resolve_src(config.tracked_files[name], repo_root),
                resolve_dst(config.tracked_files[name]),
            )
        }
    store = OwnershipStore()
    result: dict[str, bool] = {}
    for name in resolved.tracked_files:
        tracked = config.tracked_files[name]
        for sub_name, _src, destination in expand_tracked_file(
            name, resolve_src(tracked, repo_root), resolve_dst(tracked)
        ):
            if tracked.symlink is not None:
                result[sub_name] = True
                continue
            observation = observe_file(destination)
            decision = decide_file(
                observation,
                store.read(observation.resource_id),
                owner_id=owner_id,
            )
            result[sub_name] = decision.action not in {
                FileAction.ADOPT,
                FileAction.HOLD,
            }
    return result


def _compare_one(
    name: str,
    src: Path,
    dst: Path,
    tracked_file: TrackedFile,
    *,
    profile: str | None = None,
    host_local_sections: dict[HostLocalSectionName, HostLocalSection] | None = None,
    ownership_authorized: bool = True,
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

    if not dst.exists():
        return (
            FileCompare(
                name=name,
                status=CompareStatus.MISSING,
                diff="",
            ),
            True,
        )

    diff = diff_file(src, dst, tracked_file)

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

    entry = FileCompare(
        name=name,
        status=status,
        diff=diff,
        mode_drift=mode_drift,
        live_mode=live_mode,
        tracked_mode=tracked_mode,
    )
    return _classify_entry(
        entry,
        profile=profile,
        src=src,
        dst=dst,
        tracked_file=tracked_file,
        ownership_authorized=ownership_authorized,
    )


def _classify_entry(
    entry: FileCompare,
    *,
    profile: str | None,
    src: Path,
    dst: Path,
    tracked_file: TrackedFile | None = None,
    probe_stale: bool = True,
    ownership_authorized: bool = True,
) -> tuple[FileCompare, bool]:
    """Attach the drift class to a ``DRIFTED`` entry and derive its
    unexpected flag.

    Non-``DRIFTED`` entries pass through with ``drift_class=None`` and an
    unexpected flag of ``False`` (a MISSING entry never reaches here — its
    caller returns the existing ``True`` contract directly).
    ``tracked_file`` feeds the reconcile-staged expected probe (slot 4b);
    ``None`` skips it — symlink-deployed files never carry a stored base
    (the base lifecycle is regular-file-only), so their callers omit it.
    """
    if entry.status is not CompareStatus.DRIFTED:
        return entry, False
    drift_class, reason = _classify_drifted(
        entry,
        profile=profile,
        src=src,
        dst=dst,
        tracked_file=tracked_file,
        probe_stale=probe_stale,
        ownership_authorized=ownership_authorized,
    )
    entry = replace(entry, drift_class=drift_class, reason=reason)
    is_unexpected = drift_class is DriftClass.UNEXPECTED
    return entry, is_unexpected


def _classify_drifted(
    entry: FileCompare,
    *,
    profile: str | None,
    src: Path,
    dst: Path,
    tracked_file: TrackedFile | None = None,
    probe_stale: bool = True,
    ownership_authorized: bool = True,
) -> tuple[DriftClass, str | None]:
    """Classify a ``DRIFTED`` entry; first matching slot wins.

    Returns ``(drift_class, reason)`` — the reason is the summary table's Why
    column note, ``None`` when the class needs no elaboration.

    ``probe_stale=False`` skips the base-store reads (slots 3-4b) for
    entries whose ``src``/``dst`` byte comparison is meaningless (symlink
    metadata drift). ``profile=None`` (direct unit-scope calls) also
    skips them — the stored base is keyed by profile.
    """
    if tracked_file is not None and tracked_file.generated is not None:
        return DriftClass.UNEXPECTED, "generated output differs from rendered intent"
    # Slot 3 — STALE: live still equals the stored base while tracked
    # advanced; the next install fast-forwards live.
    if probe_stale and profile is not None and _is_stale(profile, entry.name, src, dst):
        return DriftClass.STALE, _STALE_REASON
    # Slot 4b — EXPECTED: a reconcile-staged plain file (A5/A5c) whose tracked
    # holds EXACTLY the promoted set (INV-8 over base+live+hunks+drafts). The
    # live↔tracked diff is then the expected staging divergence — host-only
    # LOCAL/PENDING hunks and the host-specific side of a SHARED_DRAFTED hunk —
    # never unsynced drift. If tracked carries anything the promoted set does not
    # explain, INV-8 fails and the file falls through to UNEXPECTED below.
    if (
        probe_stale
        and profile is not None
        and tracked_file is not None
        and ownership_authorized
        and _reconcile_staged_expected(profile, entry.name, src, dst)
    ):
        return DriftClass.EXPECTED, _STAGED_REASON
    # Slot 5 — UNEXPECTED: drift nothing above explains.
    return DriftClass.UNEXPECTED, None


def _has_file_authority(repo_root: Path, destination: Path) -> bool:
    """Return whether staged units can explain shared tracked/live drift."""
    try:
        owner_id = read_owner_id(repo_root)
    except OwnershipError:
        return not (repo_root / ".git").exists()
    observation = observe_file(destination)
    decision = decide_file(
        observation,
        OwnershipStore().read(observation.resource_id),
        owner_id=owner_id,
    )
    return decision.action not in {FileAction.ADOPT, FileAction.HOLD}


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


def _reconcile_staged_expected(
    profile: str, file_id_str: str, src: Path, dst: Path
) -> bool:
    """True when a reconcile-staged plain file's tracked holds exactly the
    promoted set — so its live↔tracked diff is the expected staging divergence.

    Reconstructs the expected tracked content from ``base`` + the recorded
    classifications + the drafts manifest and asserts INV-8 against the on-disk
    tracked bytes. Returns ``False`` (→ the entry classifies as real/unexpected
    drift) when the file is not reconcile-staged (no base, or no classified
    hunks) OR when INV-8 fails (tracked carries something the promoted set does
    not explain — e.g. a hand-edit of tracked). Crash-free, mirroring
    :func:`_is_stale`: any store / filesystem / decode error degrades to
    ``False`` so the read-only compare never raises.
    """
    # Imported lazily (matching transitions._snapshot_target) to keep the module
    # graph acyclic — the reconcile package imports compare-adjacent helpers.
    from setforge.errors import InvariantViolation, ReconcileStoreError
    from setforge.reconcile import hunks as reconcile_hunks
    from setforge.reconcile import index_model
    from setforge.reconcile import store as reconcile_store
    from setforge.reconcile.structured_units import structured_format
    from setforge.reconcile.types import UnitKind
    from setforge.reconcile.types import file_id as make_file_id

    fmt = structured_format(dst)  # None for .jsonc — stays on this plain path
    if fmt is not None:
        return _reconcile_staged_expected_structured(
            profile, file_id_str, src, dst, fmt
        )

    try:
        fid = make_file_id(file_id_str)
        base = reconcile_store.read_base(profile, fid)
        if base is None:
            return False
        entry = reconcile_store.read_index(profile).files.get(file_id_str)
        if entry is None or not entry.staged:
            return False  # not A5-staged → not this slot's case
        live = dst.read_bytes()
        tracked = src.read_bytes()
        base.decode("utf-8")
        live.decode("utf-8")  # text-only staging
        hunks = reconcile_hunks.classify(
            reconcile_hunks.extract_hunks(base, live),
            index_model.require_unit_kind(entry.hunks, UnitKind.LINE),
        )
        drafts = reconcile_store.read_drafts(profile, fid)
        reconcile_hunks.assert_stage_fidelity(base, live, tracked, hunks, drafts)
        return True
    except InvariantViolation:
        return False  # INV-8 failed → tracked is NOT the promoted set → real drift
    except (
        BaseStoreError,
        ReconcileStoreError,
        OSError,
        UnicodeDecodeError,
        ValueError,
    ):
        return False  # degrade like _is_stale — never raise in read-only compare


def _reconcile_staged_expected_structured(
    profile: str,
    file_id_str: str,
    src: Path,
    dst: Path,
    fmt: "StructuredFormat",
) -> bool:
    from setforge.errors import (
        InvariantViolation,
        ReconcileStoreError,
        StructuredParseError,
    )
    from setforge.reconcile import index_model
    from setforge.reconcile import store as reconcile_store
    from setforge.reconcile import structured_units as su
    from setforge.reconcile.types import UnitKind
    from setforge.reconcile.types import file_id as make_file_id

    try:
        fid = make_file_id(file_id_str)
        base = reconcile_store.read_base(profile, fid)
        if base is None:
            return False
        entry = reconcile_store.read_index(profile).files.get(file_id_str)
        if entry is None or not entry.staged:
            return False
        live = dst.read_bytes()  # raw bytes, not re-parsed — INV-8 needs on-disk form
        tracked = src.read_bytes()
        fresh = su.extract_structured_units(base, live, fmt)
        units = su.classify_structured(
            fresh, index_model.require_unit_kind(entry.hunks, UnitKind.KEY), fmt
        )
        drafts = reconcile_store.read_drafts(profile, fid)
        su.assert_stage_fidelity_structured(base, live, tracked, units, drafts, fmt)
        return True
    except InvariantViolation:
        return False
    except (
        StructuredParseError,
        BaseStoreError,
        ReconcileStoreError,
        OSError,
        UnicodeDecodeError,
        ValueError,
    ):
        return False


def _span_only_drift(src: Path, dst: Path, tracked_file: TrackedFile) -> bool:
    """Always ``False`` after the disposition/spans retirement.

    Tracked-side spans were retired, so no drift can be confined to a span; the
    reconcile engine now owns per-unit host-divergence classification. Retained
    (returning ``False``) so callers keep a stable shape.
    """
    return False


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
    if not dst.is_symlink():
        if dst.exists():
            entry = FileCompare(
                name=name,
                status=CompareStatus.DRIFTED,
                diff=(f"expected symlink to {expected!r}, found regular file at {dst}"),
            )
            return _classify_entry(
                entry, profile=profile, src=src, dst=dst, probe_stale=False
            )
        return (
            FileCompare(
                name=name,
                status=CompareStatus.MISSING,
                diff="",
            ),
            True,
        )
    # str() keeps ``actual`` a plain string so the != compare against the
    # string ``expected`` (tracked_file.symlink) and the {actual!r} repr stay verbatim.
    actual = str(dst.readlink())
    if actual != expected:
        entry = FileCompare(
            name=name,
            status=CompareStatus.DRIFTED,
            diff=(f"symlink target drift at {dst}: {actual!r} != {expected!r}"),
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
    # Symlinked targets deploy verbatim (no host-local overlay injection), so a
    # plain diff against the tracked source is correct.
    target_diff = diff_file(src, target_path, tracked_file)
    if target_diff:
        entry = FileCompare(
            name=name,
            status=CompareStatus.DRIFTED,
            diff=target_diff,
        )
        return _classify_entry(
            entry,
            profile=profile,
            src=src,
            dst=dst,
            tracked_file=tracked_file,
            probe_stale=False,
        )

    return (
        FileCompare(
            name=name,
            status=CompareStatus.UNCHANGED,
            diff="",
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
    :func:`setforge.overlay_provenance.display_tag` (SoT for the wording).
    """
    from setforge.overlay_provenance import (
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
    from setforge.overlay_provenance import OverlayOrigin, display_tag

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
    from setforge.overlay_provenance import OverlayOrigin

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
}


def compare_summary_table(report: CompareReport) -> Table:
    """Build a rich :class:`~rich.table.Table` summarising the compare report.

    One row per ``DRIFTED`` entry with columns ``File`` / ``Class`` / ``Why``.
    ``Class`` is the entry's :class:`DriftClass` (expected in dim cyan, stale in
    yellow, unexpected in bold red); ``Why`` carries the class's
    reason note when it has one.
    """
    table = Table(title="Drift Summary", show_header=True, header_style="bold")
    table.add_column("File")
    table.add_column("Class")
    table.add_column("Why")

    for entry in report.entries:
        if entry.status != CompareStatus.DRIFTED:
            continue
        if entry.drift_class is not None:
            style = _DRIFT_CLASS_STYLES[entry.drift_class]
            class_str = f"[{style}]{entry.drift_class.value}[/{style}]"
        else:
            class_str = ""
        table.add_row(entry.name, class_str, entry.reason or "")

    return table
