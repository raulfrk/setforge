"""stage subcommand — per-hunk share/keep classification for plain files (A5).

``setforge stage <file>`` walks each base↔live diff hunk of a plain tracked
file and lets the host classify it SHARED (promote into the shared config on the
next ``sync``) or LOCAL (keep host-only). ``setforge stage --list`` is a
read-only per-file count of SHARED / LOCAL / PENDING hunks — it writes nothing.

The classifications are persisted into the reconcile index; the actual promotion
into ``tracked/`` happens on ``sync`` (see :func:`setforge.capture.capture_profile`).
"""

from __future__ import annotations

import stat
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Final

import typer
from prompt_toolkit.styles import Style
from rich.console import Console

from setforge import atomicio
from setforge.cli import (
    _CONFIG_OPTION,
    _PROFILE_OPTION,
    _resolve_config_arg,
    app,
)
from setforge.cli._help_examples import STAGE_EXAMPLES
from setforge.cli._output import OutputContext, render
from setforge.compare import expand_tracked_file, resolve_dst, resolve_src
from setforge.config import Config, ResolvedProfile, load_config, resolve_profile
from setforge.locking import profile_lock
from setforge.reconcile import hunks as hunks_mod
from setforge.reconcile import share_draft
from setforge.reconcile import store as reconcile_store
from setforge.reconcile.hunks import Hunk
from setforge.reconcile.merge import split_lines
from setforge.reconcile.types import FileId, HunkClass, file_id
from setforge.ui import THEME, Button, button_bar, pt_style
from setforge.ui.widgets import CANCEL


@dataclass(frozen=True, slots=True)
class FileStage:
    """One plain file's staged-capture view: its base/live and classified hunks."""

    sub_name: str
    fid: FileId
    src: Path
    dst: Path
    base: bytes
    live: bytes
    hunks: list[Hunk]


class _Quit:
    """Sentinel: the user asked to stop the walk (kept choices persist)."""


QUIT: Final = _Quit()


class _Menu(Enum):
    """Button sentinels for the Share / draft sub-menus (distinct from HunkClass)."""

    SHARE = "share"
    DRAFT = "draft"
    VERBATIM = "verbatim"


@dataclass(frozen=True, slots=True)
class Decision:
    """One hunk's staging outcome from the walk.

    ``cls`` is the class to record. ``draft`` carries the shareable bytes for a
    ``SHARED_DRAFTED`` hunk (``None`` otherwise). ``adopt`` is ``True`` when the
    host also wants their live region rewritten to the draft (no divergence) —
    applied as a batched live-rewrite at persist; the recorded class stays
    ``SHARED_DRAFTED`` either way (classify matches it by anchor, live-independent).
    """

    cls: HunkClass
    draft: bytes | None = None
    adopt: bool = False


#: A per-hunk choose callback: ``(hunk, index, total) -> Decision | None | QUIT``
#: (``None`` = skip / leave the class unchanged).
type Choice = Callable[[Hunk, int, int], Decision | None | _Quit]


@dataclass(frozen=True, slots=True)
class WalkResult:
    """The walk's outcome: updated hunks + the per-anchor drafts + adopt set."""

    hunks: list[Hunk]
    drafts: dict[str, bytes]
    adopt_anchors: set[str]


def collect_stages(
    cfg: Config,
    resolved: ResolvedProfile,
    repo_root: Path,
    profile: str,
    *,
    only: str | None = None,
) -> list[FileStage]:
    """Classified-hunk view for each staged-eligible plain file. READ-ONLY.

    Eligibility mirrors the install reconcile gate: a plain tracked file (no
    disposition, no spans), present live, a recorded merge base, and UTF-8 on
    both sides. ``only`` filters to a single file by tracked-file name, sub-name,
    live path, or live basename. Writes nothing — safe for ``--list``.
    """
    stages: list[FileStage] = []
    for name in resolved.tracked_files:
        tracked_file = cfg.tracked_files[name]
        if tracked_file.disposition is not None or tracked_file.spans:
            continue
        src = resolve_src(tracked_file, repo_root)
        dst = resolve_dst(tracked_file)
        for sub_name, sub_src, sub_dst in expand_tracked_file(name, src, dst):
            if only is not None and only not in (
                name,
                sub_name,
                str(sub_dst),
                sub_dst.name,
            ):
                continue
            if not sub_dst.exists():
                continue
            fid = file_id(sub_name)
            base = reconcile_store.read_base(profile, fid)
            if base is None:
                continue  # not reconcile-managed (run `setforge install` first)
            live = sub_dst.read_bytes()
            try:
                base.decode("utf-8")
                live.decode("utf-8")
            except UnicodeDecodeError:
                continue  # binary plain file — staging is text-only
            entry = reconcile_store.read_index(profile).files.get(str(fid))
            stored = entry.hunks if entry is not None else []
            hunks = hunks_mod.classify(hunks_mod.extract_hunks(base, live), stored)
            stages.append(FileStage(sub_name, fid, sub_src, sub_dst, base, live, hunks))
    return stages


