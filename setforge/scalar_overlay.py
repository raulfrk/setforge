"""Stored-base 3-way driver for SCALAR ``preserve_user_keys`` paths.

The legacy ``preserve_user_keys`` overlay (``setforge.deploy._render_with_
preserve_keys`` → ``jsonc.overlay_user_keys`` / ``yaml_merge.overlay``) does a
BLIND live-wins splice: the live value always overwrites tracked for every
listed key. This module upgrades the SCALAR-path subset to a stored-base
3-way merge so an upstream (tracked) change to a key the user did NOT locally
edit propagates, while the user's own edits survive.

Orientation: ``ours`` = live, ``theirs`` = tracked/upstream. The live doc is
parsed mutable and used as the OUTPUT base — resolutions are applied onto it
and it is dumped byte-faithfully, mirroring the legacy overlay's "keep the
LIVE structure, then overlay" shape. The tracked doc is parsed read-only.

Per-path logic (see :func:`resolve_scalar_overlay`):

* **non-scalar leaf** (either read raises
  :class:`~setforge.errors.MergeTypeMismatch`) → fall back to blind live-wins:
  leave the live doc's value as-is and do NOT rebaseline. The path is not a
  scalar, so the scalar 3-way contract does not apply.
* **base ABSENT** (no stored base) → FIRST-RUN FALLBACK: keep ours (today's
  blind behavior) and SEED ``rebaseline[path] = ours`` (``ABSENT`` when ours
  is absent too) so the caller persists a base for next run.
* **base present** → :func:`setforge.scalar_merge.resolve_scalar`; TAKE /
  DELETE write through + rebaseline; CONFLICT resolves per ``auto`` (a bare
  ``auto is None`` conflict keeps ours, defers re-baselining, and flags
  ``deferred``).

CRITICAL: the :data:`~setforge.scalar_merge.ABSENT` sentinel is compared with
``is`` / ``is not`` ONLY, never ``==``. ``ABSENT`` (absent key) and ``None``
(literal ``null``) stay DISTINCT end-to-end: ``base_lookup`` returns ``ABSENT``
for an absent base and ``None`` for a ``null`` base, and ``resolve_scalar``
already distinguishes the two.
"""

import io
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from json5.dumper import ModelDumper, dumps
from json5.loader import ModelLoader, loads
from ruamel.yaml import YAML

from setforge import jsonc
from setforge.errors import MergeTypeMismatch
from setforge.scalar_merge import (
    ABSENT,
    ScalarOutcome,
    ScalarResolution,
    resolve_scalar,
)
from setforge.scalar_path import (
    read_scalar_jsonc,
    read_scalar_yaml,
    write_scalar_jsonc,
    write_scalar_yaml,
)
from setforge.section_wizard import ReconcileAuto


@dataclass(frozen=True, slots=True)
class ScalarOverlayResult:
    """Outcome of a stored-base 3-way scalar overlay over one file.

    ``merged_text`` is the rendered document to deploy (the live doc with
    every resolved path applied, dumped byte-faithfully). ``rebaseline`` maps
    each path whose base should advance to its new base value
    (:data:`~setforge.scalar_merge.ABSENT` for a deleted key); the caller
    seeds/advances the stored base from it. Paths NOT present in
    ``rebaseline`` must keep their existing base (non-scalar fallback, or a
    deferred bare conflict). ``conflicts`` lists every path that conflicted
    (even when ``auto`` resolved it). ``deferred`` is True iff at least one
    conflict was kept-live WITHOUT an ``auto`` decision, signalling the caller
    must NOT advance the base for those paths.
    """

    merged_text: str
    rebaseline: dict[str, object]
    conflicts: list[str]
    deferred: bool


def _yaml() -> YAML:
    """Return a round-trip YAML configured to match the scalar_path seam.

    Mirrors ``setforge.deploy._render_with_preserve_keys`` and
    ``tests.test_scalar_path``: ``typ="rt"`` for comment/format fidelity and
    ``preserve_quotes`` so scalar quoting survives the round-trip.
    """
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    return yaml


def resolve_scalar_overlay(
    dst: Path,
    live_text: str,
    tracked_text: str,
    preserve_user_keys: list[str],
    base_lookup: Callable[[str], object],
    auto: ReconcileAuto | None,
) -> ScalarOverlayResult:
    """Resolve scalar ``preserve_user_keys`` via a stored-base 3-way merge.

    ``dst`` selects the format (JSONC when :func:`setforge.jsonc.is_jsonc_file`,
    else YAML). ``live_text`` (= ours) is parsed into a MUTABLE doc that is the
    output base; ``tracked_text`` (= theirs) is parsed read-only.
    ``base_lookup`` maps a path to its stored base (typed scalar | ``None`` for
    a ``null`` base | :data:`~setforge.scalar_merge.ABSENT` for no base).
    ``auto`` is the install ``--auto`` reconcile decision (``None`` =
    interactive/bare). See the module docstring for the full per-path logic.

    A list-suffix path raises :class:`ValueError` from the scalar_path layer
    (a config error surfaced elsewhere) — it is NOT caught here.
    """
    read: Callable[[object, str], object]
    write: Callable[[object, str, ScalarResolution], None]
    is_jsonc = jsonc.is_jsonc_file(dst)
    if is_jsonc:
        live_doc = loads(live_text, loader=ModelLoader())
        tracked_doc = loads(tracked_text, loader=ModelLoader())
        read = read_scalar_jsonc
        write = write_scalar_jsonc
    else:
        yaml = _yaml()
        live_doc = yaml.load(live_text)
        tracked_doc = yaml.load(tracked_text)
        read = read_scalar_yaml
        write = write_scalar_yaml

    rebaseline: dict[str, object] = {}
    conflicts: list[str] = []
    deferred = False

    for path in preserve_user_keys:
        # A non-scalar leaf on EITHER side means this path is not a scalar:
        # fall back to blind live-wins (leave the live doc as-is) and skip
        # both the 3-way merge and any rebaseline.
        try:
            ours = read(live_doc, path)
            theirs = read(tracked_doc, path)
        except MergeTypeMismatch:
            continue

        base = base_lookup(path)
        if base is ABSENT:
            # First-run fallback: keep ours (live already carries it) and
            # SEED the base so next run has a stored anchor.
            rebaseline[path] = ours
            continue

        res = resolve_scalar(base, ours, theirs)
        path_deferred = _apply_resolution(
            write, live_doc, path, res, ours, theirs, auto, rebaseline, conflicts
        )
        deferred = deferred or path_deferred

    merged_text = _dump(is_jsonc, live_doc)
    return ScalarOverlayResult(
        merged_text=merged_text,
        rebaseline=rebaseline,
        conflicts=conflicts,
        deferred=deferred,
    )


