"""Per-host stored-base store for individual FORKED scalar key-paths.

Sibling to the verbatim-bytes store (:mod:`setforge.base_store`). Where
that store keeps the last-deployed *bytes* of a whole tracked file, this
store keeps the last-deployed *value of a single forked scalar key-path* —
the common ancestor a forked-scalar three-way merge
(:func:`setforge.scalar_merge.resolve_scalar`) needs to tell "the user
edited the live value" apart from "tracked moved the value upstream".

Layout + format
---------------
One JSON manifest per ``(profile, file-id)`` at
``<state_root>/scalar-base/<profile>/<file-id>.json`` — a SIBLING root to
``<state_root>/base/`` (the bytes store). Each manifest maps a dotted
key-path to a record::

    { "<dotted-path>": {"present": bool, "value": <json scalar>} }

* ``present: false`` -> the key-path was deployed *absent* (no ``value``
  key is written).
* ``present: true, value: null`` -> the key-path was deployed as a literal
  ``null``.

These two states are DISTINCT and must never collapse — a forked-scalar
merge treats field-absence (:data:`setforge.scalar_merge.ABSENT`) as an
operand wholly separate from a present ``None``.

Type fidelity rides JSON's native scalar types: ``1`` round-trips as
``int``, ``1.0`` as ``float``, ``true`` as ``bool``, ``"x"`` as ``str``.
No numeric coercion is applied before serialization. ``json.dumps`` runs
with ``allow_nan=False`` so a ``NaN``/``Inf`` value is REJECTED at write
(JSON has no such literals) rather than emitting non-standard tokens.

Single-writer invariant
------------------------
``setforge install`` is a single process, so the manifest has exactly one
writer at a time. :func:`set_bases` is the PRIMARY entry point: it does ONE
read-modify-write of the whole manifest for every path it is given, which
closes the lost-update race a per-path read-modify-write would open.
:func:`set_base` and :func:`re_baseline` are thin one-key shims over the
same read-modify-write-one-key primitive; correctness still relies on the
single-writer invariant, since two concurrent writers could interleave
their read and write phases.

No install/deploy/revert wiring lives here — the scope is the store
primitive only.
"""

import json
from pathlib import Path

from setforge import atomicio
from setforge.errors import BaseStoreError, BaseStoreIOError
from setforge.scalar_merge import ABSENT
from setforge.transitions import state_root

type _Manifest = dict[str, dict[str, object]]


def scalar_base_root() -> Path:
    """Root directory holding every profile's scalar-base manifests."""
    return state_root() / "scalar-base"


def _profile_root(profile: str) -> Path:
    """Resolved root of ``profile``'s scalar-base subtree."""
    return (scalar_base_root() / profile).resolve()


def _resolve_target(profile: str, file_id: str) -> Path:
    """Map ``(profile, file_id)`` to its manifest path, guarding traversal.

    Rejects a ``file_id`` that is absolute or contains a ``..``
    component, and verifies the resolved manifest stays within the
    profile's subtree, so a malicious or buggy file-id can never write a
    manifest outside ``scalar-base/<profile>/``.
    """
    candidate = Path(file_id)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise BaseStoreError(
            f"unsafe file-id {file_id!r}: must be a relative path with no "
            "'..' components"
        )
    profile_root = _profile_root(profile)
    target = (profile_root / f"{file_id}.json").resolve()
    if profile_root not in target.parents:
        raise BaseStoreError(
            f"file-id {file_id!r} resolves outside scalar-base/{profile}/"
        )
    return target


def _read_manifest(profile: str, file_id: str) -> _Manifest:
    """Return the parsed manifest for ``(profile, file_id)``.

    A missing manifest (no key-path ever stored) reads as an empty dict —
    every path is then :data:`ABSENT`. A corrupt/hand-edited manifest
    raises :class:`BaseStoreError`; it is NEVER silently treated as empty.
    """
    target = _resolve_target(profile, file_id)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as err:
        raise BaseStoreIOError(
            f"failed to read scalar base for {profile}/{file_id}: {err}"
        ) from err
    try:
        return json.loads(raw)
    except json.JSONDecodeError as err:
        raise BaseStoreError(
            f"corrupt scalar-base manifest for {profile}/{file_id}: {err}"
        ) from err


