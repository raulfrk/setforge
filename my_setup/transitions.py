"""Transition records: per-invocation undo support for install/sync.

Each state-changing command (install, sync, revert) writes a directory
under ``~/.local/state/my-setup/transitions/`` containing:

- ``meta.json`` — command, profile, UTC timestamp, host, my-setup version
- ``changes.patch`` — unified diff of file changes (omitted if no edits)
- ``extensions.json`` — added/removed extension IDs (omitted if no delta)
- ``plugins.json`` — installed / enabled / disabled plugin IDs plus
  added / removed marketplaces (omitted if no plugin delta)

A subsequent ``my-setup revert`` consumes the most recent transition for
a profile, applies the patch in reverse via ``patch -R``, reverses the
extension delta, reverses the plugin delta, and records its own reverse
transition.
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
from typing import cast

from my_setup import __version__
from my_setup.binaries import resolve_binary
from my_setup.errors import InvalidTransitionRecord, MySetupError, RevertFailed


class TransitionCommand(StrEnum):
    """Closed set of state-changing commands that record transitions."""

    INSTALL = "install"
    SYNC = "sync"
    REVERT = "revert"
    MERGE = "merge"


_STATE_ENV = "MY_SETUP_STATE_DIR"
_DEFAULT_STATE_ROOT_SUFFIX = (".local", "state", "my-setup")
_STALE_PENDING_AGE = timedelta(hours=24)


def state_root() -> Path:
    """Resolve the my-setup state dir.

    Honors the ``MY_SETUP_STATE_DIR`` env var (used by tests and by
    operators relocating state). Falls back to ``~/.local/state/my-setup``.
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
        probe = root / ".my-setup-write-probe"
        probe.touch()
        probe.unlink()
    except OSError as exc:
        raise MySetupError(
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
    """Metadata for one transition. Serialized to ``meta.json``."""

    command: TransitionCommand
    profile: str
    timestamp: datetime  # UTC; serialized as ISO 8601
    host: str  # platform.node()
    version: str  # my_setup.__version__

    def to_dict(self) -> dict[str, str]:
        return {
            "command": self.command.value,
            "profile": self.profile,
            "timestamp": self.timestamp.astimezone(UTC).isoformat(),
            "host": self.host,
            "version": self.version,
        }


def make_meta(command: TransitionCommand, profile: str) -> TransitionMeta:
    """Build a TransitionMeta with current host + version + UTC timestamp."""
    return TransitionMeta(
        command=command,
        profile=profile,
        timestamp=now_utc(),
        host=platform.node(),
        version=__version__,
    )


def write_meta(
    transition_dir: Path,
    meta: TransitionMeta,
    paths: list[Path] | None = None,
) -> None:
    """Serialize ``meta`` to ``<transition_dir>/meta.json``.

    If ``paths`` is provided, every absolute path is recorded in a
    ``paths`` field on the JSON payload so :func:`load_latest` can
    identify the touched files without re-parsing the diff and so
    ``revert`` can snapshot pre/post state directly. Creates
    ``transition_dir`` (with parents) if needed.
    """
    transition_dir.mkdir(parents=True, exist_ok=True)
    body: dict[str, object] = dict(meta.to_dict())
    if paths is not None:
        body["paths"] = [str(p) for p in paths]
    payload = json.dumps(body, indent=2) + "\n"
    (transition_dir / "meta.json").write_text(payload, encoding="utf-8")


def snapshot_paths(paths: Iterable[Path]) -> dict[Path, str | None]:
    """Read every path in ``paths``. Missing files map to ``None``.

    Returns a dict so callers can pass it directly to :func:`compute_patch`.
    Reads as text/UTF-8; binary file deploys are out of scope for v1
    (the deploy primitive itself only handles text dotfiles today).
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
      live :class:`my_setup.config.MarketplaceSource` MUST serialize
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
    ``MySetupError`` boundary before any inverse op runs.

    Field-level :func:`typing.cast` on the other fields: ``raw[k]`` is
    ``object`` because :func:`json.loads` is untyped at the leaf and
    the caller (revert) treats the loaded record as a free-form
    mapping. The dataclass itself constrains shapes at write time via
    :func:`write_transition`'s string-value guard, so reads here
    trust the file's structure for the simple list fields — the cast
    makes that trust assertion explicit instead of suppressing.
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

    return PluginDelta(
        installed=tuple(cast(list[str], raw.get("installed", []))),
        enabled=tuple(cast(list[str], raw.get("enabled", []))),
        disabled=tuple(cast(list[str], raw.get("disabled", []))),
        marketplaces_added=tuple(cast(list[str], raw.get("marketplaces_added", []))),
        marketplaces_removed=tuple(validated_pairs),
    )


def extension_delta_from_json(raw: dict[str, object]) -> ExtensionDelta:
    """Reconstruct an :class:`ExtensionDelta` from a JSON-deserialized
    ``extensions.json`` record. Inverse of the on-disk shape produced
    by :func:`write_transition`.

    Validates ``added`` and ``removed`` are lists of strings, raising
    :class:`InvalidTransitionRecord` on any deviation. Mirrors the
    boundary guard added to :func:`plugin_delta_from_json` in bead dtm.
    Without this guard a corrupted extensions.json (hand-edit, partial
    write, or a bug in a future writer) would surface as an opaque
    :class:`TypeError` from a downstream ``iter()`` call rather than a
    clean :class:`MySetupError` at the JSON boundary.
    """
    added_raw = raw.get("added", [])
    if not isinstance(added_raw, list):
        raise InvalidTransitionRecord(
            f"extensions.json: added must be a list, got {type(added_raw).__name__}"
        )
    validated_added: list[str] = []
    for entry in added_raw:
        if not isinstance(entry, str):
            raise InvalidTransitionRecord(
                f"extensions.json: added entry has wrong type: {type(entry).__name__}"
            )
        validated_added.append(entry)

    removed_raw = raw.get("removed", [])
    if not isinstance(removed_raw, list):
        raise InvalidTransitionRecord(
            f"extensions.json: removed must be a list, got {type(removed_raw).__name__}"
        )
    validated_removed: list[str] = []
    for entry in removed_raw:
        if not isinstance(entry, str):
            raise InvalidTransitionRecord(
                f"extensions.json: removed entry has wrong type: {type(entry).__name__}"
            )
        validated_removed.append(entry)

    return ExtensionDelta(
        added=validated_added,
        removed=validated_removed,
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
) -> Path:
    """Write a complete transition directory under :func:`transitions_root`.

    Uses a two-phase write with atomic ``os.rename`` as the commit marker so
    a crash mid-write never leaves a half-formed transition visible to
    :func:`load_latest`.

    Write order:
    1. Create ``.pending-<dirname>/`` staging dir.
    2. Write ``changes.patch`` into staging (if non-empty).
    3. Write ``extensions.json`` into staging (if non-empty).
    4. Write ``plugins.json`` into staging (if non-empty).
    5. ``os.rename(pending, target)`` — atomic POSIX rename, same fs.
    6. Write ``meta.json`` inside the now-real ``target/`` dir. ← commit point.

    A crash before step 6 leaves either a ``.pending-<dirname>/`` (skipped by
    :func:`load_latest` via the ``.pending-`` name guard) or a ``<dirname>/``
    with no ``meta.json`` (skipped by the existing meta.json filter).

    Layout of the committed directory:
    - ``meta.json`` — always present, includes a ``paths`` field listing
      every absolute path the transition touched.
    - ``changes.patch`` — present iff :func:`compute_patch` returned non-empty.
    - ``extensions.json`` — present iff ``ext_delta`` is non-None and
      non-empty.
    - ``plugins.json`` — present iff ``plugin_delta`` is non-None and
      non-empty.

    Returns the absolute path of the committed directory.
    """
    root = transitions_root()
    dirname = transition_dirname(meta.timestamp, meta.command.value, meta.profile)
    target = root / dirname
    pending = root / f".pending-{dirname}"

    root.mkdir(parents=True, exist_ok=True)
    pending.mkdir(parents=True, exist_ok=False)

    patch = compute_patch(file_pre, file_post)
    if patch:
        (pending / "changes.patch").write_text(patch, encoding="utf-8")

    if ext_delta is not None and not ext_delta.is_empty():
        payload = (
            json.dumps(
                {"added": ext_delta.added, "removed": ext_delta.removed}, indent=2
            )
            + "\n"
        )
        (pending / "extensions.json").write_text(payload, encoding="utf-8")

    if plugin_delta is not None and not plugin_delta.is_empty():
        # ``marketplaces_removed`` is ``tuple[tuple[name, source_dict], ...]``
        # → serialized as ``[[name, source_dict], ...]`` so each entry
        # round-trips through ``json.loads`` as a 2-element list (caller
        # converts back to a tuple by position).
        #
        # Defensive contract enforcement: per :class:`PluginDelta`'s
        # JSON-primitive contract, every source-dict value must be a
        # ``str``. Raise loudly here so a caller that bypasses
        # ``MarketplaceSource.model_dump(mode="json")`` and passes raw
        # enum/Path values gets an actionable error instead of an
        # opaque ``json.dumps`` failure mid-serialization. Today's
        # install path hard-codes ``()`` so this guard is dormant; it
        # fires the moment a future caller starts populating the field.
        for name, src in plugin_delta.marketplaces_removed:
            for key, value in src.items():
                if not isinstance(value, str):
                    raise TypeError(
                        f"marketplaces_removed source dict {name!r} has "
                        f"non-str value for key {key!r}: {value!r} "
                        f"({type(value).__name__}). Callers must serialize "
                        "via MarketplaceSource.model_dump(mode='json')."
                    )
        plugin_payload = (
            json.dumps(
                {
                    "installed": list(plugin_delta.installed),
                    "enabled": list(plugin_delta.enabled),
                    "disabled": list(plugin_delta.disabled),
                    "marketplaces_added": list(plugin_delta.marketplaces_added),
                    "marketplaces_removed": [
                        [name, dict(src)]
                        for name, src in plugin_delta.marketplaces_removed
                    ],
                },
                indent=2,
            )
            + "\n"
        )
        (pending / "plugins.json").write_text(plugin_payload, encoding="utf-8")

    os.rename(pending, target)

    touched = _touched_paths(file_pre, file_post)
    write_meta(target, meta, paths=touched)

    return target


def load_latest(profile: str) -> Path | None:
    """Return the most recent transition directory for ``profile``,
    or ``None`` if no history exists.

    Walks every transition directory and reads its ``meta.json`` to
    compare ``profile`` exactly. The dirname encodes profile as a
    suffix for sortability, but a substring match would conflate
    e.g. ``headless`` with ``vm-headless`` — meta.json is the canonical
    identity. Sorts lexicographically by dirname; transition_dirname's
    UTC-ISO prefix makes that equivalent to chronological order.

    Best-effort sweeps ``.pending-*`` dirs older than
    :data:`_STALE_PENDING_AGE` (24 h) before scanning candidates. These
    are orphans from a crashed :func:`write_transition`. Fresh pending
    dirs (a write in progress) are left alone.
    """
    root = transitions_root()
    if not root.exists():
        return None

    now = datetime.now(UTC).timestamp()
    for d in root.iterdir():
        if d.is_dir() and d.name.startswith(".pending-"):
            try:
                if now - d.stat().st_mtime > _STALE_PENDING_AGE.total_seconds():
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                continue

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
        if payload.get("profile") == profile:
            candidates.append(d)
    if not candidates:
        return None
    return max(candidates, key=lambda d: d.name)


def apply_patch_reverse(transition_dir: Path) -> None:
    """Apply ``<transition_dir>/changes.patch`` in reverse via ``patch -R``.

    No-op if the patch file is absent (e.g. transition recorded only an
    extension delta).

    Atomicity: a ``--dry-run`` pass runs first so drift on any single
    file aborts before any file is written. ``--reject-file=-`` discards
    rejected hunks (would otherwise leave ``.rej`` siblings in the
    user's tree). On a clean dry-run, the real apply follows.

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
            "Tip: set 'binaries.patch' in ~/.config/my-setup/local.yaml "
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
    """One row of ``my-setup transitions list``. Decoded from a transition
    directory's ``meta.json`` (canonical) plus optional ``extensions.json``
    and ``plugins.json`` siblings. Read-only — does not represent any
    in-flight state."""

    directory: Path
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
        directory=transition_dir,
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


def resolve_transition_prefix(prefix: str) -> Path:
    """Resolve a dirname prefix (or full dirname) to one transition directory.

    Resolution rules:
    1. Exact dirname match → return that directory.
    2. Otherwise collect every directory whose dirname starts with ``prefix``.
    3. Zero matches → raise :class:`MySetupError`.
    4. One match → return it.
    5. Multiple matches → raise :class:`MySetupError` listing every candidate
       sorted ascending so the user can disambiguate.

    Used by ``my-setup transitions show <prefix>``. Read-only.
    """
    root = transitions_root()
    if not root.exists():
        raise MySetupError(f"no transition matching prefix {prefix!r}")
    exact = root / prefix
    if exact.is_dir() and (exact / "meta.json").exists():
        return exact
    matches = sorted(
        child
        for child in root.iterdir()
        if child.is_dir()
        and not child.name.startswith(".pending-")
        and child.name.startswith(prefix)
        and (child / "meta.json").exists()
    )
    if not matches:
        raise MySetupError(f"no transition matching prefix {prefix!r}")
    if len(matches) > 1:
        joined = "\n  ".join(child.name for child in matches)
        raise MySetupError(
            f"prefix {prefix!r} matches {len(matches)} transitions:\n  {joined}"
        )
    return matches[0]


def summarize_transition(transition_dir: Path) -> dict[str, str]:
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
