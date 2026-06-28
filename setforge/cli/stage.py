"""stage subcommand — per-hunk share/keep classification for plain files (A5).

``setforge stage <file>`` walks each base↔live diff hunk of a plain tracked
file and lets the host classify it SHARED (promote into the shared config on the
next ``sync``) or LOCAL (keep host-only). ``setforge stage --list`` is a
read-only per-file count of SHARED / LOCAL / PENDING hunks — it writes nothing.

The classifications are persisted into the reconcile index; the actual promotion
into ``tracked/`` happens on ``sync`` (see :func:`setforge.capture.capture_profile`).
"""

from __future__ import annotations

import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

import typer
from prompt_toolkit.styles import Style
from rich.console import Console

from setforge.cli import (
    _CONFIG_OPTION,
    _PROFILE_OPTION,
    _resolve_config_arg,
    app,
)
from setforge.cli._help_examples import STAGE_EXAMPLES
from setforge.cli._output import render
from setforge.compare import expand_tracked_file, resolve_dst, resolve_src
from setforge.config import Config, ResolvedProfile, load_config, resolve_profile
from setforge.locking import profile_lock
from setforge.reconcile import hunks as hunks_mod
from setforge.reconcile import store as reconcile_store
from setforge.reconcile.hunks import Hunk
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

#: A per-hunk choose callback: ``(hunk, index, total) -> class | None | QUIT``
#: (``None`` = skip/leave class unchanged).
Choice = Callable[[Hunk, int, int], "HunkClass | None | _Quit"]


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


def walk(
    hunks: list[Hunk],
    choose: Choice,
) -> list[Hunk]:
    """Apply one classification choice per hunk.

    ``choose(hunk, index, total)`` returns a :class:`HunkClass` to (re)classify,
    ``None`` to leave the hunk's class unchanged (skip / next), or :data:`QUIT`
    to stop early. Choices made before a QUIT are kept.
    """
    out = list(hunks)
    for index, hunk in enumerate(hunks):
        decision = choose(hunk, index, len(hunks))
        if isinstance(decision, _Quit):
            break
        if decision is not None:
            out[index] = replace(hunk, cls=decision)
    return out


def _hunk_preview(stage: FileStage, hunk: Hunk) -> str:
    """A small ±diff preview of one hunk for the button-bar body."""
    base_lines = hunk_lines(stage.base)
    live_lines = hunk_lines(stage.live)
    i1, i2 = hunk.base_span
    j1, j2 = hunk.live_span
    removed = [b"- " + line for line in base_lines[i1:i2]]
    added = [b"+ " + line for line in live_lines[j1:j2]]
    body = b"".join(removed + added).decode("utf-8", "replace")
    return body if len(body) <= 600 else body[:599] + "…"


def hunk_lines(data: bytes) -> list[bytes]:
    """Split ``data`` into terminator-keeping lines (engine line model)."""
    return hunks_mod._split_lines(data)


def _interactive_choice(stage: FileStage) -> Choice:
    """A button-bar-backed choose callback for the interactive walk."""
    style = Style.from_dict(pt_style(THEME))

    def choose(hunk: Hunk, index: int, total: int) -> HunkClass | None | _Quit:
        flag = " (changed)" if hunk.changed else ""
        current = f"currently {hunk.cls.value}"
        result = button_bar(
            [
                Button("Share", HunkClass.SHARED),
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
        if result is CANCEL:
            return QUIT  # Esc / Ctrl-C stops the walk, keeping prior choices
        return result

    return choose


def _persist(profile: str, stage: FileStage, hunks: list[Hunk]) -> None:
    """Record the updated classifications. base is UNCHANGED (sync/install own it)."""
    with profile_lock(profile):
        reconcile_store.record(
            profile,
            stage.fid,
            base=stage.base,
            local=stage.live,
            hunks=hunks_mod.serialize(hunks),
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
        updated = walk(stage_item.hunks, _interactive_choice(stage_item))
        _persist(profile, stage_item, updated)
        tally = counts(updated)
        console.print(
            f"{stage_item.sub_name}: "
            f"{tally[HunkClass.SHARED]} shared  "
            f"{tally[HunkClass.LOCAL]} local  "
            f"{tally[HunkClass.PENDING]} pending"
        )


def _render_list(ctx_obj: object, stages: list[FileStage]) -> None:
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

    render(ctx_obj, "stage", data, human_fn=_human)  # type: ignore[arg-type]
