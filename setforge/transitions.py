"""Transition records: per-invocation undo support for install/sync.

Each state-changing command (install, sync, revert) writes a directory
under ``~/.local/state/setforge/transitions/`` containing:

- ``meta.json`` — command, profile, UTC timestamp, host, setforge version
- ``changes.patch`` — unified diff of file changes (omitted if no edits)
- ``extensions.json`` — added/removed extension IDs (omitted if no delta)
- ``plugins.json`` — installed / enabled / disabled plugin IDs plus
  added / removed marketplaces (omitted if no plugin delta)
- ``state_snapshots/`` — pre-command per-host store state (byte bases,
  spans sidecars, scalar-base manifests) as a ``manifest.json`` plus
  numbered raw-byte payload files (omitted when nothing was captured)

A subsequent ``setforge revert`` consumes the most recent transition for
a profile, applies the patch in reverse via ``patch -R``, reverses the
extension delta, reverses the plugin delta, restores the snapshotted
store state, and records its own reverse transition.
"""

import difflib
import json
import os
import platform
import shutil
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final, NewType

from setforge import __version__, atomicio
from setforge.binaries import resolve_binary
from setforge.errors import InvalidTransitionRecord, RevertFailed, SetforgeError

TransitionDir = NewType("TransitionDir", Path)
"""A directory containing transition metadata (``meta.json``, ``changes.patch``, etc.).

Constructed only by ``setforge.transitions`` factory functions. Consumers
accepting a ``TransitionDir`` get type-check protection that raw ``Path``
values are rejected at static-analysis time.
"""


class TransitionCommand(StrEnum):
    """Closed set of state-changing commands that record transitions."""

    INSTALL = "install"
    SYNC = "sync"
    REVERT = "revert"
    MERGE = "merge"
    CLEANUP_ORPHANS = "cleanup-orphans"
    PROMOTE = "promote"
    MIGRATE = "migrate"


# The profile label recorded on a ``migrate`` transition. A schema migration
# is profile-agnostic (it mutates setforge.yaml / shared content, not a
# profile-specific deploy), so it is recorded under this fixed label rather
# than a real ``setforge.yaml`` profile name — and the revert side tolerates
# a label that does not resolve to a config profile. Lives here (not in the
# migrate command) so revert can reference it without importing the command.
MIGRATE_TRANSITION_PROFILE: Final[str] = "migrate"


_STATE_ENV = "SETFORGE_STATE_DIR"
_DEFAULT_STATE_ROOT_SUFFIX = (".local", "state", "setforge")
_STALE_PENDING_AGE = timedelta(hours=24)


