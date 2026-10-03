from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
from setforge.cli._output import (
    OutputContext,
    OutputFormat,
    make_console,
    render,
    wrap_json,
)
from setforge.compare import expand_tracked_file, resolve_dst, resolve_src
from setforge.config import (
    Config,
    GeneratedContent,
    ResolvedProfile,
    load_config,
    resolve_effective_profile,
)
from setforge.errors import StructuredParseError
from setforge.generated import rendered_source
from setforge.locking import profile_lock
from setforge.reconcile import store as reconcile_store
from setforge.reconcile import structured_units as su_mod
from setforge.reconcile.index_model import FileEntry
from setforge.reconcile.merge import merge
from setforge.reconcile.merge_model import Conflict, MergeResult
from setforge.reconcile.types import ABSENT, Absent, FileId, HunkClass, file_id
from setforge.ui import theme
from setforge.ui.diffview import (
    RichLayout,
    three_way_segments,
    to_rich,
    two_way_lines,
)

if TYPE_CHECKING:
    from setforge.cli.stage import StageSummary

_WIDE_THRESHOLD = 120


_Match = tuple[FileId, Path, Path, GeneratedContent | None]


def _resolve_fid(
    cfg: Config, resolved: ResolvedProfile, repo_root: Path, arg: str
) -> list[_Match]:
    """Return every tracked file ``arg`` names, from the most specific rule.

    An id wins over a path, a path (``~`` and relative forms resolved) wins
    over a bare file name; more than one result means the argument is
    ambiguous.
    """
    candidate = Path(arg).expanduser().resolve()
    by_id: list[_Match] = []
    by_path: list[_Match] = []
    by_name: list[_Match] = []
    for name in resolved.tracked_files:
        tracked_file = cfg.tracked_files[name]
        src = resolve_src(tracked_file, repo_root)
        dst = resolve_dst(tracked_file)
        for sub_name, sub_src, sub_dst in expand_tracked_file(name, src, dst):
            match = (file_id(sub_name), sub_dst, sub_src, tracked_file.generated)
            if arg in (name, sub_name):
                by_id.append(match)
            elif candidate == sub_dst.resolve():
                by_path.append(match)
            elif arg == sub_dst.name:
                by_name.append(match)
    return by_id or by_path or by_name


def _unparseable_reason(
    live: bytes | Absent, upstream: bytes | Absent, dst: Path
) -> str | None:
    fmt = su_mod.structured_format(dst)
    if fmt is None:
        return None
    for label, data in (("live", live), ("tracked", upstream)):
        if isinstance(data, bytes) and data:
            try:
                su_mod.extract_structured_units(data, data, fmt)
            except StructuredParseError as exc:
                return f"{label} file is not parseable: {exc}"
    return None


def _single_match(
    ctx_obj: OutputContext | None, matches: list[_Match], file: str, profile: str
) -> _Match:
    if len(matches) == 1:
        return matches[0]
    if matches:
        listing = ", ".join(str(m[1]) for m in matches)
        message = (
            f"{file}: matches {len(matches)} tracked files ({listing}); "
            "pass the tracked-file name or the full path"
        )
    else:
        message = (
            f"{file}: not a tracked file in profile {profile!r} "
            f"(run `setforge compare --profile={profile}` to list tracked files)"
        )
    _emit_error(ctx_obj, message)
    raise typer.Exit(code=2)


def _merge_status(
    parse_problem: str | None, base_present: bool, clean: bool | None
) -> str:
    if parse_problem:
        return parse_problem
    if not base_present:
        return "no recorded merge base"
    return "merge clean" if clean else "merge conflicts"


def _pane_text(data: bytes | None | Absent) -> str | None:
    if data is None or data is ABSENT:
        return None
    if b"\x00" in data:
        return f"binary — {len(data)} bytes, cannot display"
    return data.decode("utf-8", errors="replace")


def _merge_pane_text(result: MergeResult) -> str:
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


