"""Capture-time merge wizard for deep-merge sub-key drift and symmetric
non-preserve top-level drift.

Today's ``setforge sync`` (capture) is a silent absorb: read live, strip
``preserve_user_keys`` subtrees, write to tracked. Two gaps this module
addresses:

1. **Deep-merge sub-key drift** — once
   :class:`setforge.config.TrackedFile.preserve_user_keys_deep` declares a
   path, tracked is supposed to retain hand-maintained baseline content
   at deep sub-keys. The whole-subtree strip on capture undid that on
   the first ``sync``.
2. **Capture is asymmetric with install** — install fires the merge
   wizard for unexpected drift; capture silently absorbed the same
   class of drift. Tracked-only top-level keys were also lost.

This module exposes a two-flavor walker
(:func:`walk_capture_drift`) and a thin entry point
(:func:`run_capture_wizard`) that delegates to
:func:`setforge.wizard.run_wizard_loop` with
``transition_command=TransitionCommand.SYNC``. The wizard's
``[k] / [u] / [s] / [m]`` actions cover both drift flavors uniformly —
no new actions needed.

Both walker flavors yield :class:`setforge.wizard.DriftItem` records.
``DriftItem.mode`` carries ``"deep"`` for deep-merge sub-key drift and
``"shallow"`` for top-level non-preserve drift; the wizard's
``_action_use_live`` already routes through the matching overlay
variant.

The walker is silent on:

- shared-identical sub-keys / top-level keys (no drift),
- tracked-only sub-keys / top-level keys (preserved at writeback by the
  wizard's no-touch and by capture's post-wizard read of tracked),
- shallow-preserve top-level keys (capture strips them as today; no
  per-key decision is meaningful when the tracked_file declares the strip),
- markdown tracked_files with ``preserve_user_sections=True`` (capture's
  section handling stays as today),
- tracked_files whose tracked or live file is missing (fresh capture or
  not-yet-deployed).
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from rich.console import Console

# ruamel.yaml ships py.typed without resolvable annotations; no stub pkg on PyPI.
from ruamel.yaml import YAML  # type: ignore[import-not-found]

from setforge import jsonc, wizard
from setforge.compare import expand_tracked_file, resolve_dst, resolve_src
from setforge.config import Config, resolve_profile
from setforge.errors import CaptureRequiresInteractive
from setforge.jsonc import PATH_SEPARATOR, preserved_positions_for_top
from setforge.transitions import TransitionCommand
from setforge.wizard import ActionResult, DriftItem, DriftMode, FileFormat

__all__ = [
    "CaptureRequiresInteractive",
    "run_capture_wizard",
    "walk_capture_drift",
]


# ---------------------------------------------------------------------------
# Walker
# ---------------------------------------------------------------------------


def walk_capture_drift(
    config: Config,
    profile_name: str,
    repo_root: Path,
    tracked_file_filter: str | None = None,
) -> Iterator[DriftItem]:
    """Yield :class:`DriftItem` records for both flavors of capture-time drift.

    1. **Deep-merge sub-key drift** — for every path in
       ``tracked_file.preserve_user_keys_deep``, walk live's and tracked's
       sub-keys at the path. Yield for shared-different and live-only
       sub-keys; silent for shared-identical and tracked-only.
       ``DriftItem.mode is DriftMode.DEEP``; ``key_path`` is the full dotted path
       (e.g. ``"settings.fontSize"`` for JSONC, ``"a.b.c"`` for YAML).
    2. **Non-preserve top-level drift** — for each top-level key present
       in either side that is NOT in ``preserve_user_keys`` (shallow
       exact-match) AND NOT a top-level prefix of any path in
       ``preserve_user_keys_deep`` (the deep walker covers it),
       classify as a single shallow item.
       ``DriftItem.mode is DriftMode.SHALLOW``; ``key_path`` is the top-level
       key.

    Both flavors share the same :class:`DriftItem` shape; ``mode`` is
    informational (drives prompt header context and routes
    ``_action_use_live`` to the matching overlay variant).

    Skipped (silent):

    - tracked_files with ``preserve_user_sections=True`` (markdown — section
      handling stays as today),
    - tracked_files whose tracked or live file is missing (fresh capture /
      not-yet-deployed),
    - sub-files inside directory tracked_files whose tracked or live copy is
      missing.

    Parameters
    ----------
    config:
        Loaded :class:`setforge.config.Config`.
    profile_name:
        Profile to walk; profiles inherit from ``extends:`` chains.
    repo_root:
        Repo root used for ``resolve_src``.
    tracked_file_filter:
        If set, only walk drift for the named tracked_file (top-level key in
        ``config.tracked_files``).
    """
    resolved = resolve_profile(config, profile_name)
    for name in resolved.tracked_files:
        if tracked_file_filter is not None and name != tracked_file_filter:
            continue
        tracked_file = config.tracked_files[name]
        if tracked_file.preserve_user_sections:
            # Markdown / section tracked_files — capture's section handling
            # stays as today; not part of the wizard's contract.
            continue
        src = resolve_src(tracked_file, repo_root)
        dst = resolve_dst(tracked_file)
        for _sub_name, sub_src, sub_dst in expand_tracked_file(name, src, dst):
            if not sub_src.exists() or not sub_dst.exists():
                continue
            yield from _walk_one_file(
                tracked_file_name=name,
                src=sub_src,
                dst=sub_dst,
                preserve_user_keys=list(tracked_file.preserve_user_keys),
                preserve_user_keys_deep=list(tracked_file.preserve_user_keys_deep),
            )


def _walk_one_file(
    *,
    tracked_file_name: str,
    src: Path,
    dst: Path,
    preserve_user_keys: list[str],
    preserve_user_keys_deep: list[str],
) -> Iterator[DriftItem]:
    """Yield drift items for one (tracked, live) file pair.

    Format dispatch: JSONC iff :func:`setforge.jsonc.is_jsonc_file`
    matches; everything else is YAML round-tripped.
    """
    if jsonc.is_jsonc_file(src):
        fmt: FileFormat = FileFormat.JSONC
        tracked = jsonc.parse_jsonc(src.read_text(encoding="utf-8"))
        live = jsonc.parse_jsonc(dst.read_text(encoding="utf-8"))
    else:
        fmt = FileFormat.YAML
        y = YAML(typ="rt")
        tracked = y.load(src.read_text(encoding="utf-8"))
        live = y.load(dst.read_text(encoding="utf-8"))

    if not isinstance(tracked, dict) or not isinstance(live, dict):
        return

    nested_path_heads: set[str] = (
        _nested_path_heads(preserve_user_keys) if fmt is FileFormat.JSONC else set()
    )
    yield from _walk_deep_phase(
        tracked=tracked,
        live=live,
        preserve_user_keys=preserve_user_keys,
        preserve_user_keys_deep=preserve_user_keys_deep,
        nested_path_heads=nested_path_heads,
        fmt=fmt,
        tracked_file_name=tracked_file_name,
        src=src,
        dst=dst,
    )
    yield from _walk_shallow_top_phase(
        tracked=tracked,
        live=live,
        preserve_user_keys=preserve_user_keys,
        preserve_user_keys_deep=preserve_user_keys_deep,
        nested_path_heads=nested_path_heads,
        fmt=fmt,
        tracked_file_name=tracked_file_name,
        src=src,
        dst=dst,
    )


def _walk_deep_phase(
    *,
    tracked: dict,
    live: dict,
    preserve_user_keys: list[str],
    preserve_user_keys_deep: list[str],
    nested_path_heads: set[str],
    fmt: FileFormat,
    tracked_file_name: str,
    src: Path,
    dst: Path,
) -> Iterator[DriftItem]:
    """Phase 1 of ``_walk_one_file``: deep-merge sub-key drift.

    Walks under each declared deep path, and (JSONC only) under each
    nested-path head from ``preserve_user_keys`` — heads are walked
    even though they're not in ``preserve_user_keys_deep`` so the
    wizard can prompt on UNCOVERED sibling drift while remaining
    silent on path-preserved leaves (per ``setforge-nen.19`` spec).
    Behavior lifted verbatim from ``_walk_one_file``.
    """
    deep_paths_to_walk = list(preserve_user_keys_deep)
    for head in sorted(nested_path_heads):
        if head not in deep_paths_to_walk:
            deep_paths_to_walk.append(head)
    for deep_path in deep_paths_to_walk:
        tracked_at = _navigate(tracked, deep_path, fmt)
        live_at = _navigate(live, deep_path, fmt)
        # If either side doesn't reach a dict at the deep path, the
        # wizard has nothing to merge per-sub-key. The whole-leaf
        # adoption is handled by deploy's overlay (with possible
        # MergeTypeMismatch). Nothing to yield from this pass.
        if not isinstance(tracked_at, dict) or not isinstance(live_at, dict):
            continue
        preserved_positions = (
            preserved_positions_for_top(deep_path, preserve_user_keys)
            if fmt is FileFormat.JSONC
            else set()
        )
        yield from _walk_deep(
            tracked_at,
            live_at,
            prefix=deep_path,
            tracked_file_name=tracked_file_name,
            src=src,
            dst=dst,
            fmt=fmt,
            preserved_positions=preserved_positions,
            position=(),
        )


def _walk_shallow_top_phase(
    *,
    tracked: dict,
    live: dict,
    preserve_user_keys: list[str],
    preserve_user_keys_deep: list[str],
    nested_path_heads: set[str],
    fmt: FileFormat,
    tracked_file_name: str,
    src: Path,
    dst: Path,
) -> Iterator[DriftItem]:
    """Phase 2 of ``_walk_one_file``: non-preserve top-level drift.

    Symmetric with install's ``walk_unexpected_drift``. Top-level keys
    not covered by either preserve list (shallow exact OR top-level
    prefix of any deep path OR — JSONC only — head of a nested path)
    get a single shallow drift item per shared-different / live-only
    top-level key. Behavior lifted verbatim from ``_walk_one_file``.
    """
    shallow_set = set(preserve_user_keys)
    deep_top_prefixes = {p.split(".", 1)[0] for p in preserve_user_keys_deep}
    skip_top_level = shallow_set | deep_top_prefixes | nested_path_heads
    seen_keys: set[str] = set()
    for top_key in list(live.keys()) + list(tracked.keys()):
        if top_key in seen_keys:
            continue
        seen_keys.add(top_key)
        if top_key in skip_top_level:
            continue
        in_live = top_key in live
        in_tracked = top_key in tracked
        live_value = live.get(top_key) if in_live else None
        tracked_value = tracked.get(top_key) if in_tracked else None
        if in_live and in_tracked:
            if _equal(tracked_value, live_value):
                continue
            # Shared-different → shallow drift item.
        elif in_live and not in_tracked:
            # Live-only → shallow drift item with tracked_value=None.
            pass
        else:
            # Tracked-only → silent (preserved at writeback).
            continue
        yield DriftItem(
            tracked_file_name=tracked_file_name,
            src_path=src,
            dst_path=dst,
            key_path=top_key,
            tracked_value=tracked_value,
            live_value=live_value,
            file_format=fmt,
            mode=DriftMode.SHALLOW,
        )


def _walk_deep(
    tracked_dict: dict,
    live_dict: dict,
    *,
    prefix: str,
    tracked_file_name: str,
    src: Path,
    dst: Path,
    fmt: FileFormat,
    preserved_positions: set[tuple[str, ...]] | None = None,
    position: tuple[str, ...] = (),
) -> Iterator[DriftItem]:
    """Recursively walk two dicts side-by-side under ``prefix``.

    Yields one :class:`DriftItem` (``mode is DriftMode.DEEP``) per shared-different
    *leaf* (scalar or list — anything that isn't a dict on both sides)
    and per live-only key (regardless of shape; tracked has no value to
    preserve there). Tracked-only keys are silent (preserved at
    writeback by the wizard's no-touch).

    JSONC nested-path filter: when ``preserved_positions`` contains the
    current sub-key's position-tuple, the wizard skips emitting a
    :class:`DriftItem` — the leaf is auto-applied by deploy's overlay
    via :func:`setforge.jsonc.overlay_user_keys` instead.

    When both sides have a dict at the same key, recurse one level
    deeper so the user's per-key decision lives at the leaf.
    """
    preserved = preserved_positions or set()
    for key in live_dict:
        sub_position = (*position, key)
        if sub_position in preserved:
            continue
        sub_path = _join(prefix, key, fmt)
        live_value = live_dict[key]
        if key not in tracked_dict:
            yield DriftItem(
                tracked_file_name=tracked_file_name,
                src_path=src,
                dst_path=dst,
                key_path=sub_path,
                tracked_value=None,
                live_value=live_value,
                file_format=fmt,
                mode=DriftMode.DEEP,
            )
            continue
        tracked_value = tracked_dict[key]
        if isinstance(tracked_value, dict) and isinstance(live_value, dict):
            yield from _walk_deep(
                tracked_value,
                live_value,
                prefix=sub_path,
                tracked_file_name=tracked_file_name,
                src=src,
                dst=dst,
                fmt=fmt,
                preserved_positions=preserved,
                position=sub_position,
            )
            continue
        if _equal(tracked_value, live_value):
            continue
        yield DriftItem(
            tracked_file_name=tracked_file_name,
            src_path=src,
            dst_path=dst,
            key_path=sub_path,
            tracked_value=tracked_value,
            live_value=live_value,
            file_format=fmt,
            mode=DriftMode.DEEP,
        )
    # tracked-only sub-keys: silent (preserved at writeback).


def _join(prefix: str, key: str, fmt: FileFormat) -> str:
    """Format-aware key-path separator.

    YAML continues to emit ``"a.b"`` (legacy ``preserve_user_keys_deep``
    convention). JSONC emits ``"a > b"`` (nested-path syntax from
    ``setforge-nen.19``) — the ``[u]se-live`` action forwards the
    ``key_path`` to :func:`setforge.jsonc.overlay_user_keys`, which
    parses on ``" > "``.
    """
    if fmt is FileFormat.JSONC:
        return f"{prefix}{PATH_SEPARATOR}{key}"
    return f"{prefix}.{key}"


def _nested_path_heads(preserve_user_keys: list[str]) -> set[str]:
    """Heads of every JSONC nested path in ``preserve_user_keys``.

    Used by the JSONC walker to (a) extend the deep-walk set with
    top-level keys whose interior has preserve coverage and (b) suppress
    the top-level shallow walker for those same keys (handled per-leaf
    by the deep walker).
    """
    heads: set[str] = set()
    for name in preserve_user_keys:
        if PATH_SEPARATOR not in name:
            continue
        heads.add(name.split(PATH_SEPARATOR, 1)[0])
    return heads


def _navigate(doc: Any, path: str, fmt: FileFormat) -> Any:
    """Walk ``doc`` along a format-appropriate path. Returns ``None`` if
    any component is missing or a non-dict.

    YAML splits on ``.``; JSONC splits on ``" > "``. JSONC's
    ``preserve_user_keys_deep`` entries are single literal keys per
    spec, but routing through this helper keeps the path-walking
    symmetric across formats and ready for future deep-paths-with-
    separators support.
    """
    parts = path.split(PATH_SEPARATOR) if fmt is FileFormat.JSONC else path.split(".")
    node = doc
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _equal(a: Any, b: Any) -> bool:
    """Compare two parsed values for capture-drift equality.

    ruamel.yaml round-trip mode produces wrapper types (``CommentedMap``,
    ``CommentedSeq``, ``ScalarFloat``, …) that compare equal to plain
    Python values via ``==``; relying on ``==`` here keeps the walker
    format-agnostic (YAML and JSONC both go through this helper)."""
    return a == b


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_capture_wizard(
    config: Config,
    profile_name: str,
    repo_root: Path,
    *,
    setforge_yaml_path: Path,
    snapshot_base: Path | None = None,
    console: Console | None = None,
    auto_accept: str | None = None,
    tracked_file_filter: str | None = None,
) -> list[tuple[DriftItem, ActionResult]]:
    """Fire the merge wizard at capture time.

    Thin wrapper around :func:`setforge.wizard.run_wizard_loop` that
    supplies the capture-trigger walker, transition command, and
    pending-edit message. Snapshot base defaults to
    ``~/.local/state/setforge/sync-snapshots`` (parallel to merge's
    ``merge-snapshots``).

    Parameters
    ----------
    config:
        Loaded :class:`setforge.config.Config`.
    profile_name:
        Profile to walk.
    repo_root:
        Repo root used for ``resolve_src``.
    setforge_yaml_path:
        Path to ``setforge.yaml`` — needed by the ``[s]`` action.
    snapshot_base:
        Parent directory for the timestamped snapshot dir. Defaults to
        ``~/.local/state/setforge/sync-snapshots``.
    console:
        Rich Console (defaults to a fresh ``Console()``).
    auto_accept:
        ``"k"`` or ``"u"`` for non-interactive runs (sync gating).
        ``None`` enables interactive prompts and signal handlers.
    tracked_file_filter:
        If set, only walk drift for the named tracked_file.

    Returns
    -------
    list of ``(DriftItem, ActionResult)`` pairs — one per drift item
    walked, in walk order. The list ends at the first
    ``MANUAL_PENDING``.

    Raises
    ------
    KeyboardInterrupt
        When the user presses Ctrl-C and ``auto_accept`` is ``None``.
        Callers (the CLI layer) are expected to surface a clean exit
        code 130.
    """
    if snapshot_base is None:
        snapshot_base = Path.home() / ".local" / "state" / "setforge" / "sync-snapshots"
    if console is None:
        console = Console()

    items = walk_capture_drift(
        config, profile_name, repo_root, tracked_file_filter=tracked_file_filter
    )
    pending_message = (
        f"[yellow]pending manual edit in {{src_path}}; "
        f"resume with: setforge sync --profile={profile_name}[/yellow]"
    )
    return wizard.run_wizard_loop(
        items,
        setforge_yaml_path=setforge_yaml_path,
        snapshot_base=snapshot_base,
        console=console,
        auto_accept=auto_accept,
        transition_command=TransitionCommand.SYNC,
        profile=profile_name,
        pending_message=pending_message,
    )