def state_root() -> Path:
    """Resolve the setforge state dir.

    Honors the ``SETFORGE_STATE_DIR`` env var (used by tests and by
    operators relocating state). Falls back to ``~/.local/state/setforge``.
    """
    override = os.environ.get(_STATE_ENV)
    if override:
        return Path(override)
    return Path.home().joinpath(*_DEFAULT_STATE_ROOT_SUFFIX)


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

    The trailing three fields (``end_timestamp``, ``command_line``,
    ``preserve_user_keys_applied``) were added in a later schema bump so
    ``setforge transitions show`` can display per-invocation duration,
    the exact argv, and whether any preserve_user_keys overlay matched
    a live key during deploy. All three follow the same omit-when-None
    pattern as ``source_sha`` so old meta.json files (recorded before
    the bump) still round-trip byte-identically.
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
    # (NOT default_factory=list) so the omit-when-None invariant holds for every
    # field and slots=True doesn't allocate a per-instance default container.
    end_timestamp: str | None = None
    command_line: list[str] | None = None
    preserve_user_keys_applied: bool | None = None

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
        if self.preserve_user_keys_applied is not None:
            out["preserve_user_keys_applied"] = self.preserve_user_keys_applied
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
    end_timestamp: str | None = None,
    command_line: list[str] | None = None,
    preserve_user_keys_applied: bool | None = None,
) -> TransitionMeta:
    """Build a TransitionMeta with current host + version + UTC timestamp.

    When ``source_dir`` is provided AND is a git repo, records its HEAD
    commit sha as ``source_sha`` so ``setforge status`` can compute
    ``commits-since-last-install``. Otherwise leaves
    ``source_sha`` as ``None``; callers that don't have a source dir
    handy (revert, plugin reconcile sub-record) keep the pre-bump call
    shape.

    The three trailing kwargs (``end_timestamp``, ``command_line``,
    ``preserve_user_keys_applied``) are a later schema bump.
    All default to ``None`` so pre-bump callers compile unchanged;
    each field is omitted from ``meta.json`` when ``None`` so old
    records still round-trip byte-identically.
    """
    source_sha = _git_head(source_dir) if source_dir is not None else None
    return TransitionMeta(
        command=command,
        profile=profile,
        timestamp=now_utc(),
        host=platform.node(),
        version=__version__,
        source_sha=source_sha,
        end_timestamp=end_timestamp,
        command_line=command_line,
        preserve_user_keys_applied=preserve_user_keys_applied,
    )


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
    payload_path = transition_dir / "meta.json"
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InvalidTransitionRecord(
            f"cannot read meta.json at {payload_path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise InvalidTransitionRecord(
            f"meta.json at {payload_path} is not a JSON object"
        )
    try:
        return TransitionMeta(
            command=TransitionCommand(payload["command"]),
            profile=str(payload["profile"]),
            timestamp=datetime.fromisoformat(payload["timestamp"]),
            host=str(payload["host"]),
            version=str(payload["version"]),
            source_sha=payload.get("source_sha"),
            # optional, all None pre-bump. .get() (NOT
            # payload[<field>]) so dozens of existing transition records
            # written before the bump still load cleanly.
            end_timestamp=payload.get("end_timestamp"),
            command_line=payload.get("command_line"),
            preserve_user_keys_applied=payload.get("preserve_user_keys_applied"),
        )
    except (KeyError, ValueError) as exc:
        raise InvalidTransitionRecord(
            f"meta.json at {payload_path} is missing or malformed: {exc}"
        ) from exc


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
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())


