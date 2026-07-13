"""inspect subcommand — themed 3-way viewer (base | live | merge-preview) (A7).

``setforge inspect <file>`` renders a tracked file's reconcile state as a
Tokyo-Night-themed three-way view — the merge base, the live bytes, and the
merge-preview (the resolved merged text, conflict regions marked) — plus a
per-hunk index (line-range + shared / kept-local / conflict tag). It is a
one-shot batch render (no Live loop); the layout is responsive, going
3-column when the console is wide enough and stacked otherwise.

Read-only: it reuses ``compare``'s file resolver + the store's confinement
guard, reads base / local / index under one ``profile_lock``, and computes
the 3-way merge via :func:`setforge.reconcile.merge.merge`. An untracked /
unresolvable file mirrors ``compare``'s exit-2 (a structured error rides the
JSON envelope's ``errors`` list, never a bare human string in JSON mode).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel

from setforge.cli import (
    _CONFIG_OPTION,
    _PROFILE_OPTION,
    _resolve_config_arg,
    app,
)
from setforge.cli._help_examples import INSPECT_EXAMPLES
from setforge.cli._output import OutputContext, OutputFormat, render, wrap_json
from setforge.compare import expand_tracked_file, resolve_dst, resolve_src
from setforge.config import Config, ResolvedProfile, load_config, resolve_profile
from setforge.locking import profile_lock
from setforge.reconcile import store as reconcile_store
from setforge.reconcile.merge import merge
from setforge.reconcile.merge_model import Conflict, MergeResult
from setforge.reconcile.types import ABSENT, Absent, FileId, file_id
from setforge.ui import theme
from setforge.ui.diffview import (
    RichLayout,
    three_way_segments,
    to_rich,
    two_way_lines,
)

# Below this console width the 3-column base|live|merge layout is unreadable, so
# the panes stack. ``Console`` reports 80 for a non-tty stream, so a piped /
# CliRunner invocation deterministically resolves to STACKED.
_WIDE_THRESHOLD = 120


def _resolve_fid(
    cfg: Config, resolved: ResolvedProfile, repo_root: Path, arg: str
) -> tuple[FileId, Path] | None:
    """Resolve ``arg`` to a ``(file-id, live-path)`` for a tracked file.

    Matches ``arg`` against each expanded tracked file's name / sub-name /
    live path / live basename — the same match set ``stage`` uses — so a
    directory member (``name/relpath``) resolves too. Returns ``None`` when
    nothing matches (the caller maps that to exit 2).
    """
    for name in resolved.tracked_files:
        tracked_file = cfg.tracked_files[name]
        src = resolve_src(tracked_file, repo_root)
        dst = resolve_dst(tracked_file)
        for sub_name, _sub_src, sub_dst in expand_tracked_file(name, src, dst):
            if arg in (name, sub_name, str(sub_dst), sub_dst.name):
                return file_id(sub_name), sub_dst
    return None


def _pane_text(data: bytes | None | Absent) -> str | None:
    """Decode a pane's bytes for the JSON payload; ``None`` when the side is absent.

    Binary / non-UTF8 degrades to a stat placeholder rather than crashing — the
    same contract the Rich renderer honours. ``None`` / :data:`ABSENT` both mean
    "no such pane" (base-absent 2-pane view, or a clean deletion).
    """
    if data is None or data is ABSENT:
        return None
    if b"\x00" in data:
        return f"binary — {len(data)} bytes, cannot display"
    return data.decode("utf-8", errors="replace")


def _merge_pane_text(result: MergeResult) -> str:
    """The merge-preview pane body: resolved bytes when clean, conflict-marked
    otherwise.

    A clean result renders its merged bytes (or a deletion note for a clean
    absence); a conflicted result stitches each conflict's ours/theirs between
    ``<<<<<<<`` / ``=======`` / ``>>>>>>>`` markers so the preview shows exactly
    what still needs resolving. Binary sides degrade to a stat placeholder.
    """
    if result.clean:
        merged = result.merged()
        if merged is ABSENT:
            return "(file resolves to absent — clean deletion)"
        text = _pane_text(merged)
        return text if text is not None else ""
    parts: list[str] = []
    for seg in result.segments:
        if isinstance(seg, Conflict):
            parts.append("<<<<<<< OURS (this host)\n")
            parts.append(_pane_text(seg.ours) or "")
            parts.append("=======\n")
            parts.append(_pane_text(seg.theirs) or "")
            parts.append(">>>>>>> THEIRS (upstream)\n")
        else:
            parts.append(_pane_text(seg.bytes_) or "")
    return "".join(parts)


def _index_summary(result: MergeResult) -> dict[str, list[dict[str, Any]]]:
    """Per-hunk index: each conflict segment tagged, walked in document order.

    In this render the merge stream carries clean runs and conflict regions; a
    conflict becomes a ``conflict`` entry, and each side's line count feeds a
    ``kept_local`` (ours) / ``shared`` (theirs) tally. The line-range is the
    conflict's position in the merged-preview line stream.
    """
    shared: list[dict[str, Any]] = []
    kept_local: list[dict[str, Any]] = []
    conflict: list[dict[str, Any]] = []
    line = 1
    for seg in result.segments:
        if isinstance(seg, Conflict):
            ours = seg.ours.count(b"\n")
            theirs = seg.theirs.count(b"\n")
            span = {"start": line, "end": line + ours + theirs}
            conflict.append({**span, "tag": "conflict"})
            if ours:
                kept_local.append({**span, "tag": "kept_local", "lines": ours})
            if theirs:
                shared.append({**span, "tag": "shared", "lines": theirs})
            line += ours + theirs
        else:
            line += seg.bytes_.count(b"\n")
    return {"shared": shared, "kept_local": kept_local, "conflict": conflict}


@app.command(epilog=INSPECT_EXAMPLES)
def inspect(
    ctx: typer.Context,
    file: str = typer.Argument(
        ..., help="Tracked file to inspect (name or live path)."
    ),
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
) -> None:
    """Show a tracked file's base | live | merge-preview 3-way view + hunk index."""
    config = _resolve_config_arg(config)
    cfg = load_config(config)
    repo_root = config.resolve().parent
    resolved = resolve_profile(cfg, profile)

    match = _resolve_fid(cfg, resolved, repo_root, file)
    if match is None:
        _emit_error(
            ctx.obj,
            f"{file}: not a tracked file in profile {profile!r} "
            f"(run `setforge compare --profile={profile}` to list tracked files)",
        )
        raise typer.Exit(code=2)
    fid, dst = match

    with profile_lock(profile):
        base = reconcile_store.read_base(profile, fid)
        recorded = reconcile_store.read_local(profile, fid)

    # "ours" is the current live file on disk (the state the user sees). When the
    # file is absent live, fall back to the recorded-local trichotomy
    # (bytes | None | ABSENT): a recorded ``b""`` (empty) is its own bytes value,
    # while both None (nothing recorded) and ABSENT collapse to "the file is
    # absent" — matched on the sentinel, never truthiness.
    if dst.exists():
        live: bytes | Absent = dst.read_bytes()
    elif isinstance(recorded, bytes):
        live = recorded
    else:
        live = ABSENT  # recorded is None or ABSENT → absent
    upstream = _tracked_bytes(cfg, resolved, repo_root, fid)

    base_present = base is not None
    if base is not None:
        result = merge(base, live, upstream)
        merge_pane = _merge_pane_text(result)
        index = _index_summary(result)
        model = three_way_segments(result)
    else:
        # No recorded base → 2-pane live↔upstream diff (no merge to compute).
        live_bytes = b"" if live is ABSENT else live
        up_bytes = b"" if upstream is ABSENT else upstream
        model = two_way_lines(live_bytes, up_bytes)
        merge_pane = _pane_text(upstream) or ""
        index = {"shared": [], "kept_local": [], "conflict": []}

    data: dict[str, Any] = {
        "file": str(dst),
        "base_present": base_present,
        "panes": {
            "base": _pane_text(base),
            "live": _pane_text(live),
            "merge": merge_pane,
        },
        "index": index,
        "errors": [],
    }

    def _human() -> None:
        console = Console()
        layout = (
            RichLayout.SIDE_BY_SIDE
            if console.width >= _WIDE_THRESHOLD
            else RichLayout.STACKED
        )
        header = theme.styled(
            f"inspect {dst}  ({model.summary})", theme.Role.HEADING, stream=console.file
        )
        console.print(header, markup=False)
        console.print(Panel(to_rich(model, layout=layout), title="base | live | merge"))
        _render_index(console, index)

    render(ctx.obj, "inspect", data, human_fn=_human)


