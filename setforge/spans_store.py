"""Per-host derived-state sidecar for sub-file spans (Layer 2).

Sibling to the verbatim-bytes store (:mod:`setforge.base_store`) and the
scalar-base store (:mod:`setforge.scalar_base_store`). Where those keep
the last-deployed bytes / scalar values, this store keeps the *derived
relocation + baseline state* for every span: the data the relocation
ladder needs to find a span's current offsets in a file whose content has
moved since the span was last resolved.

Layout + format
---------------
One JSON manifest per ``(profile, file-id)`` at
``<state_root>/spans/<profile>/<file-id>.json`` — a SIBLING root to
``<state_root>/base/`` and ``<state_root>/scalar-base/``. Each manifest
maps a span's ANCHOR (its stable Layer-1 identity) to a
:class:`SpanState` record::

    { "<anchor>": {"anchor", "fingerprint", "prefix", "suffix",
                   "position_hint_start_line", "position_hint_n_lines",
                   "heading_level"} }

The fingerprint is a sha256 hex digest of the span's resolved body
(``sections.hash_sections`` style). ``prefix`` / ``suffix`` are the ~3
lines of context immediately around the span used by the fuzzy relocation
stage. ``position_hint_*`` is an ADVISORY search-start hint (GNU-patch
style) — never the authoritative location key (Invariant I12). The
heading level lets an upstream level change (``## Foo`` → ``### Foo``) be
detected and warned rather than silently re-scoping.

Invariant I12: intent (anchor + kind + semantics) lives only in Layer 1;
these resolved offsets + baseline state live only here. Offsets are
recomputed from current content each run.

Single-writer invariant: ``setforge install`` is a single process, so
:func:`set_states` does ONE read-modify-write of the whole manifest,
closing the lost-update race a per-anchor write would open.

No install/deploy/revert wiring lives here — the scope is the store
primitive only. The revert lockstep (Invariant I5) is owned by the
install/revert integration, which snapshots this sidecar's files into the
transition record alongside the live files.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from setforge import atomicio
from setforge.errors import BaseStoreError, BaseStoreIOError
from setforge.transitions import state_root

__all__ = [
    "SpanState",
    "get_states",
    "manifest_path",
    "prune",
    "set_states",
    "spans_root",
]


@dataclass(slots=True, frozen=True)
class SpanState:
    """Derived relocation + baseline state for one span.

    ``anchor`` is the span's Layer-1 identity (manifest key, repeated in
    the record for round-trip self-description). ``fingerprint`` is the
    sha256 hex digest of the span's resolved body. ``prefix`` / ``suffix``
    are the lines of context above / below the span (for the fuzzy
    relocation stage). ``position_hint_start_line`` /
    ``position_hint_n_lines`` are the advisory last-known offsets (a
    search-start hint only). ``heading_level`` is the markdown ATX level
    of the span's anchor heading (1-6).

    ``last_deployed_body`` is the exact canonical body bytes an OVERLAY
    span last injected into the live file (``None`` for pinned / forked
    spans, which carry no body). It is the deploy / capture excise needle —
    the body's identity is these recorded bytes, never a re-derived
    anchor / offset. Anchor-keyed like every field on this record (a
    name-keyed body would contradict the manifest's anchor-key invariant).
    """

    anchor: str
    fingerprint: str
    prefix: list[str]
    suffix: list[str]
    position_hint_start_line: int
    position_hint_n_lines: int
    heading_level: int
    last_deployed_body: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return the JSON-serializable record for this state."""
        record: dict[str, object] = {
            "anchor": self.anchor,
            "fingerprint": self.fingerprint,
            "prefix": list(self.prefix),
            "suffix": list(self.suffix),
            "position_hint_start_line": self.position_hint_start_line,
            "position_hint_n_lines": self.position_hint_n_lines,
            "heading_level": self.heading_level,
        }
        # Omit when absent so pinned/forked manifests stay byte-identical to
        # their pre-OVERLAY shape (no spurious key churn on re-serialize).
        if self.last_deployed_body is not None:
            record["last_deployed_body"] = self.last_deployed_body
        return record


type _Manifest = dict[str, SpanState]


def spans_root() -> Path:
    """Root directory holding every profile's spans manifests."""
    return state_root() / "spans"


def _profile_root(profile: str) -> Path:
    """Resolved root of ``profile``'s spans subtree."""
    return (spans_root() / profile).resolve()


def _resolve_target(profile: str, file_id: str) -> Path:
    """Map ``(profile, file_id)`` to its manifest path, guarding traversal.

    Rejects a ``file_id`` that is absolute or contains a ``..``
    component, and verifies the resolved manifest stays within the
    profile's subtree, so a malicious or buggy file-id can never write a
    manifest outside ``spans/<profile>/``.
    """
    candidate = Path(file_id)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise BaseStoreError(
            f"unsafe file-id {file_id!r}: must be a relative path with no "
            "'..' components"
        )
    profile_root = _profile_root(profile)
    target = (profile_root / f"{candidate}.json").resolve()
    if profile_root not in target.parents:
        raise BaseStoreError(f"file-id {file_id!r} resolves outside spans/{profile}/")
    return target


def _decode_state(anchor: str, record: object, profile: str, file_id: str) -> SpanState:
    """Validate one JSON record into a :class:`SpanState`.

    Raises :class:`BaseStoreError` on any shape deviation so a corrupt /
    hand-edited manifest never silently degrades the relocation ladder.
    """
    if not isinstance(record, dict):
        raise BaseStoreError(
            f"corrupt spans manifest for {profile}/{file_id}: record for "
            f"{anchor!r} must be an object, got {type(record).__name__}"
        )
    try:
        prefix = record["prefix"]
        suffix = record["suffix"]
        if not (isinstance(prefix, list) and isinstance(suffix, list)):
            raise TypeError("prefix/suffix must be lists")
        raw_body = record.get("last_deployed_body")
        last_deployed_body = None if raw_body is None else str(raw_body)
        return SpanState(
            anchor=str(record["anchor"]),
            fingerprint=str(record["fingerprint"]),
            prefix=[str(line) for line in prefix],
            suffix=[str(line) for line in suffix],
            position_hint_start_line=int(record["position_hint_start_line"]),
            position_hint_n_lines=int(record["position_hint_n_lines"]),
            heading_level=int(record["heading_level"]),
            last_deployed_body=last_deployed_body,
        )
    except (KeyError, TypeError, ValueError) as err:
        raise BaseStoreError(
            f"corrupt spans manifest for {profile}/{file_id}: record for "
            f"{anchor!r} is malformed: {err}"
        ) from err


def manifest_path(profile: str, file_id: str) -> Path:
    """Return the on-disk manifest path for ``(profile, file_id)``.

    Public so the transition state-snapshot integration (Invariant I5)
    can capture and restore the sidecar's verbatim bytes alongside the
    byte base — revert then rolls the sidecar back in lockstep with
    live + base. Applies the same traversal guard as the read/write
    entry points.
    """
    return _resolve_target(profile, file_id)


def get_states(profile: str, file_id: str) -> _Manifest:
    """Return every stored :class:`SpanState` for ``(profile, file_id)``.

    A missing manifest reads as an empty dict. A corrupt manifest raises
    :class:`BaseStoreError`; it is NEVER silently treated as empty.
    """
    target = _resolve_target(profile, file_id)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as err:
        raise BaseStoreIOError(
            f"failed to read spans manifest for {profile}/{file_id}: {err}"
        ) from err
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as err:
        raise BaseStoreError(
            f"corrupt spans manifest for {profile}/{file_id}: {err}"
        ) from err
    if not isinstance(parsed, dict):
        raise BaseStoreError(
            f"corrupt spans manifest for {profile}/{file_id}: top level must be "
            f"an object, got {type(parsed).__name__}"
        )
    return {
        anchor: _decode_state(anchor, record, profile, file_id)
        for anchor, record in parsed.items()
    }


def _write_manifest(profile: str, file_id: str, manifest: _Manifest) -> None:
    """Atomically serialize and write ``manifest`` for ``(profile, file_id)``."""
    target = _resolve_target(profile, file_id)
    payload = {anchor: state.to_dict() for anchor, state in manifest.items()}
    text = json.dumps(payload, indent=2, sort_keys=True)
    try:
        atomicio.atomic_write_text(target, text + "\n")
    except OSError as err:
        raise BaseStoreIOError(
            f"failed to write spans manifest for {profile}/{file_id}: {err}"
        ) from err


def set_states(profile: str, file_id: str, states: _Manifest) -> None:
    """Record :class:`SpanState` for every anchor in ``states``.

    Does ONE read-modify-write of the whole manifest: untouched anchors
    are preserved, each given anchor is overwritten. Performing every
    update in a single write closes the lost-update race a per-anchor
    write would open.
    """
    manifest = get_states(profile, file_id)
    manifest.update(states)
    _write_manifest(profile, file_id, manifest)


def prune(profile: str, file_id: str, live_anchors: set[str]) -> None:
    """Drop manifest entries for ``(profile, file_id)`` not in ``live_anchors``.

    Strictly scoped to the one manifest. An anchor absent from
    ``live_anchors`` has its record REMOVED (a span that left the file's
    intent). A missing manifest is a no-op.
    """
    manifest = get_states(profile, file_id)
    pruned = {anchor: st for anchor, st in manifest.items() if anchor in live_anchors}
    if pruned != manifest:
        _write_manifest(profile, file_id, pruned)