def seed_scalar_bases(
    dst: Path, tracked_text: str, preserve_user_keys: list[str]
) -> dict[str, object]:
    """Seed a scalar base map from ``tracked_text`` for a first-install dst.

    Used by the deploy path when ``dst`` does not yet exist: the file is
    created from tracked verbatim, so each shallow scalar path's stored base
    must be seeded to its TRACKED value (the deployed value) — the ancestor
    the NEXT install resolves against. ``dst`` selects the format (JSONC when
    :func:`setforge.jsonc.is_jsonc_file`, else YAML). A path terminating on a
    non-scalar leaf is skipped (no base for a non-scalar path, mirroring the
    overlay's fallback); an absent key seeds :data:`ABSENT`. A list-suffix
    path raises :class:`ValueError` from the scalar_path layer (a config
    error surfaced elsewhere) — it is NOT caught here.
    """
    read: Callable[[object, str], object]
    if jsonc.is_jsonc_file(dst):
        tracked_doc = loads(tracked_text, loader=ModelLoader())
        read = read_scalar_jsonc
    else:
        tracked_doc = _yaml().load(tracked_text)
        read = read_scalar_yaml

    seed: dict[str, object] = {}
    for path in preserve_user_keys:
        try:
            seed[path] = read(tracked_doc, path)
        except MergeTypeMismatch:
            continue
    return seed


def _apply_resolution(
    write: Callable[[object, str, ScalarResolution], None],
    live_doc: object,
    path: str,
    res: ScalarResolution,
    ours: object,
    theirs: object,
    auto: ReconcileAuto | None,
    rebaseline: dict[str, object],
    conflicts: list[str],
) -> bool:
    """Apply one ``resolve_scalar`` outcome to ``live_doc`` at ``path``.

    Mutates ``live_doc`` (TAKE/DELETE writes; CONFLICT keeps or takes per
    ``auto``), appends to ``rebaseline`` / ``conflicts`` as the spec dictates,
    and returns whether this path DEFERRED (a bare ``auto is None`` conflict —
    its base must NOT advance). ``ours`` / ``theirs`` are the already-read live
    and tracked values, reused for the conflict branches.
    """
    match res.outcome:
        case ScalarOutcome.TAKE:
            write(live_doc, path, res)
            rebaseline[path] = res.value
            return False
        case ScalarOutcome.DELETE:
            write(live_doc, path, res)
            rebaseline[path] = ABSENT
            return False
        case ScalarOutcome.CONFLICT:
            conflicts.append(path)
            return _resolve_conflict(
                write, live_doc, path, ours, theirs, auto, rebaseline
            )


def _resolve_conflict(
    write: Callable[[object, str, ScalarResolution], None],
    live_doc: object,
    path: str,
    ours: object,
    theirs: object,
    auto: ReconcileAuto | None,
    rebaseline: dict[str, object],
) -> bool:
    """Resolve a CONFLICT at ``path`` per ``auto``; return whether deferred.

    ``USE_TRACKED`` writes theirs (TAKE, or DELETE when theirs is absent) and
    rebaselines theirs. ``KEEP_LIVE`` keeps ours (live already carries it) and
    rebaselines ours. ``None`` (bare) keeps ours, does NOT rebaseline, and
    DEFERS — the caller must hold the base where it is.
    """
    match auto:
        case ReconcileAuto.USE_TRACKED:
            write(live_doc, path, _take_or_delete(theirs))
            rebaseline[path] = theirs
            return False
        case ReconcileAuto.KEEP_LIVE:
            rebaseline[path] = ours
            return False
        case None:
            return True


def _take_or_delete(value: object) -> ScalarResolution:
    """Build a TAKE resolution for ``value`` (DELETE when it is ABSENT)."""
    if value is ABSENT:
        return ScalarResolution(ScalarOutcome.DELETE)
    return ScalarResolution(ScalarOutcome.TAKE, value)


def _dump(is_jsonc: bool, doc: object) -> str:
    """Dump ``doc`` byte-faithfully for the detected format."""
    if is_jsonc:
        return dumps(doc, dumper=ModelDumper())
    buf = io.StringIO()
    _yaml().dump(doc, buf)
    return buf.getvalue()