def _write_bytes_durable(path: Path, data: bytes) -> None:
    """Binary sibling of :func:`_write_text_durable` (same fsync contract).

    Used for the staged ``state_snapshots/<n>.payload`` files, which carry
    verbatim store bytes and must not pass through a text encode/decode.
    """
    with open(path, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def write_meta(
    transition_dir: TransitionDir,
    meta: TransitionMeta,
    paths: list[Path] | None = None,
) -> None:
    """Serialize ``meta`` to ``<transition_dir>/meta.json``.

    If ``paths`` is provided, every absolute path is recorded in a
    ``paths`` field on the JSON payload so :func:`load_latest` can
    identify the touched files without re-parsing the diff and so
    ``revert`` can snapshot pre/post state directly. Creates
    ``transition_dir`` (with parents) if needed. The meta.json fd is
    fsynced (via :func:`_write_text_durable`) so the commit marker's data
    is power-loss durable.
    """
    transition_dir.mkdir(parents=True, exist_ok=True)
    body: dict[str, object] = dict(meta.to_dict())
    if paths is not None:
        body["paths"] = [str(p) for p in paths]
    payload = json.dumps(body, indent=2) + "\n"
    _write_text_durable(transition_dir / "meta.json", payload)


def snapshot_paths(paths: Iterable[Path]) -> dict[Path, str | None]:
    """Read every path in ``paths``. Missing files map to ``None``.

    Returns a dict so callers can pass it directly to :func:`compute_patch`.
    Reads as text/UTF-8; binary file deploys are out of scope for v1
    (the deploy primitive itself only handles text tracked_files today).
    """
    out: dict[Path, str | None] = {}
    for p in paths:
        try:
            out[p] = p.read_text(encoding="utf-8")
        except FileNotFoundError:
            out[p] = None
    return out


def _diff_path(path: Path) -> str:
    """Format a Path for a diff header.

    GNU patch's safe-paths feature rejects absolute paths as "potentially
    dangerous." Workaround: emit paths root-relative (no leading ``/``),
    and apply with ``patch -d /`` so the relative path resolves
    absolute. ``/dev/null`` is the standard sentinel for missing files
    and must NOT be stripped.
    """
    s = str(path)
    return s.lstrip("/") if s.startswith("/") else s


def compute_patch(
    pre: Mapping[Path, str | None],
    post: Mapping[Path, str | None],
) -> str:
    """Return one combined unified diff covering every path that
    differs between ``pre`` and ``post``.

    Missing files appear as ``/dev/null`` so ``patch`` can apply
    creations on forward (``+++ a/b``) and deletions on reverse
    (``--- a/b`` paired with ``+++ /dev/null``). Real paths are emitted
    root-relative (leading ``/`` stripped) so :func:`apply_patch_reverse`
    can invoke ``patch -d /`` and bypass GNU patch's safe-paths check.
    """
    chunks: list[str] = []
    for path in sorted(set(pre) | set(post), key=str):
        before = pre.get(path)
        after = post.get(path)
        if before == after:
            continue
        before_lines = (before or "").splitlines(keepends=True)
        after_lines = (after or "").splitlines(keepends=True)
        from_path = "/dev/null" if before is None else _diff_path(path)
        to_path = "/dev/null" if after is None else _diff_path(path)
        chunks.append(
            "".join(
                difflib.unified_diff(
                    before_lines,
                    after_lines,
                    fromfile=from_path,
                    tofile=to_path,
                )
            )
        )
    return "".join(chunks)


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

    Failed plugin operations are excluded so revert never tries to
    reverse a no-op, mirroring :class:`ExtensionDelta`'s contract.
    """

    installed: tuple[str, ...]
    enabled: tuple[str, ...]
    disabled: tuple[str, ...]
    marketplaces_added: tuple[str, ...]
    marketplaces_removed: tuple[tuple[str, dict[str, str]], ...]

    def is_empty(self) -> bool:
        return not (
            self.installed
            or self.enabled
            or self.disabled
            or self.marketplaces_added
            or self.marketplaces_removed
        )


class ReconcileKind(StrEnum):
    """Closed set of item kinds a :class:`ReconcileOutcome` can record.

    StrEnum (not bare ``Literal[...]``) per CLAUDE.md's
    "StrEnum / IntEnum for closed sets — never bare module-level magic
    strings" rule. Members compare equal to their string values, so the
    on-disk ``reconcile_outcomes.json`` shape — and existing tests that
    assert ``outcome.kind == "plugin"`` — keep working unchanged.
    """

    PLUGIN = "plugin"
    EXTENSION = "extension"


class ReconcileStatus(StrEnum):
    """Closed set of per-item outcome statuses on a reconcile pass.

    StrEnum (not bare ``Literal[...]``) for the same reason as
    :class:`ReconcileKind`. ``OK`` covers first-attempt successes;
    ``RETRIED_OK`` second-attempt successes after the user picked
    RETRY at the failure prompt; ``SKIPPED`` items the user opted to
    leave behind; ``ABORTED`` items that landed before the user picked
    ABORT and got rolled back as part of the abort path's reverse
    reconcile.
    """

    OK = "ok"
    RETRIED_OK = "retried_ok"
    SKIPPED = "skipped"
    ABORTED = "aborted"


@dataclass(slots=True, frozen=True)
class ReconcileOutcome:
    """One per-item outcome from a plugin or extension reconcile pass.

    Serialized alongside ``ExtensionDelta`` / ``PluginDelta`` into the
    transition record's ``reconcile_outcomes.json`` sibling, so the
    ``install --retry-failed`` flag can rebuild the set of skipped
    items on the next invocation and a future ``revert`` step can see
    which items landed only partially.

    Backward compatibility: old transition records written before
    the reconcile-outcomes schema bump have no ``reconcile_outcomes.json`` file;
    :func:`load_reconcile_outcomes` returns ``()`` in that case.
    Within ``reconcile_outcomes.json``, ``kind`` and ``status``
    continue to serialize as their string values (``"plugin"`` /
    ``"ok"`` / ...) because :class:`StrEnum` members ARE strings;
    deserialization wraps each raw string in the enum constructor
    inside :func:`_validate_one_outcome`.
    """

    item_id: str
    kind: ReconcileKind
    status: ReconcileStatus
    error_summary: str | None


def _serialize_reconcile_outcomes(
    outcomes: tuple[ReconcileOutcome, ...],
) -> str | None:
    """Return the ``reconcile_outcomes.json`` body, or ``None`` when empty.

    Emits ``kind`` and ``status`` as their underlying string values via
    explicit ``.value`` access so the on-disk shape is stable
    regardless of ``json.dumps``'s implementation-defined behavior on
    :class:`StrEnum` instances.
    """
    if not outcomes:
        return None
    return (
        json.dumps(
            {
                "outcomes": [
                    {
                        "item_id": o.item_id,
                        "kind": o.kind.value,
                        "status": o.status.value,
                        "error_summary": o.error_summary,
                    }
                    for o in outcomes
                ]
            },
            indent=2,
        )
        + "\n"
    )


_VALID_OUTCOME_KINDS: frozenset[str] = frozenset(k.value for k in ReconcileKind)
_VALID_OUTCOME_STATUSES: frozenset[str] = frozenset(s.value for s in ReconcileStatus)


def _validate_one_outcome(entry: object) -> ReconcileOutcome:
    """Validate one JSON entry into a :class:`ReconcileOutcome`.

    Raises :class:`InvalidTransitionRecord` on any deviation from the
    four-field shape. Kept as a free function so
    :func:`reconcile_outcomes_from_json`'s per-entry block flattens to
    one ``append(_validate_one_outcome(entry))`` call (nesting depth 2,
    not 3). Wraps the raw string ``kind`` / ``status`` payload in the
    :class:`ReconcileKind` / :class:`ReconcileStatus` enum constructors
    after the membership-check guard fires; the explicit guard keeps
    the error message stable and lets us raise
    :class:`InvalidTransitionRecord` rather than the bare
    :class:`ValueError` that would surface from a direct enum
    constructor on a bogus payload.
    """
    if not isinstance(entry, dict):
        raise InvalidTransitionRecord(
            f"reconcile_outcomes.json: entry must be a dict, got {type(entry).__name__}"
        )
    item_id = entry.get("item_id")
    kind = entry.get("kind")
    status = entry.get("status")
    err = entry.get("error_summary")
    if not isinstance(item_id, str):
        raise InvalidTransitionRecord(
            f"reconcile_outcomes.json: item_id must be str, got "
            f"{type(item_id).__name__}"
        )
    if kind not in _VALID_OUTCOME_KINDS:
        raise InvalidTransitionRecord(
            f"reconcile_outcomes.json: kind must be in "
            f"{sorted(_VALID_OUTCOME_KINDS)}, got {kind!r}"
        )
    if status not in _VALID_OUTCOME_STATUSES:
        raise InvalidTransitionRecord(
            f"reconcile_outcomes.json: status must be in "
            f"{sorted(_VALID_OUTCOME_STATUSES)}, got {status!r}"
        )
    if err is not None and not isinstance(err, str):
        raise InvalidTransitionRecord(
            f"reconcile_outcomes.json: error_summary must be str | None, "
            f"got {type(err).__name__}"
        )
    return ReconcileOutcome(
        item_id=item_id,
        kind=ReconcileKind(kind),
        status=ReconcileStatus(status),
        error_summary=err,
    )


def reconcile_outcomes_from_json(
    raw: dict[str, object],
) -> tuple[ReconcileOutcome, ...]:
    """Reconstruct ``tuple[ReconcileOutcome, ...]`` from a JSON payload.

    Validates each entry against the four-field shape; raises
    :class:`InvalidTransitionRecord` on any deviation. The empty
    ``{"outcomes": []}`` payload returns ``()`` so the boundary is
    backward-compat-safe with old transition records (no file → empty
    tuple at the loader; valid-but-empty payload → same shape).
    """
    raw_list = raw.get("outcomes", [])
    if not isinstance(raw_list, list):
        raise InvalidTransitionRecord(
            f"reconcile_outcomes.json: outcomes must be a list, got "
            f"{type(raw_list).__name__}"
        )
    return tuple(_validate_one_outcome(entry) for entry in raw_list)


def load_reconcile_outcomes(
    transition_dir: TransitionDir,
) -> tuple[ReconcileOutcome, ...]:
    """Return the reconcile-outcome tuple for a transition directory.

    Returns ``()`` when the ``reconcile_outcomes.json`` file is absent —
    the backward-compat path for transitions written before that schema bump.
    Raises :class:`InvalidTransitionRecord` when the file exists but its
    shape is corrupt (delegated to :func:`reconcile_outcomes_from_json`).
    """
    path = transition_dir / "reconcile_outcomes.json"
    if not path.exists():
        return ()
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise InvalidTransitionRecord(
            f"reconcile_outcomes.json: top-level must be a dict, got "
            f"{type(raw).__name__}"
        )
    return reconcile_outcomes_from_json(raw)


class SnapshotStore(StrEnum):
    """Closed set of per-host state stores a transition can snapshot.

    The string values double as the store's directory name under
    :func:`state_root`, so the on-disk ``state_snapshots/manifest.json``
    records exactly the subtree each entry restores into.
    """

    BASE = "base"
    SPANS = "spans"
    SCALAR_BASE = "scalar-base"


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


def _snapshot_target(store: SnapshotStore, profile: str, key: str) -> Path:
    """Resolve one snapshot entry to its on-disk store path.

    Delegates to each store module's public path accessor so the
    traversal guard (relative key, no ``..``, stays inside the profile
    subtree) and the per-store suffix convention (``.json`` for the two
    manifest stores) live in exactly one place. The store modules import
    :func:`state_root` from here, so the imports are deferred to call
    time to keep the module graph acyclic.
    """
    from setforge import base_store, scalar_base_store, spans_store

    match store:
        case SnapshotStore.BASE:
            return base_store.base_path(profile, key)
        case SnapshotStore.SPANS:
            return spans_store.manifest_path(profile, key)
        case SnapshotStore.SCALAR_BASE:
            return scalar_base_store.manifest_path(profile, key)


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
    except (OSError, json.JSONDecodeError) as exc:
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
    )


def extension_delta_from_json(raw: dict[str, object]) -> ExtensionDelta:
    """Reconstruct an :class:`ExtensionDelta` from a JSON-deserialized
    ``extensions.json`` record. Inverse of the on-disk shape produced
    by :func:`write_transition`.

    Validates ``added`` and ``removed`` are lists of strings via
    :func:`_validated_str_list`, raising :class:`InvalidTransitionRecord`
    on any deviation. Mirrors the boundary guard added to
    :func:`plugin_delta_from_json` in bead dtm. Without this guard a
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


def _touched_paths(
    pre: Mapping[Path, str | None], post: Mapping[Path, str | None]
) -> list[Path]:
    """Return the sorted set of paths whose content differs between pre
    and post snapshots. Used to populate ``meta.json``'s ``paths`` field
    so ``revert`` doesn't need to parse diff headers to know what was
    touched."""
    return sorted(
        (p for p in (set(pre) | set(post)) if pre.get(p) != post.get(p)),
        key=str,
    )


def write_transition(
    meta: TransitionMeta,
    file_pre: Mapping[Path, str | None],
    file_post: Mapping[Path, str | None],
    ext_delta: ExtensionDelta | None,
    plugin_delta: PluginDelta | None = None,
    reconcile_outcomes: tuple[ReconcileOutcome, ...] = (),
    state_snapshots: tuple[StateSnapshotEntry, ...] = (),
) -> TransitionDir:
    """Write a complete transition directory under :func:`transitions_root`.

    Uses a two-phase write with atomic ``os.rename`` as the commit marker so
    a crash mid-write never leaves a half-formed transition visible to
    :func:`load_latest`. The sequence is power-loss durable: every staged
    payload file fsyncs its own fd before the rename, three distinct
    directory fsyncs (pending before rename, root after rename, target
    after meta.json) make the dir entries durable, and meta.json — the
    commit marker — is written and fsynced STRICTLY LAST, with nothing
    payload-related written after it.

    Write order: stage ``changes.patch`` (if non-empty), ``extensions.json``
    (if delta non-empty), ``plugins.json`` (if delta non-empty),
    ``reconcile_outcomes.json`` (if non-empty), and ``state_snapshots/``
    (if non-empty) into a ``.pending-<dirname>/``
    staging dir; ``os.rename(pending, target)`` — atomic POSIX rename,
    same fs; write ``meta.json`` inside the now-real ``target/`` dir as
    the commit point. A crash before that final ``meta.json`` write
    leaves either a ``.pending-<dirname>/`` (skipped by
    :func:`load_latest` via the ``.pending-`` name guard) or a
    ``<dirname>/`` without ``meta.json`` (skipped by the existing
    meta.json filter).

    ``reconcile_outcomes`` and ``state_snapshots`` default to empty
    tuples so the legacy call shapes stay backward-compatible; an empty
    ``state_snapshots`` writes no ``state_snapshots/`` dir at all, which
    :func:`load_state_snapshots` reads back as its ``None`` sentinel.

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
    patch = compute_patch(file_pre, file_post)
    if patch:
        _write_text_durable(pending / "changes.patch", patch)

    ext_payload = _serialize_ext_payload(ext_delta)
    if ext_payload is not None:
        _write_text_durable(pending / "extensions.json", ext_payload)

    plugin_payload = _serialize_plugin_payload(plugin_delta)
    if plugin_payload is not None:
        _write_text_durable(pending / "plugins.json", plugin_payload)

    outcomes_payload = _serialize_reconcile_outcomes(reconcile_outcomes)
    if outcomes_payload is not None:
        _write_text_durable(pending / "reconcile_outcomes.json", outcomes_payload)

    _stage_state_snapshots(pending, state_snapshots)

    atomicio.fsync_dir(pending)
    os.rename(pending, target)
    atomicio.fsync_dir(root)

    touched = _touched_paths(file_pre, file_post)
    write_meta(target, meta, paths=touched)
    atomicio.fsync_dir(target)

    return target


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
    return (
        json.dumps(
            {
                "installed": list(plugin_delta.installed),
                "enabled": list(plugin_delta.enabled),
                "disabled": list(plugin_delta.disabled),
                "marketplaces_added": list(plugin_delta.marketplaces_added),
                "marketplaces_removed": [
                    [name, dict(src)] for name, src in plugin_delta.marketplaces_removed
                ],
            },
            indent=2,
        )
        + "\n"
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
    for callers that want "the last thing that happened" (e.g. revert,
    install --retry-failed). Filtering callers (e.g. status's
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
    for d in root.iterdir():
        if not d.is_dir() or d.name.startswith(".pending-"):
            continue
        meta_file = d / "meta.json"
        if not meta_file.exists():
            continue
        try:
            payload = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
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


def apply_patch_reverse(
    transition_dir: TransitionDir, *, dry_run: bool = False
) -> None:
    """Apply ``<transition_dir>/changes.patch`` in reverse via ``patch -R``.

    No-op if the patch file is absent (e.g. transition recorded only an
    extension delta).

    When ``dry_run=False`` (default, backward-compat): a ``--dry-run`` pass
    runs first so drift on any single file aborts before any file is
    written; on a clean dry-run, the real apply follows.

    When ``dry_run=True``: only the dry-run pass runs; raises
    :class:`RevertFailed` on failure; returns ``None`` on success without
    modifying the live tree. Used as a building block for multi-step
    revert chains.

    The function is single-transition; multi-step coordination (including
    the partial-state failure model) belongs to the caller (see
    :func:`_revert_to_before` in :mod:`setforge.cli.revert`).

    ``--reject-file=-`` discards rejected hunks (would otherwise leave
    ``.rej`` siblings in the user's tree).

    Raises :class:`RevertFailed` if the ``patch`` binary is missing or
    if either pass fails. The patch's stderr is surfaced verbatim so
    the user sees the conflicting paths.
    """
    patch_file = transition_dir / "changes.patch"
    if not patch_file.exists():
        return
    patch_bin = resolve_binary("patch")
    if patch_bin is None:
        raise RevertFailed(
            "`patch` binary not on PATH; revert cannot apply file diffs. "
            "Tip: set 'binaries.patch' in ~/.config/setforge/local.yaml "
            "to override."
        )
    # Run with cwd=/ and -p0 so root-relative paths in the diff
    # (per :func:`_diff_path`) resolve to absolute targets.
    base_args = [
        str(patch_bin),
        "-p0",
        "-R",
        "-d",
        "/",
        "--reject-file=-",
        "--input",
        str(patch_file.resolve()),
    ]
    dry = subprocess.run(
        [*base_args, "--dry-run"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if dry.returncode != 0:
        raise RevertFailed(
            f"patch -R dry-run failed (exit {dry.returncode}); no files changed:\n"
            f"{dry.stderr.strip() or dry.stdout.strip()}"
        )
    if dry_run:
        return
    result = subprocess.run(
        base_args,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        # Should not happen after a clean dry-run; surface for forensics.
        raise RevertFailed(
            f"patch -R failed unexpectedly after dry-run succeeded "
            f"(exit {result.returncode}):\n"
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


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


def _load_listing(transition_dir: Path) -> TransitionListing | None:
    """Decode one transition directory into a :class:`TransitionListing`,
    or return ``None`` if its ``meta.json`` is missing or unreadable. Used
    by :func:`list_transitions` to skip half-written / corrupted dirs
    without aborting the whole listing."""
    meta_file = transition_dir / "meta.json"
    if not meta_file.exists():
        return None
    try:
        payload = json.loads(meta_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        timestamp = datetime.fromisoformat(payload["timestamp"])
        command = str(payload["command"])
        profile = str(payload["profile"])
    except (KeyError, ValueError):
        return None

    paths = payload.get("paths", [])
    file_count = len(paths) if isinstance(paths, list) else 0

    ext_file = transition_dir / "extensions.json"
    ext_count = 0
    if ext_file.exists():
        try:
            ext_payload = json.loads(ext_file.read_text(encoding="utf-8"))
            added = ext_payload.get("added", [])
            removed = ext_payload.get("removed", [])
            ext_count = (len(added) if isinstance(added, list) else 0) + (
                len(removed) if isinstance(removed, list) else 0
            )
        except (OSError, json.JSONDecodeError):
            ext_count = 0

    plugin_file = transition_dir / "plugins.json"
    plugin_count = 0
    if plugin_file.exists():
        try:
            plugin_payload = json.loads(plugin_file.read_text(encoding="utf-8"))
            for key in (
                "installed",
                "enabled",
                "disabled",
                "marketplaces_added",
                "marketplaces_removed",
            ):
                value = plugin_payload.get(key, [])
                if isinstance(value, list):
                    plugin_count += len(value)
        except (OSError, json.JSONDecodeError):
            plugin_count = 0

    return TransitionListing(
        directory=TransitionDir(transition_dir),
        timestamp=timestamp,
        command=command,
        profile=profile,
        file_count=file_count,
        ext_count=ext_count,
        plugin_count=plugin_count,
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
    for child in root.iterdir():
        if child.name.startswith(".pending-"):
            continue
        if not child.is_dir():
            continue
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
        for child in root.iterdir()
        if child.is_dir()
        and not child.name.startswith(".pending-")
        and child.name.startswith(prefix)
        and (child / "meta.json").exists()
    )
    if not matches:
        raise SetforgeError(f"no transition matching prefix {prefix!r}")
    if len(matches) > 1:
        joined = "\n  ".join(child.name for child in matches)
        raise SetforgeError(
            f"prefix {prefix!r} matches {len(matches)} transitions:\n  {joined}"
        )
    return TransitionDir(matches[0])


def summarize_transition(transition_dir: TransitionDir) -> dict[str, str]:
    """Map every absolute path touched by ``transition_dir`` to one of
    ``"created"``, ``"deleted"``, ``"modified"``.

    Derived from the ``--- old`` / ``+++ new`` headers of every hunk in
    ``changes.patch``. Returns an empty dict when the patch file is absent
    (e.g. transitions that recorded only an extension delta). Pairs of
    ``/dev/null`` indicate creation (forward) or deletion (forward); both
    real paths indicate modification.

    Path round-trip: :func:`_diff_path` strips the leading ``/`` to satisfy
    GNU patch's safe-paths rule, so reversing means prepending ``/``.
    """
    patch_file = transition_dir / "changes.patch"
    if not patch_file.exists():
        return {}
    text = patch_file.read_text(encoding="utf-8")
    lines = text.splitlines()
    out: dict[str, str] = {}
    i = 0
    while i < len(lines) - 1:
        if not lines[i].startswith("--- "):
            i += 1
            continue
        if not lines[i + 1].startswith("+++ "):
            i += 1
            continue
        from_path = lines[i][4:].split("\t", 1)[0]
        to_path = lines[i + 1][4:].split("\t", 1)[0]
        if from_path == "/dev/null" and to_path != "/dev/null":
            out["/" + to_path] = "created"
        elif to_path == "/dev/null" and from_path != "/dev/null":
            out["/" + from_path] = "deleted"
        elif from_path != "/dev/null":
            out["/" + from_path] = "modified"
        i += 2
    return out
