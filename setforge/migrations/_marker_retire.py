"""The marker-retire migration — schema 2.0 → 2.1.

Retires the user-section MARKER mechanism: strips every ``setforge:user-section``
comment-marker pair from tracked + live files (shared bodies stay in place; host-
local bodies are captured into the markerless ``local.yaml`` overlay first), then
the parser/reconcile/wizard modules are deleted in the same change.

This module embeds a **frozen, self-contained marker reader** (the grammar copied
verbatim from :mod:`setforge._legacy_markers` at author time) so the migration keeps
working AFTER ``sections.py`` is deleted — a fresh host that still carries legacy
markers must be able to migrate. The reader is STRICT and fail-closed: a marker
missing its ``host-local|shared`` keyword, or a pre-rename ``my-setup:`` namespace
marker, is REFUSED with :class:`~setforge.errors.MarkerError` rather than silently
defaulted to SHARED — defaulting a host-local body to shared would leak it into the
shared repo (the lenient-parse trap in :mod:`setforge._legacy_markers` this must
not inherit).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from setforge.errors import ConfigError, MarkerError
from setforge.migrations import ManifestEntry, ManifestType, MigrationRoots

if TYPE_CHECKING:
    from setforge.reconcile import FileId

#: The closed set of marker semantics keywords (frozen copy of the sections.py
#: grammar). Kept as the regex alternation the marker line must carry.
_SEMANTICS_KEYWORDS: Final = "host-local|shared"

# Grammar frozen from setforge._legacy_markers at author time — see that module's
# docstring for the canonical marker syntax. Kept self-contained so the migration
# survives the deletion of sections.py.
_MARKER_RE: Final = re.compile(
    r"^\s*<!--\s*setforge:user-section\s+(start|end)"
    rf"(?:\s+({_SEMANTICS_KEYWORDS}))?"
    r"(?:\s+(?!hash=)(\S+))?"
    r"(?:\s+hash=(\S+))?"
    r"\s*-->\s*$"
)
# Broad detector: a line that declares one of our markers regardless of payload
# shape — distinguishes "not our marker" from "our marker, malformed".
_MARKER_PREFIX_RE: Final = re.compile(
    r"^\s*<!--\s*setforge:user-section\s+(start|end)\s+(.*?)\s*-->\s*$"
)
# Pre-rename ``my-setup:`` namespace markers — invisible to the post-rename parser,
# so the migration must REFUSE them rather than drop their bodies.
_LEGACY_NAMESPACE_RE: Final = re.compile(
    r"^\s*<!--\s*my-setup:user-section\s+(start|end)\b"
)
_HASH_VALUE_RE: Final = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ParsedSection:
    """One marker-delimited section parsed by the migration's frozen reader.

    ``name`` is the canonical key (the start-marker name, or the 0-based string
    index for an unnamed section, matching
    :func:`setforge._legacy_markers.extract_sections`).
    ``semantics`` is the ``host-local``/``shared`` keyword value. ``body`` is the
    verbatim text between the markers (newlines preserved; ``""`` for an empty
    section). ``embedded_hash`` is the end marker's ``hash=`` baseline, or ``None``.
    """

    name: str
    semantics: str
    body: str
    embedded_hash: str | None


@dataclass(slots=True)
class _Open:
    """Accumulator for the currently-open section during the walk."""

    name: str | None
    key: str
    semantics: str
    body: list[str]


def parse_markers(text: str) -> list[ParsedSection]:
    """STRICT, self-contained parse of ``text`` into its marker sections.

    Byte-equivalent to :func:`setforge._legacy_markers.extract_sections` /
    ``section_semantics`` / ``extract_marker_hashes`` for VALID input. Fail-closed:
    raises :class:`~setforge.errors.MarkerError` on a marker missing its
    ``host-local|shared`` keyword, a malformed marker, a ``my-setup:`` legacy
    namespace marker, a nested/unbalanced pair, or a duplicate section key — so a
    host-local body can never be silently reclassified as shared.
    """
    out: list[ParsedSection] = []
    open_section: _Open | None = None
    unnamed_index = 0
    seen_keys: set[str] = set()

    for lineno, line in enumerate(text.splitlines(keepends=True), start=1):
        if _LEGACY_NAMESPACE_RE.match(line):
            raise MarkerError(
                f"line {lineno}: legacy 'my-setup:user-section' marker — rename the "
                f"namespace to 'setforge:' before migrating (would otherwise lose "
                f"its host-local body)"
            )
        match = _MARKER_RE.match(line)
        if match is None:
            if _MARKER_PREFIX_RE.match(line):
                raise MarkerError(f"line {lineno}: malformed user-section marker")
            if open_section is not None:
                open_section.body.append(line)
            continue

        kind, semantics_kw, name, hash_seg = match.groups()
        if kind == "start":
            open_section = _handle_start(
                lineno, semantics_kw, name, open_section, unnamed_index
            )
            if name is None:
                unnamed_index += 1
        else:
            section = _handle_end(
                lineno, semantics_kw, name, hash_seg, open_section, seen_keys
            )
            out.append(section)
            open_section = None

    if open_section is not None:
        raise MarkerError(f"unclosed user-section {open_section.key!r}")
    return out


def _handle_start(
    lineno: int,
    semantics_kw: str | None,
    name: str | None,
    open_section: _Open | None,
    unnamed_index: int,
) -> _Open:
    """Validate a start marker and open a section (fail-closed on a bad keyword)."""
    if open_section is not None:
        raise MarkerError(f"line {lineno}: nested user-section is not supported")
    if semantics_kw is None:
        raise MarkerError(
            f"line {lineno}: user-section start marker missing the required "
            f"'host-local' or 'shared' keyword (refusing — would leak a host-local "
            f"body if defaulted to shared)"
        )
    key = name if name is not None else str(unnamed_index)
    return _Open(name=name, key=key, semantics=semantics_kw, body=[])


def _handle_end(
    lineno: int,
    semantics_kw: str | None,
    name: str | None,
    hash_seg: str | None,
    open_section: _Open | None,
    seen_keys: set[str],
) -> ParsedSection:
    """Validate an end marker against its open start; return the parsed section."""
    if open_section is None:
        raise MarkerError(f"line {lineno}: user-section end marker without a start")
    if semantics_kw is None:
        raise MarkerError(
            f"line {lineno}: user-section end marker missing the required keyword"
        )
    if semantics_kw != open_section.semantics:
        raise MarkerError(
            f"line {lineno}: end marker semantics {semantics_kw!r} does not match "
            f"start {open_section.semantics!r}"
        )
    if name != open_section.name:
        raise MarkerError(
            f"line {lineno}: end marker name {name!r} does not match start "
            f"{open_section.name!r}"
        )
    if open_section.key in seen_keys:
        raise MarkerError(
            f"line {lineno}: duplicate user-section key {open_section.key!r}"
        )
    seen_keys.add(open_section.key)
    embedded_hash = (
        hash_seg if hash_seg is not None and _HASH_VALUE_RE.match(hash_seg) else None
    )
    return ParsedSection(
        name=open_section.key,
        semantics=open_section.semantics,
        body="".join(open_section.body),
        embedded_hash=embedded_hash,
    )


def _body_hash(body: str) -> str:
    """sha256-hex of a section body — byte-equivalent to ``sections.hash_sections``.

    Frozen so the migration can recognise a ``hash=`` baseline without importing
    the retired :mod:`setforge._legacy_markers`.
    """
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def strip_markers(text: str) -> str:
    """Return ``text`` with every marker retired: marker comment lines removed,
    host-local section bodies dropped, shared section bodies kept inline.

    This is the ``live → recorded-local`` shaping the migration records and the
    on-disk strip it writes back. Validates STRICTLY first (raising
    :class:`~setforge.errors.MarkerError` on a malformed / legacy marker), so the
    caller gets the same fail-closed guarantee as :func:`parse_markers`.
    """
    return _render_stripped(text, {})


def baseline_stripped(live_text: str, tracked_text: str) -> str:
    """Reconstruct the 3-way merge ``base`` for the reconcile store.

    Same shape as :func:`strip_markers` over ``live_text`` (markers gone,
    host-local dropped), but every SHARED section body is replaced by its
    embedded-``hash=`` baseline — the body on whichever side (live, then tracked)
    still hashes to its recorded baseline. Seeding the base from the live body
    unconditionally would make ``ours == base`` for a live-edited section and let
    the next install silently overwrite the edit with tracked (pitfall MP-4); the
    baseline reconstruction keeps the edited side the changed one. Both sides are
    stripped by the SAME walk, so the surrounding context that
    ``reconcile.hunks.extract_hunks`` anchors on stays byte-aligned (MP-5b).
    """
    return _render_stripped(live_text, _baseline_bodies(live_text, tracked_text))


def _baseline_bodies(live_text: str, tracked_text: str) -> dict[str, str]:
    """Map each SHARED section key to its baseline body (the pristine side).

    Prefers the live body when it still hashes to its embedded baseline, then the
    tracked body; falls back to the live body when neither matches (both sides
    moved, or no ``hash=`` recorded — the baseline bytes are unrecoverable, only
    its hash was kept, so the live bytes are the safest base).
    """
    tracked = {p.name: p for p in parse_markers(tracked_text)}
    baselines: dict[str, str] = {}
    for parsed in parse_markers(live_text):
        if parsed.semantics != "shared":
            continue
        if parsed.embedded_hash is not None and _body_hash(parsed.body) == (
            parsed.embedded_hash
        ):
            baselines[parsed.name] = parsed.body
            continue
        counterpart = tracked.get(parsed.name)
        if (
            counterpart is not None
            and counterpart.embedded_hash is not None
            and _body_hash(counterpart.body) == counterpart.embedded_hash
        ):
            baselines[parsed.name] = counterpart.body
            continue
        baselines[parsed.name] = parsed.body
    return baselines


def _render_stripped(text: str, shared_overrides: dict[str, str]) -> str:
    """Strip ``text``'s markers, optionally substituting shared section bodies.

    Walks ``text`` after a strict :func:`parse_markers` validation pass: marker
    lines and host-local bodies are dropped; a shared section emits its
    ``shared_overrides`` body when keyed, else its verbatim body. The validation
    pass guarantees the walk only meets well-formed ``setforge:`` markers.
    """
    parse_markers(text)
    out: list[str] = []
    open_semantics: str | None = None
    open_key: str | None = None
    body: list[str] = []
    unnamed_index = 0

    for line in text.splitlines(keepends=True):
        match = _MARKER_RE.match(line)
        if match is None:
            if open_semantics is None:
                out.append(line)
            else:
                body.append(line)
            continue
        kind, semantics_kw, name, _hash_seg = match.groups()
        if kind == "start":
            open_semantics = semantics_kw
            open_key = name if name is not None else str(unnamed_index)
            if name is None:
                unnamed_index += 1
            body = []
        else:
            if open_semantics == "shared":
                assert open_key is not None
                out.append(shared_overrides.get(open_key, "".join(body)))
            open_semantics = None
            open_key = None
            body = []
    return "".join(out)


def _has_markers(text: str) -> bool:
    """Cheap gate: does ``text`` carry any user-section marker comment at all?

    Liberal on purpose — a false positive only costs a redundant (no-op) parse +
    strip; the authoritative grammar check is :func:`parse_markers`. Catches both
    the ``setforge:`` and the pre-rename ``my-setup:`` namespaces so a legacy
    marker still reaches the fail-closed parse.
    """
    return "user-section" in text


@dataclass(frozen=True, slots=True)
class _FilePlan:
    """A single tracked file's retire plan, resolved + validated in pass 1."""

    name: str
    fid: FileId
    src: Path
    dst: Path
    profiles: tuple[str, ...]
    tracked_text: str
    live_text: str
    has_markers: bool