def _tracked_bytes(
    cfg: Config, resolved: ResolvedProfile, repo_root: Path, fid: FileId
) -> bytes | Absent:
    """The tracked (upstream) bytes for ``fid`` — the merge ``theirs`` side.

    Re-walks the profile's expanded tracked files to find the src whose sub-name
    matches ``fid``; :data:`ABSENT` when the tracked src is missing (an upstream
    deletion). Mirrors the src resolution ``stage`` / ``compare`` use.
    """
    for name in resolved.tracked_files:
        tracked_file = cfg.tracked_files[name]
        src = resolve_src(tracked_file, repo_root)
        dst = resolve_dst(tracked_file)
        for sub_name, sub_src, _sub_dst in expand_tracked_file(name, src, dst):
            if sub_name == str(fid):
                return sub_src.read_bytes() if sub_src.exists() else ABSENT
    return ABSENT


def _render_index(console: Console, index: dict[str, list[dict[str, Any]]]) -> None:
    """Print the per-hunk index summary (shared / kept-local / conflict tags)."""
    rows = index["conflict"]
    if not rows:
        console.print(
            theme.styled(
                "no conflicts — merge is clean", theme.Role.SUCCESS, stream=console.file
            ),
            markup=False,
        )
        return
    console.print(
        theme.styled("hunk index:", theme.Role.HEADING, stream=console.file),
        markup=False,
    )
    for row in rows:
        console.print(f"  lines {row['start']}-{row['end']}  [conflict]", markup=False)


def _emit_error(ctx_obj: OutputContext | None, message: str) -> None:
    """Surface a resolution error on the right surface for the output mode.

    JSON mode writes the versioned envelope with a top-level ``errors`` list
    (via :func:`wrap_json`'s ``errors`` arg) so a ``| jq`` pipeline sees
    structured output, never a bare stderr string. Human mode prints a red
    error to stderr, mirroring ``compare``'s failure UX. Since the JSON body
    is written directly here (not through :func:`render`), no ``Console`` is
    ever constructed on the error path.
    """
    if ctx_obj is not None and ctx_obj.format is OutputFormat.JSON:
        sys.stdout.write(wrap_json("inspect", _empty_data(), errors=[message]))
        sys.stdout.write("\n")
        return
    typer.secho(f"error: {message}", err=True, fg=typer.colors.RED)


def _empty_data() -> dict[str, Any]:
    return {
        "file": None,
        "base_present": False,
        "panes": {"base": None, "live": None, "merge": None},
        "index": {"shared": [], "kept_local": [], "conflict": []},
    }
