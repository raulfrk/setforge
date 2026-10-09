"""Transition records: per-invocation undo support for install/sync.

Each state-changing command (install, sync, revert) writes a directory
under ``~/.local/state/setforge/transitions/`` containing:

- ``meta.json`` — command, profile, UTC timestamp, host, setforge version
- ``extensions.json`` — added/removed extension IDs (omitted if no delta)
- ``plugins.json`` — installed / enabled / disabled plugin IDs plus
  added / removed marketplaces, and the plugins a revert uninstalled
  (omitted if no plugin delta)
- ``mcp.json`` — added / updated MCP-server registrations (omitted if no
  MCP delta)
- ``filesystem_deltas.json`` — pre/post images (kind, bytes, link target,
  mode) of every file, symlink and directory the command changed (omitted
  if none); marked ``complete`` because it covers every file change.
- ``state_snapshots/`` — pre-command per-host store state (byte bases,
  spans sidecars, scalar-base manifests) as a ``manifest.json`` plus
  numbered raw-byte payload files (omitted when nothing was captured)

A subsequent ``setforge revert`` consumes the most recent transition for
a profile, restores every recorded file's pre image once each still holds
its post image, restores the snapshotted store state, reverses the
extension, plugin and MCP deltas, and records its own reverse transition.
Records of earlier versions kept file changes as a ``changes.patch`` text
diff plus ``file_modes.json``; revert refuses those (see
:func:`refuse_legacy_file_changes`).
"""

import base64
import binascii
import json
import os
import platform
import shutil
import stat
import subprocess
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, NewType

from pydantic import ValidationError

from setforge import __version__, atomicio, base_store, scalar_base_store
from setforge.errors import (
    InvalidTransitionRecord,
    ReconcileStoreError,
    RevertFailed,
    SetforgeError,
)
from setforge.ownership import (
    ClaimEvent,
    OwnershipClaim,
    ownership_claim_from_json,
    ownership_claim_to_json,
)
from setforge.paths import state_root
from setforge.reconcile import store as reconcile_store
from setforge.reconcile.types import resolve_store_path

TransitionDir = NewType("TransitionDir", Path)
"""A directory holding one transition record (``meta.json`` and its sidecars).

Constructed only by ``setforge.transitions`` factory functions. Consumers
accepting a ``TransitionDir`` get type-check protection that raw ``Path``
values are rejected at static-analysis time.
"""


class TransitionCommand(StrEnum):
    """Closed set of state-changing commands that record transitions."""

    INSTALL = "install"
    STAGE = "stage"
    SYNC = "sync"
    REVERT = "revert"
    MERGE = "merge"
    CLEANUP_ORPHANS = "cleanup-orphans"
    PROMOTE = "promote"
    MIGRATE = "migrate"


class FilesystemKind(StrEnum):
    """Portable filesystem kinds stored in a transition delta."""

    ABSENT = "absent"
    FILE = "file"
    SYMLINK = "symlink"
    DIRECTORY = "directory"


@dataclass(frozen=True, slots=True)
class FilesystemImage:
    """Exact state of one transition-managed filesystem leaf."""

    kind: FilesystemKind
    payload: bytes | None = None
    link_target: str | None = None
    mode: int | None = None
    mtime_ns: int | None = None


@dataclass(frozen=True, slots=True)
class FilesystemDelta:
    """Pre/post images for one arbitrary-byte file or symlink mutation."""

    path: Path
    pre: FilesystemImage
    post: FilesystemImage


@dataclass(frozen=True, slots=True)
class OwnershipTransferDelta:
    """Exact claim states on the two sides of one ownership transfer."""

    before: "OwnershipClaim"
    after: "OwnershipClaim"

    def __post_init__(self) -> None:
        if self.before.owner_id == self.after.owner_id:
            raise ValueError("ownership transfer must change owner")

        expected = replace(
            self.before,
            owner_id=self.after.owner_id,
            declaration_refs=self.after.declaration_refs,
            generation=self.before.generation + 1,
            history=(
                *self.before.history,
                ClaimEvent("transfer", self.after.owner_id, self.before.generation + 1),
            ),
        )
        if self.after != expected:
            raise ValueError("ownership transfer is not an exact claim successor")


# The profile label recorded on a ``migrate`` transition. A schema migration
# is profile-agnostic (it mutates setforge.yaml / shared content, not a
# profile-specific deploy), so it is recorded under this fixed label rather
# than a real ``setforge.yaml`` profile name — and the revert side tolerates
# a label that does not resolve to a config profile. Lives here (not in the
# migrate command) so revert can reference it without importing the command.
MIGRATE_TRANSITION_PROFILE: Final[str] = "migrate"


_STALE_PENDING_AGE = timedelta(hours=24)


def transitions_root() -> Path:
    """Directory that holds every transition record for this host."""
    return state_root() / "transitions"


def ensure_state_dir_writable() -> None:
    """Probe the transition state dir for writability.

    Called at the top of state-changing commands so install/sync fail
    fast with a clear error before mutating live files. If the dir is
    not writable (permissions, disk full, parent missing) the user
    would otherwise end up with applied changes and no transition
    record — no revert path.
    """
    root = transitions_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".setforge-write-probe"
        probe.touch()
        probe.unlink()
    except OSError as exc:
        raise SetforgeError(
            f"transition state dir not writable: {root} ({exc})"
        ) from exc


def validate_state_dir_writable() -> None:
    """Read-only preflight for the nearest existing transition-state parent."""
    candidate = transitions_root()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    if not candidate.is_dir() or not os.access(candidate, os.W_OK | os.X_OK):
        raise SetforgeError(
            f"transition state dir not writable: {transitions_root()} "
            f"(nearest existing parent {candidate})"
        )


def now_utc() -> datetime:
    """Single source of truth for transition timestamps."""
    return datetime.now(UTC)


def transition_dirname(timestamp: datetime, command: str, profile: str) -> str:
    """Return the directory name for one transition.

    Format: ``YYYYMMDDTHHMMSSffffffZ-<command>-<profile>`` (microseconds
    appended; ``ffffff`` is six-digit zero-padded microseconds) so that
    lexicographic sort matches chronological sort and ``load_latest`` is
    a single ``max()``. Microsecond precision avoids same-second
    dirname collisions when state-changing commands run rapidly.
    """
    iso = timestamp.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{iso}-{command}-{profile}"


@dataclass(frozen=True, slots=True)
class TransitionMeta:
    """Metadata for one transition. Serialized to ``meta.json``.

    ``source_sha`` records the config-repo HEAD at install time so
    ``setforge status`` can compute ``commits-since-last-install``.
    It is ``None`` for transitions recorded before the
    schema bump and for transitions whose source directory is not a git
    repo; :meth:`to_dict` omits the key entirely when ``None`` so old
    meta.json files round-trip byte-identically through load + re-dump.

    The trailing two fields (``end_timestamp``, ``command_line``) were
    added in a later schema bump so ``setforge transitions show`` can
    display them. Both follow the same omit-when-None pattern as
    ``source_sha`` so old meta.json files (recorded before the bump) still
    round-trip byte-identically.
    """

    command: TransitionCommand
    profile: str
    timestamp: datetime  # UTC; serialized as ISO 8601
    host: str  # platform.node()
    version: str  # setforge.__version__
    source_sha: str | None = (
        None  # config-repo HEAD at install time; None pre-source-sha
    )
    # all None pre-bump. List/bool/str all use the None sentinel
    # (NOT default_factory=list) so slots=True doesn't allocate a per-instance
    # default container. See TransitionMeta docstring for the omit-when-None
    # round-trip rationale.
    end_timestamp: str | None = None
    command_line: list[str] | None = None

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "command": self.command.value,
            "profile": self.profile,
            "timestamp": self.timestamp.astimezone(UTC).isoformat(),
            "host": self.host,
            "version": self.version,
        }
        if self.source_sha is not None:
            out["source_sha"] = self.source_sha
        if self.end_timestamp is not None:
            out["end_timestamp"] = self.end_timestamp
        if self.command_line is not None:
            # Defensive copy: ``command_line`` is ``list[str]`` and
            # ``frozen=True`` only freezes attribute *rebinding*, not
            # list mutation through the attribute reference.
            out["command_line"] = list(self.command_line)
        return out