def counts(hunks: list[Hunk]) -> Counter[HunkClass]:
    """Tally hunks by class (SHARED / LOCAL / PENDING)."""
    return Counter(hunk.cls for hunk in hunks)


def walk(hunks: list[Hunk], choose: Choice) -> WalkResult:
    """Apply one :class:`Decision` per hunk, collecting drafts + the adopt set.

    ``choose(hunk, index, total)`` returns a :class:`Decision` to (re)classify,
    ``None`` to leave the hunk unchanged (skip / next), or :data:`QUIT` to stop
    early. Choices made before a QUIT are kept. A drafted decision records the
    hunk's ``draft_hash`` and stashes its bytes under the hunk's ``anchor``; an
    ``adopt`` decision additionally marks the anchor for the live-rewrite.
    """
    out = list(hunks)
    drafts: dict[str, bytes] = {}
    adopt_anchors: set[str] = set()
    for index, hunk in enumerate(hunks):
        decision = choose(hunk, index, len(hunks))
        if isinstance(decision, _Quit):
            break
        if decision is None:
            continue
        draft_hash = (
            hunks_mod._sha(decision.draft) if decision.draft is not None else None
        )
        out[index] = replace(hunk, cls=decision.cls, draft_hash=draft_hash)
        if decision.draft is not None:
            drafts[hunk.anchor] = decision.draft
        if decision.adopt:
            adopt_anchors.add(hunk.anchor)
    return WalkResult(hunks=out, drafts=drafts, adopt_anchors=adopt_anchors)


def _hunk_preview(stage: FileStage, hunk: Hunk) -> str:
    """A small ±diff preview of one hunk for the button-bar body."""
    base_lines = split_lines(stage.base)
    live_lines = split_lines(stage.live)
    i1, i2 = hunk.base_span
    j1, j2 = hunk.live_span
    removed = [b"- " + line for line in base_lines[i1:i2]]
    added = [b"+ " + line for line in live_lines[j1:j2]]
    body = b"".join(removed + added).decode("utf-8", "replace")
    return body if len(body) <= 600 else body[:599] + "…"


def _interactive_choice(stage: FileStage) -> Choice:
    """A button-bar-backed choose callback for the interactive walk."""
    # Strip the ``class:`` prefix pt_style emits — Style.from_dict keys on bare
    # role names (mirrors claude_merge / wizard / share_draft _themed_style;
    # de-forking the four copies is tracked separately).
    style = Style.from_dict(
        {key.removeprefix("class:"): value for key, value in pt_style(THEME).items()}
    )

    def choose(hunk: Hunk, index: int, total: int) -> Decision | None | _Quit:
        flag = " (changed)" if hunk.changed else ""
        current = f"currently {hunk.cls.value}"
        result = button_bar(
            [
                Button("Share", _Menu.SHARE),
                Button("Keep local", HunkClass.LOCAL),
                Button("Skip", None),
                Button("Quit", QUIT),
            ],
            title=f"stage {stage.sub_name} — hunk {index + 1}/{total}: "
            f"{hunk.label}{flag}",
            body=f"{_hunk_preview(stage, hunk)}\n[{current}]",
            initial=0 if hunk.cls is not HunkClass.LOCAL else 1,
            style=style,
        )
        if result is CANCEL or isinstance(result, _Quit):
            return QUIT  # Esc / Ctrl-C / Quit stops the walk, keeping prior choices
        if result is None:
            return None  # Skip — leave the class unchanged
        if result is HunkClass.LOCAL:
            return Decision(HunkClass.LOCAL)
        return _share_submenu(stage, hunk, style)  # Share → how to share

    return choose