_LOCAL_YAML_RELPATH: Final = (".config", "setforge", "local.yaml")


def _local_yaml_path(roots: MigrationRoots) -> Path:
    """The host-local ``local.yaml`` under ``roots.home`` (overlay capture target)."""
    path = roots.home
    for part in _LOCAL_YAML_RELPATH:
        path = path / part
    return path


def _store_legs(profile: str, fid: FileId) -> tuple[Path, ...]:
    """The reconcile-store paths a seed for ``(profile, fid)`` writes.

    Listed in :meth:`MarkerRetireMigration.affected_paths` so
    ``setforge revert --profile=migrate`` snapshots + restores them (the seed is
    NOT re-derivable once the markers are stripped — pitfall MP-1/5).
    """
    from setforge.transitions import state_root

    root = state_root()
    return (
        root / "base" / profile / fid,
        root / "local" / profile / fid,
        root / "local-absent" / profile / fid,
        root / "index" / f"{profile}.json",
    )


def _file_plans(roots: MigrationRoots) -> list[_FilePlan]:
    """Resolve + read + STRICT-validate every (profile x tracked file).

    Pass 1 of :meth:`MarkerRetireMigration.apply`: enumerates each profile's
    resolved tracked files (C-7), dedupes by tracked-file key (src/dst are
    profile-independent), reads the on-disk tracked + live bytes, and parses any
    marker-bearing file so a malformed / legacy marker REFUSES before pass 2
    mutates anything (fail-closed, all-or-nothing on validation).
    """
    from setforge.compare import resolve_dst, resolve_src
    from setforge.config import load_config, resolve_profile
    from setforge.reconcile import file_id

    config = load_config(roots.cfg_path)
    file_profiles: dict[str, list[str]] = {}
    for profile_name in config.profiles:
        for name in resolve_profile(config, profile_name).tracked_files:
            file_profiles.setdefault(name, []).append(profile_name)

    plans: list[_FilePlan] = []
    for name, profiles in file_profiles.items():
        tracked_file = config.tracked_files[name]
        src = resolve_src(tracked_file, roots.repo_root)
        dst = resolve_dst(tracked_file)
        tracked_text = src.read_text(encoding="utf-8") if src.exists() else ""
        live_text = dst.read_text(encoding="utf-8") if dst.exists() else ""
        has_markers = _has_markers(tracked_text) or _has_markers(live_text)
        if has_markers:
            # Fail-closed: a malformed/legacy marker in ANY file aborts the whole
            # migration before a single byte is written.
            parse_markers(tracked_text)
            parse_markers(live_text)
        plans.append(
            _FilePlan(
                name=name,
                fid=file_id(name),
                src=src,
                dst=dst,
                profiles=tuple(profiles),
                tracked_text=tracked_text,
                live_text=live_text,
                has_markers=has_markers,
            )
        )
    return plans