def _index_summary(
    result: MergeResult, entry: FileEntry | None
) -> dict[str, list[dict[str, Any]]]:
    # shared/kept_local come from the STORE index, not merge conflicts: a
    # kept-local hunk re-merges CLEAN, so a conflict-only walk would miss it.
    shared: list[dict[str, Any]] = []
    kept_local: list[dict[str, Any]] = []
    for row in entry.hunks if entry is not None else []:
        cls = row.get("cls")
        if cls == HunkClass.SHARED.value:
            shared.append({"label": row.get("label"), "tag": "shared"})
        elif cls == HunkClass.LOCAL.value:
            kept_local.append({"label": row.get("label"), "tag": "kept_local"})

    conflict: list[dict[str, Any]] = []
    line = 1
    for seg in result.segments:
        if isinstance(seg, Conflict):
            span = line + seg.ours.count(b"\n") + seg.theirs.count(b"\n")
            conflict.append({"start": line, "end": span, "tag": "conflict"})
            line = span
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
    """Show the reconcile state (base / live / merge) for one tracked file.

    Takes a tracked-file name or a live path and renders its three-way diff
    view for the profile, so a drifted file's divergence can be inspected
    before an install or sync resolves it.
    """
    from setforge.cli.stage import (
        collect_stages,
        collect_structured_stages,
        summarize_stages,
    )

    config = _resolve_config_arg(config)
    cfg = load_config(config)
    repo_root = config.resolve().parent
    resolved = resolve_effective_profile(cfg, profile, repo_root).resolved

    matches = _resolve_fid(cfg, resolved, repo_root, file)
    fid, dst, src, generated = _single_match(ctx.obj, matches, file, profile)

    staging_rows = summarize_stages(
        collect_stages(cfg, resolved, repo_root, profile, only=str(dst)),
        collect_structured_stages(cfg, resolved, repo_root, profile, only=str(dst)),
    )
    with profile_lock(profile):
        base = reconcile_store.read_base(profile, fid)
        recorded = reconcile_store.read_local(profile, fid)
        entry = reconcile_store.read_index(profile).files.get(str(fid))
    staging = staging_rows[0] if staging_rows else None

    # Absent-live falls back to recorded-local; matched on ABSENT, not truthiness.
    if dst.exists():
        live: bytes | Absent = dst.read_bytes()
    elif isinstance(recorded, bytes):
        live = recorded
    else:
        live = ABSENT
    upstream: bytes | Absent = src.read_bytes() if src.exists() else ABSENT
    if upstream is not ABSENT and generated is not None:
        upstream = rendered_source(src, generated).encode("utf-8")

    base_present = base is not None
    if base is not None:
        result = merge(base, live, upstream)
        merge_pane = _merge_pane_text(result)
        index = _index_summary(result, entry)
        model = three_way_segments(result)
    else:
        live_bytes = b"" if live is ABSENT else live
        up_bytes = b"" if upstream is ABSENT else upstream
        model = two_way_lines(live_bytes, up_bytes)
        merge_pane = _pane_text(upstream) or ""
        index = {"shared": [], "kept_local": [], "conflict": []}

    parse_problem = _unparseable_reason(live, upstream, dst)
    result_clean = result.clean if base_present else None
    data: dict[str, Any] = {
        "file": str(dst),
        "base_present": base_present,
        "panes": {
            "base": _pane_text(base),
            "live": _pane_text(live),
            "merge": merge_pane,
        },
        "index": index,
        "staging": staging.to_dict() if staging is not None else None,
        "errors": [parse_problem] if parse_problem else [],
    }

    def _human() -> None:
        console = make_console()
        layout = (
            RichLayout.SIDE_BY_SIDE
            if console.width >= _WIDE_THRESHOLD
            else RichLayout.STACKED
        )
        merge_status = _merge_status(parse_problem, base_present, result_clean)
        header = theme.styled(
            f"inspect {dst}  ({merge_status})",
            theme.Role.HEADING,
            stream=console.file,
        )
        console.print(header, markup=False, highlight=False)
        body = to_rich(model, layout=layout)
        if console.is_terminal:
            body = Panel(body, title="base | live | merge")
        console.print(body)
        if not parse_problem:
            _render_index(console, index)
        _render_staging(console, staging)

    render(ctx.obj, "inspect", data, human_fn=_human)


def _render_index(console: Console, index: dict[str, list[dict[str, Any]]]) -> None:
    rows = index["conflict"]
    if not rows:
        console.print(
            theme.styled(
                "no conflicts — merge is clean", theme.Role.SUCCESS, stream=console.file
            ),
            markup=False,
            highlight=False,
        )
        return
    console.print(
        theme.styled("hunk index:", theme.Role.HEADING, stream=console.file),
        markup=False,
        highlight=False,
    )
    for row in rows:
        console.print(f"  lines {row['start']}-{row['end']}  [conflict]", markup=False)


def _render_staging(console: Console, staging: StageSummary | None) -> None:
    if staging is None:
        console.print("staging classifications unavailable for this file")
        return
    console.print(
        f"staging: {staging.shared_promotable} shared-promotable  "
        f"{staging.drafted} drafted  {staging.reconfirm_required} "
        f"reconfirm-required  {staging.local} local  {staging.pending} pending"
    )
    for blocker in staging.blockers:
        console.print(f"  blocked: {blocker}")


def _emit_error(ctx_obj: OutputContext | None, message: str) -> None:
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
        "staging": None,
        "errors": [],
    }