def _share_submenu(stage: FileStage, hunk: Hunk, style: Style) -> Decision | None:
    """The Share sub-menu: draft (Claude rewrite) / verbatim / skip.

    Returns a SHARED_DRAFTED :class:`Decision` (carrying the draft + adopt flag),
    a plain SHARED Decision (verbatim), or ``None`` (skip / back / draft-cancelled
    — the hunk is left unchanged). The draft session is constructed per-hunk inside
    :func:`share_draft.draft_hunk` and discarded on accept/cancel.
    """
    result = button_bar(
        [
            Button("Draft (Claude)", _Menu.DRAFT),
            Button("Verbatim", _Menu.VERBATIM),
            Button("Skip", None),
        ],
        title=f"share {hunk.label} — rewrite host-specific text?",
        body=(
            "Draft: Claude rewrites this region into a shareable version "
            "(then adopt it locally or keep your local bytes).\n"
            "Verbatim: share your live bytes as-is."
        ),
        initial=0,
        style=style,
    )
    if result is CANCEL or result is None:
        return None  # back / skip → leave unchanged
    if result is _Menu.VERBATIM:
        return Decision(HunkClass.SHARED)
    # Draft: hand the host-specific live region to Claude for a shareable rewrite.
    j1, j2 = hunk.live_span
    region = b"".join(split_lines(stage.live)[j1:j2])
    outcome = share_draft.draft_hunk(region, display_path=stage.sub_name)
    if outcome is CANCEL:
        return None  # draft cancelled → leave the hunk unchanged
    return Decision(HunkClass.SHARED_DRAFTED, draft=outcome.draft, adopt=outcome.adopt)


def _adopt_live(stage: FileStage, result: WalkResult) -> bytes:
    """Splice each adopted hunk's draft into the live bytes (the Adopt rewrite).

    Returns ``stage.live`` unchanged when nothing was adopted. Each adopted region
    is replaced by its draft; every other region (including a keep-mine-local
    drafted hunk, whose live stays host-specific) passes through verbatim. Because
    a ``SHARED_DRAFTED`` hunk is matched by anchor (base-context, unchanged by the
    rewrite), re-extraction after the rewrite re-identifies it cleanly.
    """
    if not result.adopt_anchors:
        return stage.live
    live_lines = split_lines(stage.live)
    adopted = sorted(
        (h for h in result.hunks if h.anchor in result.adopt_anchors),
        key=lambda h: h.live_span[0],
    )
    out: list[bytes] = []
    cursor = 0
    for hunk in adopted:
        j1, j2 = hunk.live_span
        out.extend(live_lines[cursor:j1])
        out.append(result.drafts[hunk.anchor])
        cursor = j2
    out.extend(live_lines[cursor:])
    return b"".join(out)


def _apply(profile: str, stage: FileStage, result: WalkResult) -> None:
    """Apply the walk: rewrite live for any Adopt (atomic, captured mode), then
    persist the classifications + drafts under the lock."""
    final_live = _adopt_live(stage, result)
    if final_live != stage.live:
        # stat() (follow) so a symlinked dst keeps its target's mode, not the link's.
        mode = stat.S_IMODE(stage.dst.stat().st_mode)
        atomicio.atomic_write_bytes(stage.dst, final_live, mode=mode)
    _persist(profile, stage, result, final_live)


