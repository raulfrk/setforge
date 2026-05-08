"""Capture-time merge wizard for deep-merge sub-key drift and symmetric
non-preserve top-level drift.

Today's ``my-setup sync`` (capture) is a silent absorb: read live, strip
``preserve_user_keys`` subtrees, write to tracked. Two gaps this module
addresses:

1. **Deep-merge sub-key drift** — once
   :class:`my_setup.config.Dotfile.preserve_user_keys_deep` declares a
   path, tracked is supposed to retain hand-maintained baseline content
   at deep sub-keys. The whole-subtree strip on capture undid that on
   the first ``sync``.
2. **Capture is asymmetric with install** — install fires the merge
   wizard for unexpected drift; capture silently absorbed the same
   class of drift. Tracked-only top-level keys were also lost.

This module exposes a two-flavor walker
(:func:`walk_capture_drift`) and a thin entry point
(:func:`run_capture_wizard`) that delegates to
:func:`my_setup.wizard.run_wizard_loop` with
``transition_command=TransitionCommand.SYNC``. The wizard's
``[k] / [u] / [s] / [m]`` actions cover both drift flavors uniformly —
no new actions needed.

Both walker flavors yield :class:`my_setup.wizard.DriftItem` records.
``DriftItem.mode`` carries ``"deep"`` for deep-merge sub-key drift and
``"shallow"`` for top-level non-preserve drift; the wizard's
``_action_use_live`` already routes through the matching overlay
variant.

The walker is silent on:

- shared-identical sub-keys / top-level keys (no drift),
- tracked-only sub-keys / top-level keys (preserved at writeback by the
  wizard's no-touch and by capture's post-wizard read of tracked),
- shallow-preserve top-level keys (capture strips them as today; no
  per-key decision is meaningful when the dotfile declares the strip),
- markdown dotfiles with ``preserve_user_sections=True`` (capture's
  section handling stays as today),
- dotfiles whose tracked or live file is missing (fresh capture or
  not-yet-deployed).
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

from rich.console import Console
from ruamel.yaml import YAML

from my_setup import jsonc, wizard
from my_setup.compare import expand_dotfile, resolve_dst, resolve_src
from my_setup.config import Config, resolve_profile
from my_setup.errors import CaptureRequiresInteractive
from my_setup.transitions import TransitionCommand
from my_setup.wizard import ActionResult, DriftItem


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
    dotfile_filter: str | None = None,
) -> Iterator[DriftItem]:
    """Yield :class:`DriftItem` records for both flavors of capture-time drift.

    1. **Deep-merge sub-key drift** — for every path in
       ``dotfile.preserve_user_keys_deep``, walk live's and tracked's
       sub-keys at the path. Yield for shared-different and live-only
       sub-keys; silent for shared-identical and tracked-only.
       ``DriftItem.mode = "deep"``; ``key_path`` is the full dotted path
       (e.g. ``"settings.fontSize"`` for JSONC, ``"a.b.c"`` for YAML).
    2. **Non-preserve top-level drift** — for each top-level key present
       in either side that is NOT in ``preserve_user_keys`` (shallow
       exact-match) AND NOT a top-level prefix of any path in
       ``preserve_user_keys_deep`` (the deep walker covers it),
       classify as a single shallow item.
       ``DriftItem.mode = "shallow"``; ``key_path`` is the top-level
       key.

    Both flavors share the same :class:`DriftItem` shape; ``mode`` is
    informational (drives prompt header context and routes
    ``_action_use_live`` to the matching overlay variant).

    Skipped (silent):

    - dotfiles with ``preserve_user_sections=True`` (markdown — section
      handling stays as today),
    - dotfiles whose tracked or live file is missing (fresh capture /
      not-yet-deployed),
    - sub-files inside directory dotfiles whose tracked or live copy is
      missing.

    Parameters
    ----------
    config:
        Loaded :class:`my_setup.config.Config`.
    profile_name:
        Profile to walk; profiles inherit from ``extends:`` chains.
    repo_root:
        Repo root used for ``resolve_src``.
    dotfile_filter:
        If set, only walk drift for the named dotfile (top-level key in
        ``config.dotfiles``).
    """
    resolved = resolve_profile(config, profile_name)
    for name in resolved.dotfiles:
        if dotfile_filter is not None and name != dotfile_filter:
            continue
        dotfile = config.dotfiles[name]
        if dotfile.preserve_user_sections:
            # Markdown / section dotfiles — capture's section handling
            # stays as today; not part of the wizard's contract.
            continue
        src = resolve_src(dotfile, repo_root)
        dst = resolve_dst(dotfile)
        for sub_name, sub_src, sub_dst in expand_dotfile(name, src, dst):
            if not sub_src.exists() or not sub_dst.exists():
                continue
            yield from _walk_one_file(
                dotfile_name=name,
                src=sub_src,
                dst=sub_dst,
                preserve_user_keys=list(dotfile.preserve_user_keys),
                preserve_user_keys_deep=list(dotfile.preserve_user_keys_deep),
            )


def _walk_one_file(
    *,
    dotfile_name: str,
    src: Path,
    dst: Path,
    preserve_user_keys: list[str],
    preserve_user_keys_deep: list[str],
) -> Iterator[DriftItem]:
    """Yield drift items for one (tracked, live) file pair.

    Format dispatch: JSONC iff :func:`my_setup.jsonc.is_jsonc_file`
    matches; everything else is YAML round-tripped.
    """
    if jsonc.is_jsonc_file(src):
        fmt: Literal["yaml", "jsonc"] = "jsonc"
        tracked = jsonc.parse_jsonc(src.read_text(encoding="utf-8"))
        live = jsonc.parse_jsonc(dst.read_text(encoding="utf-8"))
    else:
        fmt = "yaml"
        y = YAML(typ="rt")
        tracked = y.load(src.read_text(encoding="utf-8"))
        live = y.load(dst.read_text(encoding="utf-8"))

    if not isinstance(tracked, dict) or not isinstance(live, dict):
        return

    # 1. Deep-merge sub-key drift — walk under each declared deep path.
    # JSONC deep-merge sub-key walking is out of scope for `nen.23` v1
    # because the wizard's [u] action uses
    # :func:`my_setup.jsonc.overlay_user_keys`, which only handles
    # top-level literal key names. Per-sub-key JSONC drift lands via
    # `dotfiles-nen.19`. JSONC deep-merge top-level overlay still flows
    # through the existing capture-on-deploy primitives (``overlay_user_keys``
    # at deploy time) — capture-time wizard skips it for JSONC files.
    deep_paths_to_walk = (
        preserve_user_keys_deep if fmt != "jsonc" else []
    )
    for deep_path in deep_paths_to_walk:
        tracked_at = _navigate(tracked, deep_path)
        live_at = _navigate(live, deep_path)
        # If either side doesn't reach a dict at the deep path, the
        # wizard has nothing to merge per-sub-key. The whole-leaf
        # adoption is handled by deploy's overlay (with possible
        # MergeTypeMismatch). Nothing to yield from this pass.
        if not isinstance(tracked_at, dict) or not isinstance(live_at, dict):
            continue
        yield from _walk_deep(
            tracked_at,
            live_at,
            prefix=deep_path,
            dotfile_name=dotfile_name,
            src=src,
            dst=dst,
            fmt=fmt,
        )

    # 2. Non-preserve top-level drift — symmetric with install's
    # walk_unexpected_drift. Top-level keys not covered by either
    # preserve list (shallow exact OR top-level prefix of any deep
    # path) get a single shallow drift item per shared-different /
    # live-only top-level key. Note ``preserve_user_keys_deep`` (the
    # original list) is consulted here regardless of format so JSONC
    # deep-merge top-level keys are still skipped from non-preserve
    # walking.
    shallow_set = set(preserve_user_keys)
    deep_top_prefixes = {p.split(".", 1)[0] for p in preserve_user_keys_deep}
    seen_keys: set[str] = set()
    for top_key in list(live.keys()) + list(tracked.keys()):
        if top_key in seen_keys:
            continue
        seen_keys.add(top_key)
        if top_key in shallow_set:
            continue
        if top_key in deep_top_prefixes:
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
            dotfile_name=dotfile_name,
            src_path=src,
            dst_path=dst,
            key_path=top_key,
            tracked_value=tracked_value,
            live_value=live_value,
            file_format=fmt,
            mode="shallow",
        )


def _walk_deep(
    tracked_dict: dict,
    live_dict: dict,
    *,
    prefix: str,
    dotfile_name: str,
    src: Path,
    dst: Path,
    fmt: Literal["yaml", "jsonc"],
) -> Iterator[DriftItem]:
    """Recursively walk two dicts side-by-side under ``prefix``.

    Yields one :class:`DriftItem` (``mode='deep'``) per shared-different
    *leaf* (scalar or list — anything that isn't a dict on both sides)
    and per live-only key (regardless of shape; tracked has no value to
    preserve there). Tracked-only keys are silent (preserved at
    writeback by the wizard's no-touch).

    When both sides have a dict at the same key, recurse one level
    deeper so the user's per-key decision lives at the leaf.
    """
    for key in live_dict:
        sub_path = f"{prefix}.{key}"
        live_value = live_dict[key]
        if key not in tracked_dict:
            yield DriftItem(
                dotfile_name=dotfile_name,
                src_path=src,
                dst_path=dst,
                key_path=sub_path,
                tracked_value=None,
                live_value=live_value,
                file_format=fmt,
                mode="deep",
            )
            continue
        tracked_value = tracked_dict[key]
        if isinstance(tracked_value, dict) and isinstance(live_value, dict):
            yield from _walk_deep(
                tracked_value,
                live_value,
                prefix=sub_path,
                dotfile_name=dotfile_name,
                src=src,
                dst=dst,
                fmt=fmt,
            )
            continue
        if _equal(tracked_value, live_value):
            continue
        yield DriftItem(
            dotfile_name=dotfile_name,
            src_path=src,
            dst_path=dst,
            key_path=sub_path,
            tracked_value=tracked_value,
            live_value=live_value,
            file_format=fmt,
            mode="deep",
        )
    # tracked-only sub-keys: silent (preserved at writeback).


def _navigate(doc: Any, dotted_path: str) -> Any:
    """Walk ``doc`` along a dotted path. Returns ``None`` if any
    component is missing or a non-dict. Used by the walker to descend
    into deep paths declared in ``preserve_user_keys_deep``.

    JSONC's ``preserve_user_keys_deep`` paths are literal direct-children
    of the top-level object per spec (v1); the dotted-path treatment
    here is general but in practice a JSONC deep entry is a single key
    name without dots."""
    node = doc
    for part in dotted_path.split("."):
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
    my_setup_yaml_path: Path,
    snapshot_base: Path | None = None,
    console: Console | None = None,
    auto_accept: str | None = None,
    dotfile_filter: str | None = None,
) -> list[tuple[DriftItem, ActionResult]]:
    """Fire the merge wizard at capture time.

    Thin wrapper around :func:`my_setup.wizard.run_wizard_loop` that
    supplies the capture-trigger walker, transition command, and
    pending-edit message. Snapshot base defaults to
    ``~/.local/state/my-setup/sync-snapshots`` (parallel to merge's
    ``merge-snapshots``).

    Parameters
    ----------
    config:
        Loaded :class:`my_setup.config.Config`.
    profile_name:
        Profile to walk.
    repo_root:
        Repo root used for ``resolve_src``.
    my_setup_yaml_path:
        Path to ``my_setup.yaml`` — needed by the ``[s]`` action.
    snapshot_base:
        Parent directory for the timestamped snapshot dir. Defaults to
        ``~/.local/state/my-setup/sync-snapshots``.
    console:
        Rich Console (defaults to a fresh ``Console()``).
    auto_accept:
        ``"k"`` or ``"u"`` for non-interactive runs (sync gating).
        ``None`` enables interactive prompts and signal handlers.
    dotfile_filter:
        If set, only walk drift for the named dotfile.

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
        snapshot_base = (
            Path.home() / ".local" / "state" / "my-setup" / "sync-snapshots"
        )
    if console is None:
        console = Console()

    items = walk_capture_drift(
        config, profile_name, repo_root, dotfile_filter=dotfile_filter
    )
    pending_message = (
        f"[yellow]pending manual edit in {{src_path}}; "
        f"resume with: my-setup sync --profile={profile_name}[/yellow]"
    )
    return wizard.run_wizard_loop(
        items,
        my_setup_yaml_path=my_setup_yaml_path,
        snapshot_base=snapshot_base,
        console=console,
        auto_accept=auto_accept,
        transition_command=TransitionCommand.SYNC,
        profile=profile_name,
        pending_message=pending_message,
    )