@dataclass(frozen=True, slots=True)
class MarkerRetireMigration:
    """Strip every user-section marker; fold host-local bodies into overlay spans.

    The 2.0 -> 2.1 forward migration. Per profile x tracked file (C-7): captures
    host-local marker bodies into ``local.yaml`` overlay spans (DL4, before any
    strip), strips every marker line from tracked + live (shared bodies stay
    inline), and seeds the reconcile store with a baseline-derived 3-way base
    (MP-4) under the profile lock (C-1/2). The ``schema_version`` stamp is written
    LAST so a crash mid-strip re-runs cleanly: per-file idempotency keys on marker
    PRESENCE, never the global stamp (MP-3). Irreversible — see :attr:`reverse`.
    """

    from_version: str = "2.0"
    to_version: str = "2.1"

    @property
    def reverse(self) -> _MarkerRetireReverse:
        """The one-way inverse — refuses (markers cannot be regenerated)."""
        return _MarkerRetireReverse(
            from_version=self.to_version, to_version=self.from_version
        )

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        """One EDIT per stripped tracked/live file + the local.yaml + the stamp."""
        entries: list[ManifestEntry] = []
        for plan in _file_plans(roots):
            if not plan.has_markers:
                continue
            for path in (plan.src, plan.dst):
                if path.exists():
                    entries.append(
                        ManifestEntry(
                            type=ManifestType.EDIT,
                            description=f"strip user-section markers from {plan.name}",
                            affected_path=path,
                        )
                    )
        entries.append(
            ManifestEntry(
                type=ManifestType.EDIT,
                description="capture host-local marker bodies -> local.yaml overlays",
                affected_path=_local_yaml_path(roots),
            )
        )
        entries.append(
            ManifestEntry(
                type=ManifestType.EDIT,
                description=f"stamp schema_version {self.to_version!r}",
                affected_path=roots.cfg_path,
            )
        )
        return tuple(entries)

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        """setforge.yaml + local.yaml + every stripped file + the seeded store legs.

        The store legs are listed so ``setforge revert --profile=migrate``
        snapshots them: a seeded base is NOT re-derivable once markers are
        stripped (MP-1/5).
        """
        paths: list[Path] = [roots.cfg_path, _local_yaml_path(roots)]
        for plan in _file_plans(roots):
            paths.extend(p for p in (plan.src, plan.dst) if p.exists())
            for profile in plan.profiles:
                paths.extend(_store_legs(profile, plan.fid))
        return tuple(paths)

    def apply(self, *, roots: MigrationRoots) -> None:
        """Retire every marker, then stamp 2.1 (see the class docstring)."""
        from setforge import locking, reconcile
        from setforge.atomicio import atomic_write_text
        from setforge.host_local_marker_migration import append_overlay_spans

        plans = _file_plans(roots)  # pass 1: resolve + validate (fail-closed)
        local_yaml = _local_yaml_path(roots)

        for plan in plans:  # pass 2: capture + seed + strip
            base_text, local_text = _base_and_local(plan)
            for profile in plan.profiles:
                if not plan.dst.exists():
                    continue  # nothing deployed on this host -> nothing to seed
                with locking.profile_lock(profile):
                    if reconcile.read_base(profile, plan.fid) is not None:
                        continue  # already seeded (idempotent replay, MP-4)
                    reconcile.record(
                        profile,
                        plan.fid,
                        base=base_text.encode("utf-8"),
                        local=local_text.encode("utf-8"),
                    )
            if not plan.has_markers:
                continue
            host_bodies = [
                (section.name, section.body)
                for section in parse_markers(plan.live_text)
                if section.semantics != "shared"
            ]
            if host_bodies:  # DL4: capture BEFORE the strip deletes the bodies
                append_overlay_spans(local_yaml, {plan.fid: host_bodies})
            if plan.src.exists():
                atomic_write_text(plan.src, strip_markers(plan.tracked_text))
            if plan.dst.exists():
                atomic_write_text(plan.dst, local_text)

        _stamp_schema_version(roots.cfg_path, self.to_version)