def _write_manifest(profile: str, file_id: str, manifest: _Manifest) -> None:
    """Atomically serialize and write ``manifest`` for ``(profile, file_id)``.

    Serializes with ``allow_nan=False`` so a ``NaN``/``Inf`` scalar is
    rejected (wrapped as :class:`BaseStoreError`) before any disk write.
    """
    target = _resolve_target(profile, file_id)
    try:
        text = json.dumps(manifest, allow_nan=False, indent=2, sort_keys=True)
    except ValueError as err:
        raise BaseStoreError(
            f"non-finite scalar value for {profile}/{file_id}: {err}"
        ) from err
    try:
        atomicio.atomic_write_text(target, text + "\n")
    except OSError as err:
        raise BaseStoreIOError(
            f"failed to write scalar base for {profile}/{file_id}: {err}"
        ) from err


def _record(value_or_absent: object) -> dict[str, object]:
    """Build the on-disk record for ``value_or_absent``.

    :data:`ABSENT` yields ``{"present": False}`` (no ``value`` key); any
    present scalar — including ``None`` — yields
    ``{"present": True, "value": <scalar>}``.
    """
    if value_or_absent is ABSENT:
        return {"present": False}
    return {"present": True, "value": value_or_absent}


def get_base(profile: str, file_id: str, path: str) -> object:
    """Return the stored base value for ``path`` under ``(profile, file_id)``.

    Returns the typed scalar (``int``/``float``/``bool``/``str``), ``None``
    for a stored literal ``null``, or :data:`ABSENT` for both a
    ``present: false`` record AND a missing path/manifest. The ``present``
    flag is read explicitly — never inferred from a value lookup — so a
    stored ``null`` is never confused with absence.
    """
    manifest = _read_manifest(profile, file_id)
    record = manifest.get(path)
    if record is None or not record.get("present", False):
        return ABSENT
    return record.get("value")


def set_bases(profile: str, file_id: str, values: dict[str, object]) -> None:
    """Record base values for every path in ``values`` (PRIMARY entry point).

    Does ONE read-modify-write of the whole manifest: untouched paths are
    preserved, and each given path is set to its value (or, if the value is
    :data:`ABSENT`, to a ``present: false`` record). Performing every
    update in a single write closes the lost-update race that a per-path
    read-modify-write would open.
    """
    manifest = _read_manifest(profile, file_id)
    for path, value in values.items():
        manifest[path] = _record(value)
    _write_manifest(profile, file_id, manifest)


def set_base(profile: str, file_id: str, path: str, value: object) -> None:
    """Record ``value`` as the base for a single ``path``.

    Thin one-key shim over :func:`set_bases`. Passing
    :data:`ABSENT` records ``present: false`` (deployed-absent).
    """
    set_bases(profile, file_id, {path: value})


def re_baseline(profile: str, file_id: str, path: str, value_or_absent: object) -> None:
    """Overwrite the base for ``path`` post-resolution.

    Rewrites the stored ancestor to whatever actually landed live after a
    merge resolves. Passing :data:`ABSENT` records ``present: false``
    (deployed-absent) — DISTINCT from :func:`prune`, which removes the
    manifest entry entirely. Semantically identical to :func:`set_base`;
    named separately to mark the post-deploy re-baseline call site.
    """
    set_bases(profile, file_id, {path: value_or_absent})


def prune(profile: str, file_id: str, live_paths: set[str]) -> None:
    """Drop manifest entries for ``(profile, file_id)`` not in ``live_paths``.

    Strictly scoped to the one manifest: no other ``(profile, file_id)``
    pair is touched. A path absent from ``live_paths`` has its record
    REMOVED (distinct from a ``present: false`` record, which records a
    deployed-absent value). A missing manifest is a no-op.
    """
    manifest = _read_manifest(profile, file_id)
    pruned = {path: record for path, record in manifest.items() if path in live_paths}
    if pruned != manifest:
        _write_manifest(profile, file_id, pruned)