def _git_head(source_dir: Path) -> str | None:
    """Return the HEAD commit sha of ``source_dir`` or ``None``.

    Used by :func:`make_meta` to record the config-repo state at install
    time. Returns ``None`` when ``source_dir`` is not a
    git repo, when ``git`` is not on ``PATH``, or when the subprocess
    fails for any reason — the field is informational, not load-bearing,
    and a missing value is the documented "no provenance" state.
    """
    git_bin = shutil.which("git")
    if git_bin is None:
        return None
    try:
        result = subprocess.run(
            [git_bin, "-C", str(source_dir), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def make_meta(
    command: TransitionCommand,
    profile: str,
    *,
    source_dir: Path | None = None,
    record_end: bool = False,
    command_line: list[str] | None = None,
) -> TransitionMeta:
    """Build a TransitionMeta with current host + version + UTC timestamp.

    When ``source_dir`` is provided AND is a git repo, records its HEAD
    commit sha as ``source_sha`` so ``setforge status`` can compute
    ``commits-since-last-install``. Otherwise leaves
    ``source_sha`` as ``None``; callers that don't have a source dir
    handy (revert, plugin reconcile sub-record) keep the pre-bump call
    shape.

    ``record_end`` stamps ``end_timestamp`` here, after ``timestamp``, so a
    record's end is never earlier than its start. It and ``command_line``
    default to off so the fields stay absent from records that never carried
    them. See the TransitionMeta docstring for the omit-when-None round-trip
    rationale.
    """
    timestamp = now_utc()
    source_sha = _git_head(source_dir) if source_dir is not None else None
    return TransitionMeta(
        command=command,
        profile=profile,
        timestamp=timestamp,
        host=platform.node(),
        version=__version__,
        source_sha=source_sha,
        end_timestamp=now_utc().isoformat() if record_end else None,
        command_line=command_line,
    )


def load_meta_payload(transition_dir: Path) -> dict[str, Any]:
    """Read ``<transition_dir>/meta.json`` as a JSON object or refuse it cleanly."""
    payload_path = transition_dir / "meta.json"
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InvalidTransitionRecord(
            f"cannot read meta.json at {payload_path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise InvalidTransitionRecord(
            f"meta.json at {payload_path} is not a JSON object"
        )
    return payload


def _meta_from_payload(payload: dict[str, Any], payload_path: Path) -> TransitionMeta:
    try:
        return TransitionMeta(
            command=TransitionCommand(payload["command"]),
            profile=str(payload["profile"]),
            timestamp=datetime.fromisoformat(payload["timestamp"]),
            host=str(payload["host"]),
            version=str(payload["version"]),
            source_sha=payload.get("source_sha"),
            # optional, all None pre-bump. .get() (NOT payload[<field>]) so
            # dozens of existing transition records written before the bump
            # still load cleanly. See TransitionMeta docstring for the
            # omit-when-None round-trip rationale.
            end_timestamp=payload.get("end_timestamp"),
            command_line=payload.get("command_line"),
        )
    except (KeyError, ValueError) as exc:
        raise InvalidTransitionRecord(
            f"meta.json at {payload_path} is missing or malformed: {exc}"
        ) from exc


def load_meta(transition_dir: TransitionDir) -> TransitionMeta:
    """Load and parse ``<transition_dir>/meta.json`` into a :class:`TransitionMeta`.

    Reads the JSON payload written by :func:`write_meta` and reconstructs
    the dataclass. Falls back to ``source_sha = None`` for transitions
    recorded before the schema bump (no ``source_sha`` key
    in the payload). Raises :class:`InvalidTransitionRecord` on missing
    or malformed required fields; on ``ValueError`` from
    :class:`TransitionCommand` membership or
    :func:`datetime.fromisoformat`, the raised error wraps the original
    exception so the caller sees both.
    """
    return _meta_from_payload(
        load_meta_payload(transition_dir), transition_dir / "meta.json"
    )


def _write_text_durable(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` and fsync the file's own fd.

    Power-loss durability requires the file's data — not just the
    directory entry — to reach disk. ``Path.write_text`` closes the fd
    before any fsync is possible, so this opens the file directly,
    writes, ``flush``-es the userspace buffer (``os.fsync`` only syncs
    kernel buffers, not Python's), then ``os.fsync``-s the fd. A failing
    data fsync (e.g. ``ENOSPC``) propagates — a swallowed data-fsync
    error would report durable when it isn't.
    """
    with path.open("w", encoding="utf-8", errors="surrogateescape", newline="") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())


def _write_bytes_durable(path: Path, data: bytes) -> None:
    """Binary sibling of :func:`_write_text_durable` (same fsync contract).

    Used for the staged ``state_snapshots/<n>.payload`` files, which carry
    verbatim store bytes and must not pass through a text encode/decode.
    """
    with path.open("wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def write_meta(
    transition_dir: TransitionDir,
    meta: TransitionMeta,
    paths: list[Path] | None = None,
    tracked_file_destinations: Mapping[str, tuple[Path, ...]] | None = None,
) -> None:
    """Serialize ``meta`` to ``<transition_dir>/meta.json``.

    If ``paths`` is provided, every absolute path is recorded in a
    ``paths`` field on the JSON payload so :func:`load_latest` can
    identify the touched files without re-parsing the diff and so
    ``revert`` can snapshot pre/post state directly. Creates
    ``transition_dir`` (with parents) if needed. The meta.json fd is
    fsynced (via :func:`_write_text_durable`) so the commit marker's data
    is power-loss durable.

    ``tracked_file_destinations`` optionally retains declaration identities
    for orphan-ignore decisions after a definition is removed. Older records
    omit it and remain readable.
    """
    transition_dir.mkdir(parents=True, exist_ok=True)
    body: dict[str, object] = dict(meta.to_dict())
    if paths is not None:
        body["paths"] = [str(p) for p in paths]
    if tracked_file_destinations:
        body["tracked_file_destinations"] = {
            name: [str(path.absolute()) for path in destinations]
            for name, destinations in sorted(tracked_file_destinations.items())
        }
    payload = json.dumps(body, indent=2) + "\n"
    _write_text_durable(transition_dir / "meta.json", payload)


def snapshot_paths(paths: Iterable[Path]) -> dict[Path, str | None]:
    """Read every path in ``paths``. Missing files map to ``None``.

    Decodes the raw bytes as UTF-8 with ``surrogateescape`` and no newline
    translation, so two snapshots compare equal only when the bytes do (see
    :func:`setforge.deploy.read_text_exact`).
    """
    out: dict[Path, str | None] = {}
    for p in paths:
        try:
            out[p] = p.read_bytes().decode("utf-8", "surrogateescape")
        except FileNotFoundError:
            out[p] = None
    return out


@dataclass(frozen=True, slots=True)
class ExtensionDelta:
    """Net successful changes to the installed extension set during a
    state-changing command. Failed installs/uninstalls are excluded so
    revert never tries to reverse a no-op."""

    added: list[str]  # successfully installed during the command
    removed: list[str]  # successfully uninstalled during the command

    def is_empty(self) -> bool:
        return not (self.added or self.removed)


@dataclass(slots=True, frozen=True)
class PluginDelta:
    """Net successful changes to the Claude plugin / marketplace surface
    during a state-changing command.

    Five fields (vs. ``ExtensionDelta``'s two) because plugin state has
    three independent reconciler actions (install, enable, disable)
    whereas extension state has only two (install, uninstall):

    - ``installed`` — plugin IDs (``"<name>@<marketplace>"``) that
      transitioned from absent to present.
    - ``enabled``   — plugin IDs that flipped ``enabled: False → True``.
    - ``disabled``  — plugin IDs that flipped ``enabled: True → False``.
    - ``marketplaces_added``   — marketplace names registered.
    - ``marketplaces_removed`` — ``(name, source_repr)`` pairs where
      ``source_repr`` is a dict with the :class:`MarketplaceSource`
      fields (``source`` + exactly one of ``repo`` / ``path``). The
      pair shape preserves enough info to rebuild a
      :class:`MarketplaceSource` and call :func:`marketplace_add` at
      revert time; flat names alone would not be invertible.

      **JSON-primitive contract.** The ``source_repr`` dict MUST
      contain only JSON-safe primitive values (``str`` / ``int`` /
      ``bool`` / ``None``). Callers populating this field from a
      live :class:`setforge.config.MarketplaceSource` MUST serialize
      via ``MarketplaceSource.model_dump(mode="json")`` (or
      equivalent) — the raw model has an enum ``source`` field and an
      optional :class:`pathlib.Path` ``path`` field, neither of which
      survives ``json.dumps``. The static annotation
      ``dict[str, str]`` documents the string-value subset that
      today's serialized shape produces (``source`` kind +
      ``repo``/``path`` are all strings post-serialization), and
      :func:`write_transition` raises :class:`TypeError` defensively
      on any non-string value to surface contract violations loudly.

    - ``uninstalled`` — ``(plugin_id, was_enabled)`` pairs for plugins
      that transitioned from present to absent, with the enabled state
      each had beforehand. Only a revert writes it (install never
      uninstalls), so that reverting that revert can reinstall the plugin
      as it was. Stored as an optional ``uninstalled`` key that is omitted
      when empty; a release without the field ignores the key.

    Failed plugin operations are excluded so revert never tries to
    reverse a no-op, mirroring :class:`ExtensionDelta`'s contract.
    """

    installed: tuple[str, ...]
    enabled: tuple[str, ...]
    disabled: tuple[str, ...]
    marketplaces_added: tuple[str, ...]
    marketplaces_removed: tuple[tuple[str, dict[str, str]], ...]
    uninstalled: tuple[tuple[str, bool], ...] = ()

    def is_empty(self) -> bool:
        return not (
            self.installed
            or self.enabled
            or self.disabled
            or self.marketplaces_added
            or self.marketplaces_removed
            or self.uninstalled
        )


@dataclass(slots=True, frozen=True)
class CodexPluginDelta:
    """Successful Codex plugin and marketplace mutations.

    Codex has no native enable/disable operation, and this deliberately lives
    apart from :class:`PluginDelta` so revert cannot dispatch it through the
    Claude adapter.
    """

    installed: tuple[str, ...]
    removed: tuple[str, ...]
    marketplaces_added: tuple[str, ...]
    marketplaces_removed: tuple[tuple[str, dict[str, str]], ...]

    def is_empty(self) -> bool:
        return not (
            self.installed
            or self.removed
            or self.marketplaces_added
            or self.marketplaces_removed
        )


@dataclass(slots=True, frozen=True)
class MCPDelta:
    """Net successful changes to the registered MCP-server set during a
    state-changing command.

    Two fields because an MCP reconcile has two invertible actions:

    - ``added`` — ``(name, command, scope)`` triples for servers
      registered this command, including replacement endpoints. The inverse is
      ``claude mcp remove``; the command + scope are nonetheless stored
      (not just the name) so a redo — a revert of the revert — can re-add
      the exact registration, making the round-trip closed.
    - ``updated`` — ``(name, prior_command, prior_scope)`` triples for
      servers whose prior registration was successfully removed, even if
      its replacement failed. The PRIOR command + scope is stored because the
      inverse is re-adding the original registration — a flat name list
      would be non-invertible. Follows
      :class:`PluginDelta.marketplaces_removed`'s (name, repr) precedent.

    Records ONLY successfully-applied operations so revert never tries to
    reverse a no-op, mirroring :class:`ExtensionDelta` / :class:`PluginDelta`.
    An empty delta serializes to no ``mcp.json`` at all (omit-when-empty).
    """

    added: tuple[tuple[str, tuple[str, ...], str], ...]
    updated: tuple[tuple[str, tuple[str, ...], str], ...]
    # Absent on legacy records: they decode, but cannot prove native destinations.
    context: tuple[str, str, str] | None = None

    def is_empty(self) -> bool:
        return not (self.added or self.updated)

    @property
    def scopes(self) -> frozenset[str]:
        return frozenset(scope for _, _, scope in (*self.added, *self.updated))


class ReconcileKind(StrEnum):
    """Closed set of item kinds a :class:`ReconcileOutcome` can record.

    StrEnum (not bare ``Literal[...]``) per CLAUDE.md's
    "StrEnum / IntEnum for closed sets — never bare module-level magic
    strings" rule. Members compare equal to their string values, so tests
    that assert ``outcome.kind == "plugin"`` keep working unchanged.
    """

    PLUGIN = "plugin"
    EXTENSION = "extension"


class ReconcileStatus(StrEnum):
    """Closed set of per-item outcome statuses on a reconcile pass.

    StrEnum (not bare ``Literal[...]``) for the same reason as
    :class:`ReconcileKind`. ``OK`` covers first-attempt successes;
    ``RETRIED_OK`` second-attempt successes after the user picked
    RETRY at the failure prompt; ``SKIPPED`` items the user opted to
    leave behind.
    """

    OK = "ok"
    RETRIED_OK = "retried_ok"
    SKIPPED = "skipped"


@dataclass(slots=True, frozen=True)
class ReconcileOutcome:
    """One per-item outcome from a plugin or extension reconcile pass.

    Held in memory for the run that produced it: it drives the failure
    summary and the install exit status, and is never written to the
    transition record. A ``reconcile_outcomes.json`` left in a record by an
    earlier release is ignored.
    """

    item_id: str
    kind: ReconcileKind
    status: ReconcileStatus
    error_summary: str | None


class SnapshotStore(StrEnum):
    """Closed set of per-host state stores a transition can snapshot.

    The string values double as the store's directory name under
    :func:`state_root`, so the on-disk ``state_snapshots/manifest.json``
    records exactly the subtree each entry restores into.
    """

    BASE = "base"
    SPANS = "spans"
    SCALAR_BASE = "scalar-base"
    # Reconcile store (A5/A5c): local keep-content + its absence marker (two legs
    # of one logical entry), the per-fid drafts manifest, and the per-profile
    # classification index. Without these, a revert of an install/sync that wrote
    # the reconcile store left local/index/drafts AHEAD of the reverted tracked src.
    LOCAL_CONTENT = "local"
    LOCAL_ABSENT = "local-absent"
    DRAFTS = "drafts"
    INDEX = "index"


@dataclass(frozen=True, slots=True)
class StateSnapshotEntry:
    """Pre-command state of ONE per-host store entry.

    ``key`` is the tracked-file id (``expand_tracked_file``'s ``sub_name``)
    the store keys its files by. ``payload`` carries the entry's verbatim
    bytes; ``None`` means the entry did NOT exist at capture time —
    distinct from ``b""`` (an existing empty file), and checked ONLY via
    ``is None`` so the two states never collapse through truthiness.
    Restoring a ``None`` entry DELETES the store file; restoring a bytes
    entry rewrites it byte-exact.
    """

    store: SnapshotStore
    profile: str
    key: str
    payload: bytes | None


_STATE_SNAPSHOTS_DIRNAME: Final[str] = "state_snapshots"
_STATE_SNAPSHOTS_MANIFEST: Final[str] = "manifest.json"


def _spans_manifest_path(profile: str, key: str) -> Path:
    """SPANS sidecar manifest path, guarding traversal.

    Applies the store-wide path guard
    (:func:`setforge.reconcile.types.resolve_store_path`) to the retired
    ``spans_store`` layout without importing that module: an unsafe ``profile``
    or ``key``, or a resolved path outside ``spans/``, is rejected. A
    hand-edited ``store="spans"`` transition record therefore can never
    write a payload outside the subtree on revert (the same threat model
    :func:`_validate_one_state_snapshot` guards ``payload_file`` against).
    """
    try:
        return resolve_store_path(state_root() / "spans", profile, key, suffix=".json")
    except ReconcileStoreError as err:
        raise InvalidTransitionRecord(str(err)) from err


def _snapshot_target(store: SnapshotStore, profile: str, key: str) -> Path:
    """Resolve one snapshot entry to its on-disk store path.

    Delegates to each surviving store module's public path accessor so its
    traversal guard (relative key, no ``..``, stays inside the profile
    subtree) and suffix convention live in one place. The ``SPANS`` store is the
    retired legacy sidecar, kept only so pre-existing ``store="spans"``
    transitions still restore byte-exact — its guarded manifest path is
    computed by :func:`_spans_manifest_path`, which applies the same store-wide
    path guard (:func:`setforge.reconcile.types.resolve_store_path`) rather than
    reaching through the retired module.
    """
    match store:
        case SnapshotStore.BASE:
            return base_store.base_path(profile, key)
        case SnapshotStore.SPANS:
            return _spans_manifest_path(profile, key)
        case SnapshotStore.SCALAR_BASE:
            return scalar_base_store.manifest_path(profile, key)
        case SnapshotStore.LOCAL_CONTENT:
            return reconcile_store.local_content_path(profile, key)
        case SnapshotStore.LOCAL_ABSENT:
            return reconcile_store.local_absent_path(profile, key)
        case SnapshotStore.DRAFTS:
            return reconcile_store.drafts_manifest_path(profile, key)
        case SnapshotStore.INDEX:
            # Profile-scoped: one index doc per profile, key-independent.
            return reconcile_store.index_manifest_path(profile)


def snapshot_store_state(
    store: SnapshotStore, profile: str, key: str
) -> StateSnapshotEntry:
    """Capture the CURRENT on-disk state of one store entry.

    A missing store file captures as ``payload=None`` (the absent state a
    later restore turns back into a deletion). Read errors other than
    absence propagate — a snapshot that silently recorded wrong state
    would corrupt the revert it exists to serve.
    """
    target = _snapshot_target(store, profile, key)
    try:
        payload: bytes | None = target.read_bytes()
    except FileNotFoundError:
        payload = None
    return StateSnapshotEntry(store=store, profile=profile, key=key, payload=payload)


def reconcile_file_snapshots(profile: str, key: str) -> list[StateSnapshotEntry]:
    """The reconcile-store leg snapshots for ONE file: local content, its absence
    marker, and the drafts manifest.

    Captures all three legs as one unit so a restore puts the local trichotomy
    (content / absent-marker / neither) + drafts back consistently. The BASE leg
    (shared with ``base_store``) and the per-profile INDEX are captured separately
    by the caller — BASE via :data:`SnapshotStore.BASE`, INDEX once per profile.
    """
    return [
        snapshot_store_state(store, profile, key)
        for store in (
            SnapshotStore.LOCAL_CONTENT,
            SnapshotStore.LOCAL_ABSENT,
            SnapshotStore.DRAFTS,
        )
    ]


def restore_state_snapshots(entries: Iterable[StateSnapshotEntry]) -> None:
    """Write every entry's captured state back into its store.

    Per entry: ``payload is None`` → the store file is unlinked
    (``missing_ok`` — it may already be gone); bytes → rewritten
    byte-exact via :func:`atomicio.atomic_write_bytes`. Both operations
    are idempotent, so an interrupted revert can safely re-run the whole
    restore.
    """
    for entry in entries:
        target = _snapshot_target(entry.store, entry.profile, entry.key)
        if entry.payload is None:
            target.unlink(missing_ok=True)
            atomicio.fsync_dir(target.parent)
        else:
            atomicio.atomic_write_bytes(target, entry.payload)


def _stage_state_snapshots(
    pending: Path, snapshots: tuple[StateSnapshotEntry, ...]
) -> None:
    """Stage the ``state_snapshots/`` payload inside the pending dir.

    No-op for the empty tuple so snapshot-free transitions keep the
    pre-bump on-disk shape. Present payloads land as numbered
    ``<n>.payload`` files referenced from ``manifest.json``; an absent
    entry records ``"payload_file": null`` (explicit, never inferred from
    a zero-length file). Every staged file fsyncs its own fd (durability
    parity with the other staged payloads); the dir fsync makes the child
    entries durable before the caller's rename commit.
    """
    if not snapshots:
        return
    snap_dir = pending / _STATE_SNAPSHOTS_DIRNAME
    snap_dir.mkdir()
    records: list[dict[str, object]] = []
    payload_index = 0
    for entry in snapshots:
        payload_file: str | None = None
        if entry.payload is not None:
            payload_file = f"{payload_index}.payload"
            payload_index += 1
            _write_bytes_durable(snap_dir / payload_file, entry.payload)
        records.append(
            {
                "store": entry.store.value,
                "profile": entry.profile,
                "key": entry.key,
                "payload_file": payload_file,
            }
        )
    _write_text_durable(
        snap_dir / _STATE_SNAPSHOTS_MANIFEST,
        json.dumps({"entries": records}, indent=2) + "\n",
    )
    atomicio.fsync_dir(snap_dir)


_VALID_SNAPSHOT_STORES: frozenset[str] = frozenset(s.value for s in SnapshotStore)


def _validate_one_state_snapshot(entry: object, snap_dir: Path) -> StateSnapshotEntry:
    """Validate one manifest record into a :class:`StateSnapshotEntry`.

    Raises :class:`InvalidTransitionRecord` on any shape deviation,
    including a ``payload_file`` that is missing on disk or carries a
    path separator (a hand-edited manifest must never read outside the
    snapshot dir).
    """
    if not isinstance(entry, dict):
        raise InvalidTransitionRecord(
            f"state_snapshots manifest: entry must be a dict, got "
            f"{type(entry).__name__}"
        )
    store = entry.get("store")
    profile = entry.get("profile")
    key = entry.get("key")
    payload_file = entry.get("payload_file")
    if store not in _VALID_SNAPSHOT_STORES:
        raise InvalidTransitionRecord(
            f"state_snapshots manifest: store must be in "
            f"{sorted(_VALID_SNAPSHOT_STORES)}, got {store!r}"
        )
    if not isinstance(profile, str) or not isinstance(key, str):
        raise InvalidTransitionRecord(
            f"state_snapshots manifest: profile/key must be str, got "
            f"({type(profile).__name__}, {type(key).__name__})"
        )
    payload: bytes | None = None
    if payload_file is not None:
        if not isinstance(payload_file, str) or Path(payload_file).name != payload_file:
            raise InvalidTransitionRecord(
                f"state_snapshots manifest: malformed payload_file {payload_file!r}"
            )
        try:
            payload = (snap_dir / payload_file).read_bytes()
        except OSError as exc:
            raise InvalidTransitionRecord(
                f"state_snapshots manifest: cannot read payload {payload_file!r}: {exc}"
            ) from exc
    return StateSnapshotEntry(
        store=SnapshotStore(store), profile=profile, key=key, payload=payload
    )


def load_state_snapshots(
    transition_dir: TransitionDir,
) -> tuple[StateSnapshotEntry, ...] | None:
    """Return the state-snapshot entries for a transition directory.

    Returns ``None`` when the ``state_snapshots/`` dir is absent — the
    backward-compat sentinel for transitions written before this schema
    bump (revert then skips store restore entirely; the deliberate
    ``None``-vs-``()`` distinction mirrors how the entries themselves
    encode absent-vs-empty). Raises :class:`InvalidTransitionRecord` when
    the dir exists but the manifest is missing or its shape is corrupt.
    """
    snap_dir = transition_dir / _STATE_SNAPSHOTS_DIRNAME
    if not snap_dir.is_dir():
        return None
    manifest = snap_dir / _STATE_SNAPSHOTS_MANIFEST
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InvalidTransitionRecord(
            f"cannot read state_snapshots manifest at {manifest}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise InvalidTransitionRecord(
            f"state_snapshots manifest at {manifest}: top-level must be a "
            f"dict, got {type(raw).__name__}"
        )
    entries = raw.get("entries")
    if not isinstance(entries, list):
        raise InvalidTransitionRecord(
            f"state_snapshots manifest at {manifest}: entries must be a "
            f"list, got {type(entries).__name__}"
        )
    return tuple(_validate_one_state_snapshot(entry, snap_dir) for entry in entries)


def _validated_str_list(raw: object, *, key: str, source_label: str) -> list[str]:
    """Return a validated ``list[str]`` built from ``raw``.

    Raises :class:`InvalidTransitionRecord` on any shape deviation.
    Used by the JSON-boundary readers below to validate fields that
    must be lists of strings (``installed``, ``enabled``, ``added``,
    etc.). ``key`` names the field for error messages; ``source_label``
    names the on-disk file (e.g. ``"plugins.json"``). Returns a fresh
    list (not the input object) so the caller never aliases the
    JSON-deserialized payload.
    """
    if not isinstance(raw, list):
        raise InvalidTransitionRecord(
            f"{source_label}: {key} must be a list, got {type(raw).__name__}"
        )
    validated: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            raise InvalidTransitionRecord(
                f"{source_label}: {key} entry has wrong type: {type(entry).__name__}"
            )
        validated.append(entry)
    return validated


def plugin_delta_from_json(raw: dict[str, object]) -> PluginDelta:
    """Reconstruct a :class:`PluginDelta` from a JSON-deserialized
    ``plugins.json`` record. Inverse of the on-disk shape produced by
    :func:`write_transition`.

    Validates ``marketplaces_removed`` entries against the
    ``[str, dict]`` pair shape :func:`write_transition` writes, raising
    :class:`InvalidTransitionRecord` on any deviation. Without this
    guard a corrupted plugins.json (hand-edit, partial write, or a
    bug in a future writer) would surface as an opaque
    :class:`ValueError` at the tuple-unpack in
    :func:`_apply_marketplace_re_add`, aborting revert mid-flight.
    With the guard, the failure is caught cleanly at the
    ``SetforgeError`` boundary before any inverse op runs.

    Other list-of-string fields are validated via
    :func:`_validated_str_list`, which raises the same
    :class:`InvalidTransitionRecord` on shape deviation.
    """
    marketplaces_removed_raw = raw.get("marketplaces_removed", [])
    if not isinstance(marketplaces_removed_raw, list):
        raise InvalidTransitionRecord(
            f"plugins.json: marketplaces_removed must be a list, got "
            f"{type(marketplaces_removed_raw).__name__}"
        )
    validated_pairs: list[tuple[str, dict[str, str]]] = []
    for entry in marketplaces_removed_raw:
        if not (isinstance(entry, list) and len(entry) == 2):
            raise InvalidTransitionRecord(
                f"plugins.json: malformed marketplaces_removed entry: {entry!r}"
            )
        name, payload = entry
        if not isinstance(name, str) or not isinstance(payload, dict):
            raise InvalidTransitionRecord(
                f"plugins.json: marketplaces_removed entry has wrong types: "
                f"({type(name).__name__}, {type(payload).__name__})"
            )
        validated_pairs.append((name, dict(payload)))

    uninstalled_raw = raw.get("uninstalled", [])
    if not isinstance(uninstalled_raw, list):
        raise InvalidTransitionRecord(
            f"plugins.json: uninstalled must be a list, got "
            f"{type(uninstalled_raw).__name__}"
        )
    uninstalled: list[tuple[str, bool]] = []
    for entry in uninstalled_raw:
        if not (
            isinstance(entry, list)
            and len(entry) == 2
            and isinstance(entry[0], str)
            and isinstance(entry[1], bool)
        ):
            raise InvalidTransitionRecord(
                f"plugins.json: malformed uninstalled entry: {entry!r}"
            )
        uninstalled.append((entry[0], entry[1]))

    def _str_list_field(key: str) -> tuple[str, ...]:
        return tuple(
            _validated_str_list(raw.get(key, []), key=key, source_label="plugins.json")
        )

    return PluginDelta(
        installed=_str_list_field("installed"),
        enabled=_str_list_field("enabled"),
        disabled=_str_list_field("disabled"),
        marketplaces_added=_str_list_field("marketplaces_added"),
        marketplaces_removed=tuple(validated_pairs),
        uninstalled=tuple(uninstalled),
    )


def codex_plugin_delta_from_json(raw: dict[str, object]) -> CodexPluginDelta:
    """Decode and validate a ``codex_plugins.json`` transition payload."""
    source_label = "codex_plugins.json"
    removed_raw = raw.get("marketplaces_removed", [])
    if not isinstance(removed_raw, list):
        raise InvalidTransitionRecord(
            f"{source_label}: marketplaces_removed must be a list"
        )
    pairs: list[tuple[str, dict[str, str]]] = []
    for entry in removed_raw:
        if not (isinstance(entry, list) and len(entry) == 2):
            raise InvalidTransitionRecord(
                f"{source_label}: malformed marketplaces_removed entry: {entry!r}"
            )
        name, source = entry
        if (
            not isinstance(name, str)
            or not isinstance(source, dict)
            or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in source.items()
            )
        ):
            raise InvalidTransitionRecord(
                f"{source_label}: invalid marketplace source entry"
            )
        try:
            from setforge import codex_plugins
            from setforge.config import MarketplaceSource

            parsed_source = MarketplaceSource.model_validate(source)
            codex_plugins.validate_marketplace_source(parsed_source)
        except (ValidationError, ValueError, SetforgeError) as exc:
            raise InvalidTransitionRecord(
                f"{source_label}: invalid marketplace source entry: {name!r}"
            ) from exc
        pairs.append((name, dict(source)))
    delta = CodexPluginDelta(
        installed=tuple(
            _validated_str_list(
                raw.get("installed", []), key="installed", source_label=source_label
            )
        ),
        removed=tuple(
            _validated_str_list(
                raw.get("removed", []), key="removed", source_label=source_label
            )
        ),
        marketplaces_added=tuple(
            _validated_str_list(
                raw.get("marketplaces_added", []),
                key="marketplaces_added",
                source_label=source_label,
            )
        ),
        marketplaces_removed=tuple(pairs),
    )
    for label, values in (
        ("installed", delta.installed),
        ("removed", delta.removed),
        ("marketplaces_added", delta.marketplaces_added),
        (
            "marketplaces_removed",
            tuple(name for name, _source in delta.marketplaces_removed),
        ),
    ):
        if len(values) != len(set(values)):
            raise InvalidTransitionRecord(f"{source_label}: duplicate {label} entry")
    if set(delta.installed) & set(delta.removed):
        raise InvalidTransitionRecord(
            f"{source_label}: plugin cannot be both installed and removed"
        )
    return delta


def extension_delta_from_json(raw: dict[str, object]) -> ExtensionDelta:
    """Reconstruct an :class:`ExtensionDelta` from a JSON-deserialized
    ``extensions.json`` record. Inverse of the on-disk shape produced
    by :func:`write_transition`.

    Validates ``added`` and ``removed`` are lists of strings via
    :func:`_validated_str_list`, raising :class:`InvalidTransitionRecord`
    on any deviation. Mirrors the boundary guard on
    :func:`plugin_delta_from_json`. Without this guard a
    corrupted extensions.json (hand-edit, partial write, or a bug in a
    future writer) would surface as an opaque :class:`TypeError` from
    a downstream ``iter()`` call rather than a clean
    :class:`SetforgeError` at the JSON boundary.
    """
    return ExtensionDelta(
        added=_validated_str_list(
            raw.get("added", []), key="added", source_label="extensions.json"
        ),
        removed=_validated_str_list(
            raw.get("removed", []), key="removed", source_label="extensions.json"
        ),
    )


_FILESYSTEM_DELTAS_FILENAME: Final[str] = "filesystem_deltas.json"
_OWNERSHIP_TRANSFERS_FILENAME: Final[str] = "ownership_transfers.json"


def _serialize_ownership_transfers(
    deltas: tuple[OwnershipTransferDelta, ...],
) -> str | None:
    if not deltas:
        return None
    identities = [item.before.resource_id for item in deltas]
    if len(identities) != len(set(identities)):
        raise SetforgeError("ownership transition resources must be unique")
    return (
        json.dumps(
            {
                "schema_version": 1,
                "entries": [
                    {
                        "before": ownership_claim_to_json(item.before),
                        "after": ownership_claim_to_json(item.after),
                    }
                    for item in deltas
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def load_ownership_transfers(
    transition_dir: TransitionDir,
) -> tuple[OwnershipTransferDelta, ...]:
    """Load the optional exact ownership-transfer sidecar."""
    path = transition_dir / _OWNERSHIP_TRANSFERS_FILENAME
    if not path.exists():
        return ()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise ValueError("unsupported ownership transfer schema")
        entries = raw.get("entries")
        if not isinstance(entries, list):
            raise TypeError("entries must be a list")
        deltas = tuple(_ownership_transfer_from_json(entry) for entry in entries)
        identities = [item.before.resource_id for item in deltas]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate ownership transfer resource")
        return deltas
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise InvalidTransitionRecord(
            f"invalid {_OWNERSHIP_TRANSFERS_FILENAME} at {path}: {exc}"
        ) from exc


def _ownership_transfer_from_json(raw: object) -> OwnershipTransferDelta:
    if not isinstance(raw, dict) or set(raw) != {"before", "after"}:
        raise TypeError("ownership transfer entry must contain before and after")
    before = raw["before"]
    after = raw["after"]
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise TypeError("ownership transfer claim states must be objects")
    return OwnershipTransferDelta(
        ownership_claim_from_json(before), ownership_claim_from_json(after)
    )


def _canonical_filesystem_path(path: Path) -> Path:
    """Return one-slash, lexical absolute identity without following symlinks."""
    absolute = os.fspath(path.expanduser().absolute())
    return Path(os.path.normpath(f"/{absolute.lstrip('/')}"))


class FilesystemChanged(OSError):
    """A filesystem entry changed while its image was being captured."""


def capture_filesystem_image(
    path: str | Path, *, dir_fd: int | None = None
) -> FilesystemImage | None:
    """Capture one entry without following it; ``None`` for an unsupported kind.

    ``path`` is taken relative to ``dir_fd`` when one is given. A change seen
    during the capture raises ``FilesystemChanged``. A symlink target is the
    raw stored text; path-based callers normalise it as ``Path.readlink`` does.
    """
    at = {} if dir_fd is None else {"dir_fd": dir_fd}
    try:
        before = os.stat(path, follow_symlinks=False, **at)  # noqa: PTH116 - optional dirfd
    except FileNotFoundError:
        return FilesystemImage(FilesystemKind.ABSENT)
    mode = stat.S_IMODE(before.st_mode)
    if stat.S_ISLNK(before.st_mode) or stat.S_ISDIR(before.st_mode):
        target = os.readlink(path, **at) if stat.S_ISLNK(before.st_mode) else None
        after = os.stat(path, follow_symlinks=False, **at)  # noqa: PTH116 - optional dirfd
        if stat_identity(before) != stat_identity(after):
            raise FilesystemChanged("entry changed while snapshotting")
        return FilesystemImage(
            FilesystemKind.DIRECTORY if target is None else FilesystemKind.SYMLINK,
            link_target=target,
            mode=mode,
            mtime_ns=before.st_mtime_ns,
        )
    if not stat.S_ISREG(before.st_mode):
        return None
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW, **at)
    try:
        if stat_identity(before) != stat_identity(os.fstat(fd)):
            raise FilesystemChanged("file changed before snapshot read")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            payload = stream.read()
        if stat_identity(before) != stat_identity(os.fstat(fd)):
            raise FilesystemChanged("file changed while snapshotting")
    finally:
        os.close(fd)
    return FilesystemImage(
        FilesystemKind.FILE, payload=payload, mode=mode, mtime_ns=before.st_mtime_ns
    )


def snapshot_filesystem_image(path: Path) -> FilesystemImage:
    """Capture an arbitrary-byte regular file or symlink without dereferencing."""
    path = _canonical_filesystem_path(path)
    try:
        image = capture_filesystem_image(path)
    except OSError as exc:
        raise SetforgeError(
            f"filesystem path changed while snapshotting {path}"
        ) from exc
    if image is None:
        raise SetforgeError(f"unsupported transition filesystem object: {path}")
    if image.link_target is not None:
        # Delta records hold the Path.readlink() spelling ("real/" as "real").
        image = replace(image, link_target=str(Path(image.link_target)))
    return image


def filesystem_deletion_deltas(paths: Iterable[Path]) -> tuple[FilesystemDelta, ...]:
    """Snapshot ``paths`` as reversible deletion deltas."""
    canonical = tuple(_canonical_filesystem_path(path) for path in paths)
    if len(canonical) != len(set(canonical)):
        raise SetforgeError("filesystem transition delta paths must be unique")
    return tuple(
        FilesystemDelta(
            path,
            snapshot_filesystem_image(path),
            FilesystemImage(FilesystemKind.ABSENT),
        )
        for path in canonical
    )


_ABSENT: Final[FilesystemImage] = FilesystemImage(FilesystemKind.ABSENT)


def capture_files(
    paths: Iterable[Path], *, strict: bool = False
) -> dict[Path, FilesystemImage]:
    """Capture what each path shows, keyed by the path as given.

    The image is taken at the symlink-resolved location, where a write through
    the path lands. Every missing directory above that location (outside
    SetForge's own state tree) is captured as absent too, so a later capture
    of the same keys records the directories a command created.
    ``strict=True`` is for configuration files (migration inputs), which are
    text: a file that is not valid UTF-8 raises :class:`SetforgeError`.
    """
    state = Path(os.path.realpath(state_root()))
    out: dict[Path, FilesystemImage] = {}
    for path in paths:
        resolved = Path(os.path.realpath(path))
        image = snapshot_filesystem_image(resolved)
        if strict and image.payload is not None:
            try:
                image.payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SetforgeError(
                    f"cannot snapshot {path}: file is not valid UTF-8"
                ) from exc
        out[path] = image
        for parent in resolved.parents:
            if parent.is_relative_to(state) or os.path.lexists(parent):
                break
            out.setdefault(parent, _ABSENT)
    return out


def images_match(left: FilesystemImage, right: FilesystemImage) -> bool:
    """Whether two images agree on kind, bytes, link target and mode."""
    return replace(left, mtime_ns=None) == replace(right, mtime_ns=None)


def changed_paths(
    pre: Mapping[Path, FilesystemImage], post: Mapping[Path, FilesystemImage]
) -> list[Path]:
    """Return the sorted paths whose image differs between ``pre`` and ``post``."""
    return sorted(
        (
            path
            for path in set(pre) | set(post)
            if not images_match(pre.get(path, _ABSENT), post.get(path, _ABSENT))
        ),
        key=str,
    )


def _file_deltas(
    pre: Mapping[Path, FilesystemImage], post: Mapping[Path, FilesystemImage]
) -> tuple[FilesystemDelta, ...]:
    """Deltas of the changed paths, recorded where each path resolves."""
    return tuple(
        dict.fromkeys(
            FilesystemDelta(
                Path(os.path.realpath(path)),
                pre.get(path, _ABSENT),
                post.get(path, _ABSENT),
            )
            for path in changed_paths(pre, post)
        )
    )


def reverse_filesystem_deltas(
    deltas: tuple[FilesystemDelta, ...],
) -> tuple[FilesystemDelta, ...]:
    """Record reversing ``deltas``: each path from its post image to what it holds now.

    Called once the reversal is complete, so a redo restores exactly the
    state the reversal replaced.
    """
    return tuple(
        FilesystemDelta(item.path, item.post, snapshot_filesystem_image(item.path))
        for item in deltas
    )


def stat_identity(info: os.stat_result) -> tuple[int, ...]:
    """Return the identity and mutable metadata one capture must see unchanged."""
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _image_to_json(image: FilesystemImage) -> dict[str, object]:
    return {
        "kind": image.kind.value,
        **(
            {"payload_b64": base64.b64encode(image.payload).decode("ascii")}
            if image.payload is not None
            else {}
        ),
        **({"link_target": image.link_target} if image.link_target is not None else {}),
        **({"mode": image.mode} if image.mode is not None else {}),
        **({"mtime_ns": image.mtime_ns} if image.mtime_ns is not None else {}),
    }


def _canonicalize_filesystem_deltas(
    deltas: tuple[FilesystemDelta, ...],
) -> tuple[FilesystemDelta, ...]:
    canonical = tuple(
        FilesystemDelta(
            _canonical_filesystem_path(item.path),
            item.pre,
            item.post,
        )
        for item in deltas
    )
    if len(canonical) != len({item.path for item in canonical}):
        raise SetforgeError("filesystem transition delta paths must be unique")
    return canonical


def _serialize_filesystem_deltas(deltas: tuple[FilesystemDelta, ...]) -> str | None:
    if not deltas:
        return None
    canonical_deltas = _canonicalize_filesystem_deltas(deltas)
    return (
        json.dumps(
            {
                "schema_version": 1,
                "complete": True,
                "entries": [
                    {
                        "path": str(item.path),
                        "pre": _image_to_json(item.pre),
                        "post": _image_to_json(item.post),
                    }
                    for item in canonical_deltas
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def load_filesystem_deltas(
    transition_dir: TransitionDir,
) -> tuple[FilesystemDelta, ...]:
    """Load and validate optional arbitrary-filesystem transition deltas."""
    return _load_filesystem_deltas(transition_dir)[0]


def file_images_complete(transition_dir: TransitionDir) -> bool:
    """Whether the record's ``filesystem_deltas.json`` covers every file change."""
    return _load_filesystem_deltas(transition_dir)[1]


def _load_filesystem_deltas(
    transition_dir: TransitionDir,
) -> tuple[tuple[FilesystemDelta, ...], bool]:
    """Return the deltas and whether they cover every file the record changed."""
    path = transition_dir / _FILESYSTEM_DELTAS_FILENAME
    if not path.exists():
        return (), False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise ValueError("unsupported filesystem delta schema")
        entries = raw.get("entries")
        if not isinstance(entries, list):
            raise TypeError("entries must be a list")
        deltas = tuple(_filesystem_delta_from_json(entry) for entry in entries)
        paths = [item.path for item in deltas]
        if len(paths) != len(set(paths)):
            raise ValueError("duplicate filesystem delta path")
        return deltas, raw.get("complete") is True
    except (
        OSError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        binascii.Error,
    ) as exc:
        raise InvalidTransitionRecord(
            f"invalid {_FILESYSTEM_DELTAS_FILENAME} at {path}: {exc}"
        ) from exc


def _filesystem_delta_from_json(raw: object) -> FilesystemDelta:
    if not isinstance(raw, dict):
        raise TypeError("filesystem delta entry must be an object")
    raw_path = raw.get("path")
    if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
        raise ValueError("filesystem delta path must be absolute text")
    normalized = os.path.normpath(raw_path)
    if (
        raw_path.startswith("//")
        or normalized != raw_path
        or ".." in Path(raw_path).parts
    ):
        raise ValueError("filesystem delta path must be lexically normalized")
    return FilesystemDelta(
        Path(normalized),
        _filesystem_image_from_json(raw.get("pre")),
        _filesystem_image_from_json(raw.get("post")),
    )


def _filesystem_image_from_json(raw: object) -> FilesystemImage:
    if not isinstance(raw, dict):
        raise TypeError("filesystem image must be an object")
    raw_kind = raw.get("kind")
    if not isinstance(raw_kind, str):
        raise TypeError("filesystem kind must be text")
    kind = FilesystemKind(raw_kind)
    payload_raw = raw.get("payload_b64")
    link_target = raw.get("link_target")
    mode = raw.get("mode")
    mtime_ns = raw.get("mtime_ns")
    if payload_raw is not None and not isinstance(payload_raw, str):
        raise TypeError("filesystem payload must be base64 text")
    if link_target is not None and not isinstance(link_target, str):
        raise TypeError("filesystem link target must be text")
    if mode is not None and (isinstance(mode, bool) or not isinstance(mode, int)):
        raise TypeError("filesystem mode must be an integer")
    if mtime_ns is not None and (
        isinstance(mtime_ns, bool) or not isinstance(mtime_ns, int)
    ):
        raise TypeError("filesystem mtime_ns must be an integer")
    payload = (
        base64.b64decode(payload_raw, validate=True)
        if payload_raw is not None
        else None
    )
    image = FilesystemImage(kind, payload, link_target, mode, mtime_ns)
    _validate_filesystem_image(image)
    return image


def _validate_filesystem_image(image: FilesystemImage) -> None:
    if image.kind is FilesystemKind.ABSENT:
        if any(
            value is not None
            for value in (image.payload, image.link_target, image.mode, image.mtime_ns)
        ):
            raise ValueError("absent filesystem image carries metadata")
        return
    if image.mode is None or image.mtime_ns is None:
        raise ValueError("present filesystem image lacks mode or mtime")
    if not 0 <= image.mode <= 0o7777:
        raise ValueError("filesystem mode out of range")
    if image.kind is FilesystemKind.FILE:
        if image.payload is None or image.link_target is not None:
            raise ValueError("invalid regular-file image")
    elif image.kind is FilesystemKind.SYMLINK:
        if image.payload is not None or image.link_target is None:
            raise ValueError("invalid symlink image")
    elif image.payload is not None or image.link_target is not None:
        raise ValueError("invalid directory image")


def validate_filesystem_deltas_reverse(*steps: tuple[FilesystemDelta, ...]) -> None:
    """Refuse drift before any inverse filesystem effect begins.

    ``steps`` are reversed in order: each path must hold the post image of the
    first step that touches it, and each later step is checked against the
    pre image the steps before it restore. Modification times are ignored.
    """
    expected: dict[Path, FilesystemImage] = {}
    for deltas in steps:
        for item in deltas:
            current = expected.get(item.path)
            if current is None:
                try:
                    current = snapshot_filesystem_image(item.path)
                except SetforgeError as exc:
                    raise RevertFailed(
                        f"filesystem path changed since transition: {item.path}"
                    ) from exc
            if not images_match(current, item.post):
                raise RevertFailed(
                    f"filesystem path changed since transition: {item.path}"
                )
        expected.update((item.path, item.pre) for item in deltas)


def write_transition(
    meta: TransitionMeta,
    file_pre: Mapping[Path, FilesystemImage],
    file_post: Mapping[Path, FilesystemImage],
    ext_delta: ExtensionDelta | None,
    plugin_delta: PluginDelta | None = None,
    state_snapshots: tuple[StateSnapshotEntry, ...] = (),
    mcp_delta: MCPDelta | None = None,
    filesystem_deltas: tuple[FilesystemDelta, ...] = (),
    codex_plugin_delta: CodexPluginDelta | None = None,
    ownership_transfers: tuple[OwnershipTransferDelta, ...] = (),
    tracked_file_destinations: Mapping[str, tuple[Path, ...]] | None = None,
    paths: Sequence[Path] | None = None,
) -> TransitionDir:
    """Write a complete transition directory under :func:`transitions_root`.

    Uses a two-phase write with atomic ``pending.rename(target)`` as the
    commit marker so
    a crash mid-write never leaves a half-formed transition visible to
    :func:`load_latest`. The sequence is power-loss durable: every staged
    payload file fsyncs its own fd before the rename, three distinct
    directory fsyncs (pending before rename, root after rename, target
    after meta.json) make the dir entries durable, and meta.json — the
    commit marker — is written and fsynced STRICTLY LAST, with nothing
    payload-related written after it.

    Write order: stage ``extensions.json`` (if delta non-empty),
    ``plugins.json`` (if delta non-empty), ``mcp.json`` (if delta
    non-empty), ``filesystem_deltas.json`` (if any file changed), and
    ``state_snapshots/`` (if non-empty) into a ``.pending-<dirname>/``
    staging dir; ``pending.rename(target)`` — atomic POSIX ``Path.rename``,
    same fs; write ``meta.json`` inside the now-real ``target/`` dir as
    the commit point. A crash before that final ``meta.json`` write
    leaves either a ``.pending-<dirname>/`` (skipped by
    :func:`load_latest` via the ``.pending-`` name guard) or a
    ``<dirname>/`` without ``meta.json`` (skipped by the existing
    meta.json filter).

    ``state_snapshots`` defaults to an empty tuple so the legacy call
    shapes stay backward-compatible; an empty
    ``state_snapshots`` writes no ``state_snapshots/`` dir at all, which
    :func:`load_state_snapshots` reads back as its ``None`` sentinel.

    ``file_pre`` / ``file_post`` are :func:`capture_files` images; every path
    whose image changed becomes a ``filesystem_deltas.json`` entry next to the
    caller's ``filesystem_deltas``, and the file marks itself complete.
    A directory the caller already records is not recorded twice.
    ``meta.json`` ``paths`` lists the paths as given whose file content changed
    plus the caller's delta paths, unless ``paths`` names them.

    Returns the absolute path of the committed directory.

    Raises:
        OSError: A data fsync of a staged payload file or of ``meta.json``
            (via :func:`_write_text_durable`) failed and propagates by
            design — swallowing it would falsely report the transition
            durable when its bytes never reached disk.
        TypeError: ``plugin_delta`` carries a ``marketplaces_removed``
            source dict with a non-str value (caller bypassed
            ``MarketplaceSource.model_dump(mode="json")``).
    """
    touched = (
        list(paths)
        if paths is not None
        else sorted(
            {
                *(
                    path
                    for path in changed_paths(file_pre, file_post)
                    if FilesystemKind.DIRECTORY
                    not in (
                        file_pre.get(path, _ABSENT).kind,
                        file_post.get(path, _ABSENT).kind,
                    )
                    and not images_match(
                        replace(file_pre.get(path, _ABSENT), mode=None),
                        replace(file_post.get(path, _ABSENT), mode=None),
                    )
                ),
                *(_canonical_filesystem_path(item.path) for item in filesystem_deltas),
            },
            key=str,
        )
    )
    # ``_file_deltas`` records a directory at its resolved location; tree code
    # records it as written. Compare in one form: resolved parent, unresolved leaf.
    covered = {
        Path(os.path.realpath(item.path.parent)) / item.path.name
        for item in filesystem_deltas
    }
    filesystem_deltas = _canonicalize_filesystem_deltas(
        (
            *filesystem_deltas,
            *(
                item
                for item in _file_deltas(file_pre, file_post)
                if Path(os.path.realpath(item.path.parent)) / item.path.name
                not in covered
                or FilesystemKind.DIRECTORY not in (item.pre.kind, item.post.kind)
            ),
        )
    )
    filesystem_payload = _serialize_filesystem_deltas(filesystem_deltas)
    ownership_transfer_payload = _serialize_ownership_transfers(ownership_transfers)
    root = transitions_root()
    dirname = transition_dirname(meta.timestamp, meta.command.value, meta.profile)
    target = TransitionDir(root / dirname)
    pending = root / f".pending-{dirname}"

    root.mkdir(parents=True, exist_ok=True)
    pending.mkdir(parents=True, exist_ok=False)

    # Durable write sequence (power-loss safe), meta.json STRICTLY LAST:
    # 1. write + fsync each staged payload file's own fd,
    # 2. fsync the pending dir (staged child-creates durable),
    # 3. rename pending -> target (atomic POSIX, same fs),
    # 4. fsync the root dir (target's new dir entry durable),
    # 5. write + fsync meta.json (the commit marker),
    # 6. fsync the target dir (meta.json's dir entry durable) — last.
    ext_payload = _serialize_ext_payload(ext_delta)
    if ext_payload is not None:
        _write_text_durable(pending / "extensions.json", ext_payload)

    plugin_payload = _serialize_plugin_payload(plugin_delta)
    if plugin_payload is not None:
        _write_text_durable(pending / "plugins.json", plugin_payload)

    codex_plugin_payload = _serialize_codex_plugin_payload(codex_plugin_delta)
    if codex_plugin_payload is not None:
        _write_text_durable(pending / "codex_plugins.json", codex_plugin_payload)

    mcp_payload = _serialize_mcp_payload(mcp_delta)
    if mcp_payload is not None:
        _write_text_durable(pending / "mcp.json", mcp_payload)

    if filesystem_payload is not None:
        _write_text_durable(pending / _FILESYSTEM_DELTAS_FILENAME, filesystem_payload)

    if ownership_transfer_payload is not None:
        _write_text_durable(
            pending / _OWNERSHIP_TRANSFERS_FILENAME, ownership_transfer_payload
        )

    _stage_state_snapshots(pending, state_snapshots)

    atomicio.fsync_dir(pending)
    pending.rename(target)
    atomicio.fsync_dir(root)

    write_meta(
        target, meta, paths=touched, tracked_file_destinations=tracked_file_destinations
    )
    atomicio.fsync_dir(target)

    return target


def rewrite_file_changes(
    transition_dir: TransitionDir,
    file_pre: Mapping[Path, FilesystemImage],
    file_post: Mapping[Path, FilesystemImage],
) -> None:
    """Replace a committed record's file changes with ``file_pre`` -> ``file_post``."""
    payload = _serialize_filesystem_deltas(
        _canonicalize_filesystem_deltas(_file_deltas(file_pre, file_post))
    )
    if payload is None:
        raise SetforgeError(f"no file changes to record in {transition_dir}")
    target = transition_dir / _FILESYSTEM_DELTAS_FILENAME
    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else None
    atomicio.atomic_write_text(target, payload, mode=mode)


def _serialize_ext_payload(ext_delta: ExtensionDelta | None) -> str | None:
    """Return the ``extensions.json`` body, or ``None`` if nothing to write."""
    if ext_delta is None or ext_delta.is_empty():
        return None
    return (
        json.dumps({"added": ext_delta.added, "removed": ext_delta.removed}, indent=2)
        + "\n"
    )


def _serialize_plugin_payload(plugin_delta: PluginDelta | None) -> str | None:
    """Return the ``plugins.json`` body, or ``None`` when there's nothing to write.

    ``marketplaces_removed`` is ``tuple[tuple[name, source_dict], ...]`` →
    serialized as ``[[name, source_dict], ...]`` so each entry round-trips
    through ``json.loads`` as a 2-element list (caller converts back to a
    tuple by position).

    Defensive contract enforcement: per :class:`PluginDelta`'s
    JSON-primitive contract, every source-dict value must be a ``str``.
    Raise loudly here so a caller that bypasses
    ``MarketplaceSource.model_dump(mode="json")`` and passes raw
    enum/Path values gets an actionable error instead of an opaque
    ``json.dumps`` failure mid-serialization. Today's install path
    hard-codes ``()`` so this guard is dormant; it fires the moment a
    future caller starts populating the field.
    """
    if plugin_delta is None or plugin_delta.is_empty():
        return None
    for name, src in plugin_delta.marketplaces_removed:
        for key, value in src.items():
            if not isinstance(value, str):
                raise TypeError(
                    f"marketplaces_removed source dict {name!r} has "
                    f"non-str value for key {key!r}: {value!r} "
                    f"({type(value).__name__}). Callers must serialize "
                    "via MarketplaceSource.model_dump(mode='json')."
                )
    payload: dict[str, object] = {
        "installed": list(plugin_delta.installed),
        "enabled": list(plugin_delta.enabled),
        "disabled": list(plugin_delta.disabled),
        "marketplaces_added": list(plugin_delta.marketplaces_added),
        "marketplaces_removed": [
            [name, dict(src)] for name, src in plugin_delta.marketplaces_removed
        ],
    }
    # Omitted when empty so a record with no uninstall is written exactly as
    # releases without the field wrote it.
    if plugin_delta.uninstalled:
        payload["uninstalled"] = [
            [plugin_id, enabled] for plugin_id, enabled in plugin_delta.uninstalled
        ]
    return json.dumps(payload, indent=2) + "\n"


def _serialize_codex_plugin_payload(
    delta: CodexPluginDelta | None,
) -> str | None:
    if delta is None or delta.is_empty():
        return None
    for name, source in delta.marketplaces_removed:
        if not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in source.items()
        ):
            raise TypeError(f"Codex marketplace source {name!r} is not JSON-safe")
    return (
        json.dumps(
            {
                "installed": list(delta.installed),
                "removed": list(delta.removed),
                "marketplaces_added": list(delta.marketplaces_added),
                "marketplaces_removed": [
                    [name, dict(source)] for name, source in delta.marketplaces_removed
                ],
            },
            indent=2,
        )
        + "\n"
    )


def _serialize_mcp_payload(mcp_delta: MCPDelta | None) -> str | None:
    """Return the ``mcp.json`` body, or ``None`` when there's nothing to write.

    Both ``added`` and ``updated`` are
    ``tuple[tuple[name, command, scope], ...]`` → each serialized as
    ``[[name, [cmd...], scope], ...]`` so it round-trips through
    ``json.loads`` as a 3-element list (the loader converts the command
    list back to a tuple by position). Empty deltas return ``None`` so no
    ``mcp.json`` is written (omit-when-empty).
    """
    if mcp_delta is None or mcp_delta.is_empty():
        return None

    def _triples(
        entries: tuple[tuple[str, tuple[str, ...], str], ...],
    ) -> list[list[object]]:
        return [[name, list(command), scope] for name, command, scope in entries]

    return (
        json.dumps(
            {
                "added": _triples(mcp_delta.added),
                "updated": _triples(mcp_delta.updated),
                **({"context": list(mcp_delta.context)} if mcp_delta.context else {}),
            },
            indent=2,
        )
        + "\n"
    )


def mcp_delta_from_json(raw: dict[str, object]) -> MCPDelta:
    """Reconstruct an :class:`MCPDelta` from a JSON-deserialized ``mcp.json``.

    Inverse of :func:`_serialize_mcp_payload`. Validates each ``added`` /
    ``updated`` entry is a ``[str, [str...], str]`` triple, raising
    :class:`InvalidTransitionRecord` on any deviation. Mirrors the
    boundary guard on :func:`plugin_delta_from_json`: without it a
    corrupted ``mcp.json`` (hand-edit, partial write) would surface as an
    opaque unpack error mid-revert instead of a clean
    :class:`SetforgeError` at the JSON boundary.
    """

    def _triples(key: str) -> tuple[tuple[str, tuple[str, ...], str], ...]:
        entries_raw = raw.get(key, [])
        if not isinstance(entries_raw, list):
            raise InvalidTransitionRecord(
                f"mcp.json: {key} must be a list, got {type(entries_raw).__name__}"
            )
        validated: list[tuple[str, tuple[str, ...], str]] = []
        for entry in entries_raw:
            if not (isinstance(entry, list) and len(entry) == 3):
                raise InvalidTransitionRecord(
                    f"mcp.json: malformed {key} entry: {entry!r}"
                )
            name, command, scope = entry
            if not isinstance(name, str) or not isinstance(scope, str):
                raise InvalidTransitionRecord(
                    f"mcp.json: {key} entry has wrong types: "
                    f"({type(name).__name__}, _, {type(scope).__name__})"
                )
            command_tokens = _validated_str_list(
                command, key=f"{key}.command", source_label="mcp.json"
            )
            validated.append((name, tuple(command_tokens), scope))
        return tuple(validated)

    from setforge.mcp_servers import parse_inventory_context

    try:
        context = parse_inventory_context(raw["context"]) if "context" in raw else None
    except ValueError as exc:
        raise InvalidTransitionRecord("mcp.json: invalid native context") from exc
    return MCPDelta(
        added=_triples("added"), updated=_triples("updated"), context=context
    )


def _load_delta_payload(
    transition_dir: Path, filename: str
) -> dict[str, object] | None:
    """Read one optional adapter-delta sidecar; ``None`` when it is absent."""
    path = transition_dir / filename
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InvalidTransitionRecord(
            f"cannot read {filename} at {path}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise InvalidTransitionRecord(f"{filename} at {path} is not a JSON object")
    return raw


def load_extension_delta(transition_dir: Path) -> ExtensionDelta | None:
    """Return the recorded extension delta, or ``None`` when none was recorded."""
    raw = _load_delta_payload(transition_dir, "extensions.json")
    return None if raw is None else extension_delta_from_json(raw)


def load_plugin_delta(transition_dir: Path) -> PluginDelta | None:
    """Return the recorded plugin delta, or ``None`` when none was recorded."""
    raw = _load_delta_payload(transition_dir, "plugins.json")
    return None if raw is None else plugin_delta_from_json(raw)


def load_codex_plugin_delta(transition_dir: Path) -> CodexPluginDelta | None:
    """Return the recorded Codex plugin delta, or ``None`` when none was recorded."""
    raw = _load_delta_payload(transition_dir, "codex_plugins.json")
    return None if raw is None else codex_plugin_delta_from_json(raw)


def load_mcp_delta(transition_dir: Path) -> MCPDelta | None:
    """Return the recorded MCP delta, or ``None`` when none was recorded."""
    raw = _load_delta_payload(transition_dir, "mcp.json")
    return None if raw is None else mcp_delta_from_json(raw)


@dataclass(frozen=True, slots=True)
class TransitionRecord:
    """Everything revert needs from one committed transition, read once."""

    directory: TransitionDir
    meta: TransitionMeta
    paths: tuple[Path, ...]
    tracked_file_destinations: Mapping[str, tuple[Path, ...]]
    filesystem_deltas: tuple[FilesystemDelta, ...]
    files_complete: bool
    ownership_transfers: tuple[OwnershipTransferDelta, ...]
    state_snapshots: tuple[StateSnapshotEntry, ...] | None
    extensions: ExtensionDelta | None
    plugins: PluginDelta | None
    codex_plugins: CodexPluginDelta | None
    mcp: MCPDelta | None


def load_record(transition_dir: TransitionDir) -> TransitionRecord:
    """Read and validate every file of one transition directory.

    Raises :class:`InvalidTransitionRecord` when any file is unreadable or
    malformed, so a corrupt record is refused before a caller acts on it.
    """
    meta_file = transition_dir / "meta.json"
    payload = load_meta_payload(transition_dir)
    raw_paths = payload.get("paths", [])
    if not isinstance(raw_paths, list) or not all(
        isinstance(path, str) for path in raw_paths
    ):
        raise InvalidTransitionRecord(
            f"meta.json at {meta_file} has a non-list 'paths' field"
        )
    attribution = payload.get("tracked_file_destinations", {})
    if not isinstance(attribution, dict) or any(
        not isinstance(name, str)
        or not isinstance(paths, list)
        or any(
            not isinstance(path, str) or not Path(path).is_absolute() for path in paths
        )
        for name, paths in attribution.items()
    ):
        raise InvalidTransitionRecord(
            f"invalid tracked_file_destinations in {meta_file}"
        )
    filesystem_deltas, files_complete = _load_filesystem_deltas(transition_dir)
    return TransitionRecord(
        directory=transition_dir,
        meta=_meta_from_payload(payload, meta_file),
        paths=tuple(Path(path) for path in raw_paths),
        tracked_file_destinations={
            name: tuple(Path(os.path.normpath(path)) for path in paths)
            for name, paths in attribution.items()
        },
        filesystem_deltas=filesystem_deltas,
        files_complete=files_complete,
        ownership_transfers=load_ownership_transfers(transition_dir),
        state_snapshots=load_state_snapshots(transition_dir),
        extensions=load_extension_delta(transition_dir),
        plugins=load_plugin_delta(transition_dir),
        codex_plugins=load_codex_plugin_delta(transition_dir),
        mcp=load_mcp_delta(transition_dir),
    )


def refuse_legacy_file_changes(record: TransitionRecord) -> None:
    """Refuse a record whose file changes an earlier version kept as a text patch.

    Such a record holds no pre-image for those files, so it cannot be reverted
    exactly; it is refused before anything is changed.
    """
    if record.files_complete:
        return
    directory = record.directory
    patch = directory / "changes.patch"
    modes = directory / "file_modes.json"
    try:
        mode_paths = list(json.loads(modes.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        mode_paths = []
    covered = {str(item.path) for item in record.filesystem_deltas}
    uncovered = [
        path
        for path in dict.fromkeys((*map(str, record.paths), *map(str, mode_paths)))
        if path not in covered
    ]
    if (
        not uncovered
        and not (patch.exists() and patch.stat().st_size)
        and not modes.exists()
    ):
        return
    shown = ", ".join(uncovered[:3]) + (
        f" and {len(uncovered) - 3} more" if len(uncovered) > 3 else ""
    )
    raise RevertFailed(
        f"transition {directory.name} was recorded by an earlier version in a "
        "format this version cannot revert; nothing was changed. Revert it with "
        f"setforge {record.meta.version}, which recorded it"
        + (f", or restore by hand: {shown}" if shown else "")
    )


def load_latest(
    profile: str, *, command: TransitionCommand | None = None
) -> TransitionDir | None:
    """Return the most recent transition directory for ``profile``,
    or ``None`` if no history exists.

    Walks every transition directory and reads its ``meta.json`` to
    compare ``profile`` exactly. The dirname encodes profile as a
    suffix for sortability, but a substring match would conflate
    e.g. ``headless`` with ``vm-headless`` — meta.json is the canonical
    identity. Sorts lexicographically by dirname; transition_dirname's
    UTC-ISO prefix makes that equivalent to chronological order.

    When ``command`` is provided, restricts the candidate set to
    transitions whose ``meta.json`` ``command`` field equals that
    enum's string value. ``None`` (the default) returns the latest
    transition of ANY command type — preserves backward compatibility
    for callers that want "the last thing that happened" (e.g. revert).
    Filtering callers (e.g. status's
    last-install line) pass ``command=TransitionCommand.INSTALL`` so a
    later sync/revert doesn't shadow the install they want to display.

    Best-effort sweeps ``.pending-*`` dirs older than
    :data:`_STALE_PENDING_AGE` (24 h) before scanning candidates. These
    are orphans from a crashed :func:`write_transition`. Fresh pending
    dirs (a write in progress) are left alone.
    """
    root = transitions_root()
    if not root.exists():
        return None

    _sweep_stale_pending(root)
    candidates = _filter_transition_entries(root, profile, command=command)
    latest = _pick_latest_transition(candidates)
    return TransitionDir(latest) if latest is not None else None


def _sweep_stale_pending(root: Path) -> None:
    """Best-effort: remove ``.pending-*`` dirs older than ``_STALE_PENDING_AGE``."""
    now = datetime.now(UTC).timestamp()
    for d in root.iterdir():
        if d.is_dir() and d.name.startswith(".pending-"):
            try:
                if now - d.stat().st_mtime > _STALE_PENDING_AGE.total_seconds():
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                continue


def committed_transition_dirs(root: Path, *, tolerant: bool = False) -> Iterator[Path]:
    """Yield committed records: real directories holding the ``meta.json`` marker.

    ``tolerant`` skips an entry that cannot be examined (a record directory the
    user may not enter) instead of raising.
    """
    for child in root.iterdir():
        try:
            committed = (
                child.is_dir()
                and not child.name.startswith(".pending-")
                and (child / "meta.json").exists()
            )
        except OSError:
            if not tolerant:
                raise
            continue
        if committed:
            yield child


def _filter_transition_entries(
    root: Path, profile: str, *, command: TransitionCommand | None = None
) -> list[Path]:
    """Return committed transition dirs whose ``meta.json`` matches ``profile``.

    When ``command`` is not None, further restricts to entries whose
    ``meta.json`` ``command`` field equals that enum's string value.
    Malformed ``command`` fields (missing, non-string, unknown value)
    are silently dropped under filtered mode — best-effort posture
    consistent with the broader transitions reader.
    """
    candidates: list[Path] = []
    for d in committed_transition_dirs(root):
        try:
            payload = load_meta_payload(d)
        except InvalidTransitionRecord:
            continue
        if payload.get("profile") != profile:
            continue
        if command is not None and payload.get("command") != command.value:
            continue
        candidates.append(d)
    return candidates


def _pick_latest_transition(candidates: list[Path]) -> Path | None:
    """Return the lex-max-by-name candidate (UTC-ISO prefix → chronological)."""
    if not candidates:
        return None
    return max(candidates, key=lambda d: d.name)


@dataclass(frozen=True, slots=True)
class TransitionListing:
    """One row of ``setforge transitions list``. Decoded from a transition
    directory's ``meta.json`` (canonical) plus optional ``extensions.json``
    and ``plugins.json`` siblings."""

    directory: TransitionDir
    timestamp: datetime
    command: str
    profile: str
    file_count: int
    ext_count: int
    plugin_count: int = 0
    codex_plugin_count: int = 0
    ownership_transfer_count: int = 0


def _delta_count(load: Callable[[], object | None], *fields: str) -> int:
    """Count the entries of one adapter delta; a corrupt sidecar counts as 0."""
    try:
        delta = load()
    except InvalidTransitionRecord:
        return 0
    if delta is None:
        return 0
    return sum(len(getattr(delta, field)) for field in fields)


def _load_listing(transition_dir: Path) -> TransitionListing | None:
    """Decode one transition directory into a :class:`TransitionListing`,
    or return ``None`` if its ``meta.json`` is missing or unreadable. Used
    by :func:`list_transitions` to skip half-written / corrupted dirs
    without aborting the whole listing."""
    try:
        payload = load_meta_payload(transition_dir)
        timestamp = datetime.fromisoformat(payload["timestamp"])
        command = str(payload["command"])
        profile = str(payload["profile"])
    except (InvalidTransitionRecord, KeyError, ValueError):
        return None

    paths = payload.get("paths", [])
    return TransitionListing(
        directory=TransitionDir(transition_dir),
        timestamp=timestamp,
        command=command,
        profile=profile,
        file_count=len(paths) if isinstance(paths, list) else 0,
        ext_count=_delta_count(
            lambda: load_extension_delta(transition_dir), "added", "removed"
        ),
        plugin_count=_delta_count(
            lambda: load_plugin_delta(transition_dir),
            "installed",
            "enabled",
            "disabled",
            "marketplaces_added",
            "marketplaces_removed",
            "uninstalled",
        ),
        codex_plugin_count=_delta_count(
            lambda: load_codex_plugin_delta(transition_dir),
            "installed",
            "removed",
            "marketplaces_added",
            "marketplaces_removed",
        ),
        ownership_transfer_count=len(
            load_ownership_transfers(TransitionDir(transition_dir))
        ),
    )


def list_transitions(
    profile_filter: list[str] | None = None,
    reverse: bool = False,
) -> list[TransitionListing]:
    """Return every transition record under :func:`transitions_root`.

    ``profile_filter`` is an OR-filter — non-empty list keeps only entries
    whose profile is in the list. ``None`` or empty list keeps all.

    Default order is chronological (oldest first), matching the
    ``transition_dirname`` lexicographic invariant. ``reverse=True`` flips
    that to newest-first.

    Half-written or corrupted transition dirs (missing/unreadable
    ``meta.json``) are silently skipped; the listing degrades gracefully
    rather than failing the whole command.
    """
    root = transitions_root()
    if not root.exists():
        return []
    keep = set(profile_filter) if profile_filter else None
    listings: list[TransitionListing] = []
    for child in committed_transition_dirs(root):
        listing = _load_listing(child)
        if listing is None:
            continue
        if keep is not None and listing.profile not in keep:
            continue
        listings.append(listing)
    listings.sort(key=lambda x: x.directory.name)
    if reverse:
        listings.reverse()
    return listings


def resolve_transition_prefix(prefix: str) -> TransitionDir:
    """Resolve a dirname prefix (or full dirname) to one transition directory.

    Resolution rules:
    1. Exact dirname match → return that directory.
    2. Otherwise collect every directory whose dirname starts with ``prefix``.
    3. Zero matches → raise :class:`SetforgeError`.
    4. One match → return it.
    5. Multiple matches → raise :class:`SetforgeError` listing every candidate
       sorted ascending so the user can disambiguate.

    Used by ``setforge transitions show <prefix>``. Read-only.
    """
    root = transitions_root()
    if not root.exists():
        raise SetforgeError(f"no transition matching prefix {prefix!r}")
    exact = root / prefix
    if exact.is_dir() and (exact / "meta.json").exists():
        return TransitionDir(exact)
    matches = sorted(
        child
        for child in committed_transition_dirs(root)
        if child.name.startswith(prefix)
    )
    if not matches:
        raise SetforgeError(f"no transition matching prefix {prefix!r}")
    if len(matches) > 1:
        joined = "\n  ".join(child.name for child in matches)
        raise SetforgeError(
            f"prefix {prefix!r} matches {len(matches)} transitions:\n  {joined}"
        )
    return TransitionDir(matches[0])