def _base_and_local(plan: _FilePlan) -> tuple[str, str]:
    """The reconcile-store ``(base, local)`` content for a file plan.

    ``base`` is the baseline-reconstructed 3-way merge base (MP-4); ``local`` is
    the marker-stripped live content. A markerless file seeds its live bytes
    verbatim on both legs (a degenerate no-drift base).
    """
    if not plan.has_markers:
        return plan.live_text, plan.live_text
    return (
        baseline_stripped(plan.live_text, plan.tracked_text),
        strip_markers(plan.live_text),
    )


def _stamp_schema_version(cfg_path: Path, to_version: str) -> None:
    """Rewrite ``schema_version: <to_version>`` in setforge.yaml (comment-safe)."""
    from setforge.migrations import _require_mapping_root
    from setforge.migrations._yaml_ops import atomic_write_yaml, yaml_rt

    yaml = yaml_rt()
    with cfg_path.open("r", encoding="utf-8") as fh:
        data = yaml.load(fh)
    data = _require_mapping_root(data, cfg_path)
    data["schema_version"] = to_version
    atomic_write_yaml(cfg_path, data)


@dataclass(frozen=True, slots=True)
class _MarkerRetireReverse:
    """The one-way inverse of :class:`MarkerRetireMigration`.

    Marker retirement is irreversible: the strip discards the marker syntax, and a
    stripped shared body is indistinguishable from natively-authored content, so
    the down-migration CANNOT regenerate the markers. ``apply`` refuses with a
    clear :class:`~setforge.errors.ConfigError` rather than restamping to 2.0 and
    leaving the files markerless under an older schema (pitfall MP-2). The
    reversible path is the transition-based ``setforge revert --profile=migrate``
    (which restores the snapshotted bytes), not this schema-chain reverse.
    """

    from_version: str = "2.1"
    to_version: str = "2.0"

    @property
    def reverse(self) -> MarkerRetireMigration:
        """The forward 2.0 -> 2.1 migration (keeps the Protocol symmetric)."""
        return MarkerRetireMigration(
            from_version=self.to_version, to_version=self.from_version
        )

    def manifest(self, *, roots: MigrationRoots) -> tuple[ManifestEntry, ...]:
        """A single NOTE: the down-migration is refused, nothing is mutated."""
        return (
            ManifestEntry(
                type=ManifestType.NOTE,
                description=(
                    "marker retirement is irreversible — down-migration refuses"
                ),
                affected_path=roots.cfg_path,
            ),
        )

    def affected_paths(self, *, roots: MigrationRoots) -> tuple[Path, ...]:
        """Nothing is touched: the reverse refuses before any write."""
        return ()

    def apply(self, *, roots: MigrationRoots) -> None:
        """Refuse: stripped markers cannot be regenerated."""
        raise ConfigError(
            "cannot down-migrate from schema 2.1 to 2.0: user-section markers were "
            "retired and their syntax cannot be regenerated. Use 'setforge revert "
            "--profile=migrate' to restore the marker bytes from the recorded "
            "transition."
        )