def _persist(
    profile: str, stage: FileStage, result: WalkResult, final_live: bytes
) -> None:
    """Record the walk's classifications + drafts, merging with the current index
    UNDER the lock so a concurrent ``sync`` is not clobbered (lost-update RMW).

    The walk read + classified the index at collect time, OUTSIDE the lock; a
    naive whole-list overwrite here would drop any classification a concurrent
    ``sync`` committed in between. Instead, re-read the index under the lock,
    re-extract the (post-Adopt) base/live, and overlay ONLY the anchors the host
    explicitly decided (class changed from collect time, OR a draft attached) — so
    an anchor the host skipped keeps whatever the concurrent writer left, while the
    host's explicit choices win. base is UNCHANGED (sync/install own it).

    The drafts manifest is reconciled to EXACTLY the surviving ``SHARED_DRAFTED``
    set: prior drafts are kept, this walk's are added, and any whose hunk demoted
    away is pruned — so a demote never leaves an orphan manifest entry.

    Caveat: "explicitly decided" diffs the walk class against the collect-time
    class, so an explicit re-confirm to the SAME class (with no draft) reads as a
    skip — in the narrow race where a concurrent writer flips that anchor meanwhile,
    the concurrent value wins. Acceptable for a single-user CLI.
    """
    collect_cls = {h.anchor: h.cls for h in stage.hunks}
    walk_by_anchor = {h.anchor: h for h in result.hunks}
    # "decided" = anchors the host acted on THIS walk: a class change from the
    # collect-time class, OR a draft authored this walk. Keying the draft case on
    # result.drafts (not a draft_hash carried forward by classify for an already-
    # SHARED_DRAFTED hunk the host SKIPPED) keeps a skip from re-asserting a stale
    # value over a concurrent writer.
    decided = {
        anchor
        for anchor, hunk in walk_by_anchor.items()
        if collect_cls.get(anchor) != hunk.cls or anchor in result.drafts
    }
    with profile_lock(profile):
        entry = reconcile_store.read_index(profile).files.get(str(stage.fid))
        stored = entry.hunks if entry is not None else []
        current = hunks_mod.classify(
            hunks_mod.extract_hunks(stage.base, final_live), stored
        )
        merged = [
            replace(
                h,
                cls=walk_by_anchor[h.anchor].cls,
                draft_hash=walk_by_anchor[h.anchor].draft_hash,
            )
            if h.anchor in decided
            else h
            for h in current
        ]
        pool = {**reconcile_store.read_drafts(profile, stage.fid), **result.drafts}
        drafts = {
            h.anchor: pool[h.anchor]
            for h in merged
            if h.cls is HunkClass.SHARED_DRAFTED and h.anchor in pool
        }
        reconcile_store.record(
            profile,
            stage.fid,
            base=stage.base,
            local=final_live,
            hunks=hunks_mod.serialize(merged),
            drafts=drafts,
        )


@app.command(epilog=STAGE_EXAMPLES)
def stage(
    ctx: typer.Context,
    file: str = typer.Argument(
        None, help="Tracked file to stage (name or live path). Omit with --list."
    ),
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    list_only: bool = typer.Option(
        False, "--list", help="Read-only: per-file share/local/pending hunk counts."
    ),
) -> None:
    """Classify a plain file's local changes per hunk: share upstream or keep local."""
    config = _resolve_config_arg(config)
    cfg = load_config(config)
    repo_root = config.resolve().parent
    resolved = resolve_profile(cfg, profile)

    if list_only:
        stages = collect_stages(cfg, resolved, repo_root, profile)
        _render_list(ctx.obj, stages)
        return

    if file is None:
        raise typer.BadParameter("stage requires a FILE argument (or use --list)")
    if not sys.stdin.isatty():
        typer.secho(
            "stage is interactive; run `setforge stage --list` to inspect "
            "hunk classes non-interactively",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=2)

    stages = collect_stages(cfg, resolved, repo_root, profile, only=file)
    if not stages:
        typer.secho(
            f"{file}: nothing to stage — no local changes over a recorded base "
            f"(run `setforge install --profile={profile}` first if it is new)",
            err=True,
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=0)

    console = Console()
    for stage_item in stages:
        if not stage_item.hunks:
            continue
        result = walk(stage_item.hunks, _interactive_choice(stage_item))
        _apply(profile, stage_item, result)
        tally = counts(result.hunks)
        drafted = tally[HunkClass.SHARED_DRAFTED]
        drafted_note = f"  {drafted} drafted" if drafted else ""
        console.print(
            f"{stage_item.sub_name}: "
            f"{tally[HunkClass.SHARED]} shared{drafted_note}  "
            f"{tally[HunkClass.LOCAL]} local  "
            f"{tally[HunkClass.PENDING]} pending"
        )


def _render_list(ctx_obj: OutputContext | None, stages: list[FileStage]) -> None:
    """Render the read-only per-file hunk-class table."""
    data = [
        {
            "name": stage.sub_name,
            "shared": counts(stage.hunks)[HunkClass.SHARED],
            "local": counts(stage.hunks)[HunkClass.LOCAL],
            "pending": counts(stage.hunks)[HunkClass.PENDING],
        }
        for stage in stages
    ]

    def _human() -> None:
        console = Console()
        if not data:
            console.print("no staged-eligible plain files with local changes")
            return
        for row in data:
            console.print(
                f"{row['name']}: "
                f"{row['shared']} shared  {row['local']} local  "
                f"{row['pending']} pending"
            )

    render(ctx_obj, "stage", data, human_fn=_human)
