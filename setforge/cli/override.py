"""``override`` subcommand group — the disposition + span front door.

The user-facing surface over the stored-base disposition model
(:class:`setforge.config.Disposition`) and the sub-file span model
(:class:`setforge.spans.SpanEntry`). Seven verbs:

- ``fork`` / ``pin`` ``<file> [anchor] [--shared]`` — without an anchor,
  set the tracked_file's *file-level* disposition; with an anchor, append a
  *span* (markdown heading-text anchor OR structural dotted path). The
  default scope is host-local (``~/.config/setforge/local.yaml`` via a
  ruamel round-trip); ``--shared`` writes the resolved config-repo
  ``setforge.yaml`` atomically and prints the commit/push hint.
- ``unpin`` / ``unfork`` ``<file> [anchor] [--shared]`` — the inverse of
  ``pin`` / ``fork``: remove a file-level disposition or a span. Each is
  *kind-specific* — ``unpin`` only removes a PINNED override, ``unfork`` only
  a FORKED one, so a forked file holding a pinned span is never disturbed by
  the wrong verb. A wrong-kind target is left intact with a warning; an absent
  target is a byte-no-op (exit 0). ``reset <file> [--shared]`` clears ALL
  override state (file-level disposition + every span) for a tracked_file,
  leaving its ``host_local_sections`` / ``preserve_*`` blocks untouched. All
  three are pure ``local.yaml`` / ``setforge.yaml`` edits (no stored-base or
  sidecar mutation — the engine self-heals on the next install).
- ``list`` — reuse :func:`setforge.compare.compare_profile` to render each
  tracked_file's disposition + span state (markdown AND structural).
- ``show <file> [--spans]`` — a span summary table (with an ``ORPHANED``
  column) plus a synthesized annotated body whose virtual span comments are
  injected at render time, to STDOUT ONLY (the file byte-stays unchanged).

Every span / disposition mutation is guarded before any write: a per-
file-type anchor validator, a refusal on legacy-``preserve_*`` files
(disposition / spans are mutually exclusive with the preserve model), a
user-section-marker overlap refusal, idempotency, a pin-over-fork upgrade /
fork-over-pin downgrade rule, and the structural non-overlap / nesting check.
The ``--shared`` write rides a five-part discipline: an atomic round-trip
write (never a torn version-controlled file), a clean-check covering the
``setforge.yaml`` root (not just ``tracked/``), the post-write commit/push
hint (the span is otherwise lost on the next config-repo pull), source-layer
target resolution (never a hard-coded / CWD path), and a symlink refusal (no
silent link replacement).
"""

from __future__ import annotations

import io
from enum import StrEnum
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from ruamel.yaml import YAML

from setforge import compare as compare_mod
from setforge import disposition_merge, transitions
from setforge import source as source_mod
from setforge.atomicio import atomic_write_text
from setforge.cli import (
    _CONFIG_OPTION,
    _PROFILE_OPTION,
    _resolve_config_arg,
    app,
)
from setforge.compare import (
    CompareStatus,
    load_ignored_orphans,
    resolve_src,
)
from setforge.config import (
    Disposition,
    TrackedFile,
    apply_host_local_tracked_file_overrides,
    load_config,
    resolve_profile,
)
from setforge.errors import (
    AnchorAmbiguousError,
    AnchorNotFoundError,
    ConfigError,
    SetforgeError,
)
from setforge.markdown_spans import bound_span
from setforge.scalar_merge import ABSENT
from setforge.sections import _MARKER_RE
from setforge.source import PathSource, Source, SpanEntry
from setforge.spans import (
    SpanKind,
    SpanSemantics,
    is_heading_anchor,
    validate_spans_file_type,
)
from setforge.structural_merge import get_at_path

__all__ = ["override_app"]

override_app: typer.Typer = typer.Typer(
    help="Manage tracked-file disposition + sub-file span overrides.",
    no_args_is_help=True,
    rich_markup_mode=None,
)
app.add_typer(override_app, name="override")

_RT_YAML = YAML(typ="rt")

# A pin verb maps to PINNED, a fork verb to FORKED, at both file-level and
# span granularity. A pin "upgrades" a fork; a fork must NOT silently
# "downgrade" a pin (enforced by the transition guards below).
_DISPOSITION_OF_KIND: dict[SpanKind, Disposition] = {
    SpanKind.FORKED: Disposition.FORKED,
    SpanKind.PINNED: Disposition.PINNED,
}


# ---------------------------------------------------------------------------
# Shared parse-time validation + target resolution.
# ---------------------------------------------------------------------------


def _tracked_file_or_fail(cfg_path: Path, file_id: str) -> tuple[TrackedFile, Path]:
    """Resolve ``file_id`` to its :class:`TrackedFile` + absolute tracked src.

    Raises :class:`typer.BadParameter` for an unknown id so the user gets a
    clean error rather than a ``KeyError`` traceback.
    """
    cfg = load_config(cfg_path)
    if file_id not in cfg.tracked_files:
        known = ", ".join(sorted(cfg.tracked_files)) or "(none)"
        raise typer.BadParameter(
            f"unknown tracked_file {file_id!r}; known ids: {known}"
        )
    tracked_file = cfg.tracked_files[file_id]
    repo_root = cfg_path.resolve().parent
    src = resolve_src(tracked_file, repo_root)
    return tracked_file, src


def _refuse_legacy_preserve(tracked_file: TrackedFile, file_id: str) -> None:
    """Refuse pin/fork on a legacy ``preserve_*`` file (early BadParameter).

    Disposition / spans are mutually exclusive with the legacy preserve
    model. This surfaces that same mutual-exclusion the config model enforces,
    but at parse time on the CLI rather than as a deferred install-time
    :class:`pydantic.ValidationError`.
    """
    offenders: list[str] = []
    if tracked_file.preserve_user_sections:
        offenders.append("preserve_user_sections")
    if tracked_file.preserve_user_keys:
        offenders.append("preserve_user_keys")
    if tracked_file.preserve_user_keys_deep:
        offenders.append("preserve_user_keys_deep")
    if offenders:
        raise typer.BadParameter(
            f"tracked_file {file_id!r} uses the legacy preserve model "
            f"({sorted(offenders)}); disposition / spans are mutually "
            "exclusive with preserve_*. Drop the preserve_* field first."
        )


def _validate_anchor_for_file(anchor: str, src: Path, file_id: str) -> None:
    """Reject an anchor whose grammar is wrong for ``src``'s file type.

    Wraps :func:`setforge.spans.validate_spans_file_type` so the file-type
    dispatch (heading -> markdown, dotted -> structural) surfaces as an
    early :class:`typer.BadParameter` rather than a deferred
    :class:`ConfigError` at install / validate time.
    """
    probe = SpanEntry(anchor=anchor, kind=SpanKind.PINNED)
    try:
        validate_spans_file_type(file_id, [probe], src)
    except ConfigError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _refuse_span_user_section_overlap(anchor: str, src: Path) -> None:
    """Refuse a markdown span whose region overlaps a user-section marker.

    Resolves the heading span's half-open line range and refuses when any
    ``setforge:user-section`` marker line falls inside it — a span and a
    user-section must not contend over the same bytes. Structural files have
    no markers, so this is a no-op for them. An anchor that does not resolve
    is left to the resolution guard, not this one.
    """
    if not is_heading_anchor(anchor):
        return
    if src.suffix.lower() not in {".md", ".markdown"}:
        return
    try:
        text = src.read_text(encoding="utf-8")
    except OSError:
        return
    try:
        span = bound_span(text, anchor)
    except (AnchorNotFoundError, AnchorAmbiguousError):
        return
    for idx, line in enumerate(text.splitlines()):
        if span.start_line <= idx < span.end_line and _MARKER_RE.match(line):
            raise typer.BadParameter(
                f"span anchor {anchor!r} overlaps a setforge:user-section "
                f"marker at line {idx + 1}; a span and a user-section cannot "
                "contend over the same region."
            )


def _verify_anchor_resolves(anchor: str, src: Path, file_id: str) -> None:
    """Refuse an anchor that does not resolve in the current tracked source.

    Markdown headings resolve via :func:`bound_span`; a structural dotted
    path resolves via :func:`get_at_path` against the parsed tracked model.
    Surfacing the miss at pin time (rather than as a deferred install
    orphan-warn) keeps the user-facing CLI honest about what it just pinned.
    """
    try:
        text = src.read_text(encoding="utf-8")
    except OSError as exc:
        raise typer.BadParameter(
            f"cannot read tracked source {src} for {file_id!r}: {exc}"
        ) from exc
    if is_heading_anchor(anchor):
        try:
            bound_span(text, anchor)
        except (AnchorNotFoundError, AnchorAmbiguousError) as exc:
            raise typer.BadParameter(str(exc)) from exc
        return
    # Structural dotted path: parse the tracked model and probe the leaf.
    model = _load_structural_model(src, text)
    if get_at_path(model, anchor) is ABSENT:
        raise typer.BadParameter(
            f"structural anchor {anchor!r} does not resolve in {src}."
        )


def _load_structural_model(src: Path, text: str) -> object:
    """Parse ``text`` into the structural (comment-tree) model for ``src``."""
    from setforge import jsonc
    from setforge.disposition_merge import _load_structural

    return _load_structural(text, jsonc.is_jsonc_file(src))


# ---------------------------------------------------------------------------
# Host-local (local.yaml) round-trip writes.
# ---------------------------------------------------------------------------


def _local_config_path() -> Path:
    """Return the live ``local.yaml`` path (read through the source module).

    Looked up via the module attribute (not a load-time-bound import) so the
    test suite's :func:`conftest._isolated_local_config` redirect of
    ``setforge.source.LOCAL_CONFIG_PATH`` takes effect — the same discipline
    :mod:`setforge.cli.orphans` follows.
    """
    return source_mod.LOCAL_CONFIG_PATH


def _load_local_data() -> dict[str, object]:
    """Load ``local.yaml`` as a round-trip mapping (empty dict when absent)."""
    path = _local_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _RT_YAML.load(path.read_text(encoding="utf-8")) if path.exists() else None
    if not isinstance(data, dict):
        return {}
    return data


def _dump_local_data(data: dict[str, object]) -> None:
    """Write ``data`` back to ``local.yaml`` via the ruamel round-trip."""
    with _local_config_path().open("w", encoding="utf-8") as fh:
        _RT_YAML.dump(data, fh)


def _local_tf_block(data: dict[str, object], file_id: str) -> dict[str, object]:
    """Return the (created-if-absent) ``tracked_files.<id>`` overlay block."""
    tracked = data.get("tracked_files")
    if not isinstance(tracked, dict):
        tracked = {}
        data["tracked_files"] = tracked
    block = tracked.get(file_id)
    if not isinstance(block, dict):
        block = {}
        tracked[file_id] = block
    return block


# ---------------------------------------------------------------------------
# Disposition + span apply helpers (scope-dispatched).
# ---------------------------------------------------------------------------


def _current_disposition_host_local(file_id: str) -> str | None:
    block = _load_local_data().get("tracked_files", {})
    if isinstance(block, dict) and isinstance(block.get(file_id), dict):
        value = block[file_id].get("disposition")
        return str(value) if value is not None else None
    return None


def _current_disposition_shared(cfg_path: Path, file_id: str) -> str | None:
    data = _RT_YAML.load(cfg_path.read_text(encoding="utf-8"))
    entry = data.get("tracked_files", {}).get(file_id, {})
    value = entry.get("disposition") if isinstance(entry, dict) else None
    return str(value) if value is not None else None


def _guard_disposition_transition(
    current: str | None, target: SpanKind, console: Console
) -> bool:
    """Apply the idempotency + upgrade/downgrade rule for a file-level write.

    Returns ``True`` when the write should proceed, ``False`` when it is a
    no-op (already at the target). Raises :class:`typer.BadParameter` on a
    fork-over-pin downgrade.
    """
    target_value = _DISPOSITION_OF_KIND[target].value
    if current == target_value:
        console.print(f"already {target_value}")
        return False
    if current == Disposition.PINNED.value and target is SpanKind.FORKED:
        raise typer.BadParameter(
            "refusing to downgrade a pinned file to forked; re-issue "
            "explicitly via the removal verbs (out of scope this release)."
        )
    return True


def _guard_span_transition(
    existing: list[SpanEntry], anchor: str, target: SpanKind, console: Console
) -> list[SpanEntry] | None:
    """Apply idempotency + upgrade/downgrade for a span write on ``anchor``.

    Returns ``existing`` unchanged as a proceed-sentinel (the caller rebuilds
    the span list itself), or ``None`` when the write is a no-op (an identical
    span already exists). Raises :class:`typer.BadParameter` on a fork-over-pin
    downgrade.
    """
    by_anchor = {s.anchor: s for s in existing}
    prior = by_anchor.get(anchor)
    if prior is not None:
        if prior.kind is target:
            console.print(f"already {target.value} on anchor {anchor!r}")
            return None
        if prior.kind is SpanKind.PINNED and target is SpanKind.FORKED:
            raise typer.BadParameter(
                f"refusing to downgrade pinned span {anchor!r} to forked; "
                "re-issue explicitly via the removal verbs (out of scope)."
            )
    return existing


def _set_disposition_host_local(file_id: str, kind: SpanKind) -> None:
    data = _load_local_data()
    block = _local_tf_block(data, file_id)
    block["disposition"] = _DISPOSITION_OF_KIND[kind].value
    _dump_local_data(data)


def _append_span_host_local(file_id: str, span: SpanEntry) -> None:
    data = _load_local_data()
    block = _local_tf_block(data, file_id)
    raw = block.get("spans")
    spans: list[object] = list(raw) if isinstance(raw, list) else []
    spans = [
        s for s in spans if not (isinstance(s, dict) and s.get("anchor") == span.anchor)
    ]
    spans.append(span.model_dump(mode="json"))
    block["spans"] = spans
    _dump_local_data(data)


def _existing_spans_host_local(file_id: str) -> list[SpanEntry]:
    block = _load_local_data().get("tracked_files", {})
    if not (isinstance(block, dict) and isinstance(block.get(file_id), dict)):
        return []
    raw = block[file_id].get("spans")
    if not isinstance(raw, list):
        return []
    return [SpanEntry.model_validate(dict(s)) for s in raw]


def _existing_spans_shared(cfg_path: Path, file_id: str) -> list[SpanEntry]:
    data = _RT_YAML.load(cfg_path.read_text(encoding="utf-8"))
    entry = data.get("tracked_files", {}).get(file_id, {})
    raw = entry.get("spans") if isinstance(entry, dict) else None
    if not isinstance(raw, list):
        return []
    return [SpanEntry.model_validate(dict(s)) for s in raw]


# ---------------------------------------------------------------------------
# Shared (setforge.yaml) atomic round-trip write — discipline gates below.
# ---------------------------------------------------------------------------


def _source_for_config(cfg_path: Path) -> Source:
    """Build a :class:`Source` rooted at the resolved config's directory.

    The ``--shared`` write target is the path :func:`_resolve_config_arg`
    already produced (full ``--source`` / env / local.yaml / CWD precedence,
    never ``Path.cwd()``). The clean-check + post-write hint
    operate on THAT same directory, so we wrap its parent in a
    :class:`PathSource` rather than re-running the source layer (which would
    diverge from the actual write target, or raise ``NoSourceConfigured``
    when the user passed ``--config`` explicitly).
    """
    return PathSource(path=cfg_path.parent)


def _shared_write_preflight(cfg_path: Path) -> None:
    """Pre-write gates for a ``--shared`` setforge.yaml write.

    A symlinked ``setforge.yaml`` is refused outright (no silent link
    replace). A dirty ``setforge.yaml`` at the source root refuses, via the
    :func:`setforge.source.check_source_yaml_clean` extension.
    """
    if cfg_path.is_symlink():
        raise SetforgeError(
            f"{cfg_path} is a symlink; refusing to replace the link on a "
            "--shared write. Resolve the symlink or write through the real "
            "config repo (--source / --config the resolved target)."
        )
    source_mod.check_source_yaml_clean(_source_for_config(cfg_path))


def _shared_post_write_hint(cfg_path: Path) -> None:
    """Emit the commit/push hint after a successful ``--shared`` write.

    The shared span is otherwise silently lost on the next config-repo pull,
    so the hint reminds the user to commit + push. Emitted via
    :func:`typer.echo` (raw stdout, no rich word-wrapping) so the
    ``cd ... && git diff && git commit && git push`` shell command survives
    intact even on a narrow terminal — a wrapped command copies broken.
    """
    try:
        hint = source_mod.format_post_write_hint(
            _source_for_config(cfg_path), 1, subpath=cfg_path.name
        )
    except SetforgeError:
        return
    typer.echo(hint)


def _shared_apply(
    cfg_path: Path,
    file_id: str,
    *,
    disposition: SpanKind | None = None,
    span: SpanEntry | None = None,
) -> None:
    """Apply a disposition or span to ``setforge.yaml`` via an atomic round-trip.

    Loads the round-trip model (comment + key-order preserving), mutates the
    ``tracked_files.<id>`` entry, dumps to a string, and writes via
    :func:`atomicio.atomic_write_text` so the version-controlled file is
    never torn.
    """
    data = _RT_YAML.load(cfg_path.read_text(encoding="utf-8"))
    entry = data["tracked_files"][file_id]
    if disposition is not None:
        entry["disposition"] = _DISPOSITION_OF_KIND[disposition].value
    if span is not None:
        raw = entry.get("spans")
        spans: list[object] = list(raw) if isinstance(raw, list) else []
        spans = [
            s
            for s in spans
            if not (isinstance(s, dict) and s.get("anchor") == span.anchor)
        ]
        spans.append(span.model_dump(mode="json"))
        entry["spans"] = spans
    buffer = io.StringIO()
    _RT_YAML.dump(data, buffer)
    atomic_write_text(cfg_path, buffer.getvalue())


# ---------------------------------------------------------------------------
# The fork / pin verbs.
# ---------------------------------------------------------------------------


def _override_apply(
    kind: SpanKind,
    file_id: str,
    anchor: str | None,
    *,
    shared: bool,
    config: Path,
) -> None:
    """Shared body for ``fork`` / ``pin`` — file-level or span, host / shared."""
    console = Console()
    cfg_path = _resolve_config_arg(config)
    tracked_file, src = _tracked_file_or_fail(cfg_path, file_id)
    _refuse_legacy_preserve(tracked_file, file_id)
    semantics = SpanSemantics.SHARED if shared else SpanSemantics.HOST_LOCAL

    # SetforgeError covers the --shared write-discipline refusals
    # (DirtySourceCheckout, symlink-refuse). Surface them as a
    # clean error + exit 1 here — CliRunner invokes ``app`` directly, not the
    # ``main()`` wrapper that pretty-prints SetforgeError in production — so a
    # bare raise would otherwise propagate as an uncaught traceback.
    try:
        if shared:
            _shared_write_preflight(cfg_path)

        if anchor is None:
            _apply_file_level(
                kind, file_id, shared=shared, cfg_path=cfg_path, console=console
            )
        else:
            _apply_span(
                kind,
                file_id,
                anchor,
                src,
                semantics=semantics,
                shared=shared,
                cfg_path=cfg_path,
                console=console,
            )
    except SetforgeError as exc:
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc


def _apply_file_level(
    kind: SpanKind,
    file_id: str,
    *,
    shared: bool,
    cfg_path: Path,
    console: Console,
) -> None:
    """Set the file-level disposition for ``file_id`` (host-local or shared)."""
    current = (
        _current_disposition_shared(cfg_path, file_id)
        if shared
        else _current_disposition_host_local(file_id)
    )
    if not _guard_disposition_transition(current, kind, console):
        return
    if shared:
        _shared_apply(cfg_path, file_id, disposition=kind)
        _shared_post_write_hint(cfg_path)
    else:
        _set_disposition_host_local(file_id, kind)
    scope = "shared" if shared else "host-local"
    console.print(
        f"set {file_id} disposition={_DISPOSITION_OF_KIND[kind].value} ({scope})"
    )


def _apply_span(
    kind: SpanKind,
    file_id: str,
    anchor: str,
    src: Path,
    *,
    semantics: SpanSemantics,
    shared: bool,
    cfg_path: Path,
    console: Console,
) -> None:
    """Append a span on ``anchor`` for ``file_id`` (host-local or shared)."""
    _validate_anchor_for_file(anchor, src, file_id)
    _verify_anchor_resolves(anchor, src, file_id)
    _refuse_span_user_section_overlap(anchor, src)

    existing = (
        _existing_spans_shared(cfg_path, file_id)
        if shared
        else _existing_spans_host_local(file_id)
    )
    if _guard_span_transition(existing, anchor, kind, console) is None:
        return

    new_span = SpanEntry(anchor=anchor, kind=kind, semantics=semantics)
    # Validate the COMBINED span set (overlap / nesting) before writing.
    combined = [s for s in existing if s.anchor != anchor] + [new_span]
    _validate_combined_spans(combined, src)

    if shared:
        _shared_apply(cfg_path, file_id, span=new_span)
        _shared_post_write_hint(cfg_path)
    else:
        _append_span_host_local(file_id, new_span)
    scope = "shared" if shared else "host-local"
    console.print(f"added {kind.value} span {anchor!r} on {file_id} ({scope})")


def _validate_combined_spans(spans: list[SpanEntry], src: Path) -> None:
    """Run the structural overlap / nesting guard over the combined span set.

    Markdown spans are pairwise non-overlapping by heading bounding (the
    install merge enforces it); the dotted-path engine has no such guard, so
    :func:`setforge.disposition_merge.validate_structural_spans` (rejecting
    list-index pins and overlapping / illegally-nested dotted paths) runs over
    the structural set up front and surfaces as a :class:`typer.BadParameter`.
    """
    if not disposition_merge.is_structural(src):
        return
    try:
        disposition_merge.validate_structural_spans(spans)
    except ConfigError as exc:
        raise typer.BadParameter(str(exc)) from exc


@override_app.command("fork")
def override_fork(
    file_id: str = typer.Argument(..., metavar="FILE", help="Tracked_file id."),
    anchor: str | None = typer.Argument(
        None,
        help="Optional span anchor (markdown heading or structural dotted path).",
    ),
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    shared: bool = typer.Option(
        False,
        "--shared",
        help="Write the shared setforge.yaml (default: host-local local.yaml).",
    ),
) -> None:
    """Fork a tracked_file (or a span of it): merge upstream, never capture back."""
    _override_apply(SpanKind.FORKED, file_id, anchor, shared=shared, config=config)


@override_app.command("pin")
def override_pin(
    file_id: str = typer.Argument(..., metavar="FILE", help="Tracked_file id."),
    anchor: str | None = typer.Argument(
        None,
        help="Optional span anchor (markdown heading or structural dotted path).",
    ),
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    shared: bool = typer.Option(
        False,
        "--shared",
        help="Write the shared setforge.yaml (default: host-local local.yaml).",
    ),
) -> None:
    """Pin a tracked_file (or a span of it): live wins, never merged or captured."""
    _override_apply(SpanKind.PINNED, file_id, anchor, shared=shared, config=config)


# ---------------------------------------------------------------------------
# The unpin / unfork / reset verbs (override removal).
# ---------------------------------------------------------------------------


class _RemovalStatus(StrEnum):
    """Outcome of a single removal attempt against one tracked_file block."""

    REMOVED = "removed"
    ABSENT = "absent"
    WRONG_KIND = "wrong_kind"


def _reset_block(block: dict[str, object]) -> _RemovalStatus:
    """Drop ``disposition`` + ``spans`` outright (kind-agnostic, for reset)."""
    changed = False
    for key in ("disposition", "spans"):
        if key in block:
            del block[key]
            changed = True
    return _RemovalStatus.REMOVED if changed else _RemovalStatus.ABSENT


def _remove_disposition_in_block(
    block: dict[str, object], kind_value: str
) -> _RemovalStatus:
    """Drop the file-level ``disposition`` only when it matches ``kind_value``."""
    current = block.get("disposition")
    if current is None:
        return _RemovalStatus.ABSENT
    if str(current) != kind_value:
        return _RemovalStatus.WRONG_KIND
    del block["disposition"]
    return _RemovalStatus.REMOVED


def _remove_span_in_block(
    block: dict[str, object], anchor: str, kind_value: str
) -> _RemovalStatus:
    """Filter out every span matching ``anchor`` AND ``kind_value``.

    An anchor present only as the other kind is left + reported WRONG_KIND.
    Spans are rebuilt-and-reassigned (never ``del``/``pop`` on the ruamel
    seq, which orphans neighbour comments); an emptied ``spans`` key is
    removed so no ``spans: []`` residue lingers.
    """
    raw = block.get("spans")
    if not isinstance(raw, list):
        return _RemovalStatus.ABSENT
    matching = [s for s in raw if isinstance(s, dict) and s.get("anchor") == anchor]
    if not matching:
        return _RemovalStatus.ABSENT
    if all(s.get("kind") != kind_value for s in matching):
        return _RemovalStatus.WRONG_KIND
    new_spans = [
        s
        for s in raw
        if not (
            isinstance(s, dict)
            and s.get("anchor") == anchor
            and s.get("kind") == kind_value
        )
    ]
    if new_spans:
        block["spans"] = new_spans
    else:
        del block["spans"]
    return _RemovalStatus.REMOVED


def _apply_removal_to_block(
    block: dict[str, object],
    *,
    target_kind: SpanKind | None,
    anchor: str | None,
    reset: bool,
) -> _RemovalStatus:
    """Dispatch one removal against ``block`` (reset / file-level / span)."""
    if reset:
        return _reset_block(block)
    assert target_kind is not None  # only reset passes None
    if anchor is None:
        # File-level disposition stores the Disposition value; resolve through
        # _DISPOSITION_OF_KIND like the write path rather than assuming SpanKind
        # and Disposition share string values.
        return _remove_disposition_in_block(
            block, _DISPOSITION_OF_KIND[target_kind].value
        )
    # Spans store the SpanKind value verbatim.
    return _remove_span_in_block(block, anchor, target_kind.value)


def _remove_host_local(
    file_id: str, *, target_kind: SpanKind | None, anchor: str | None, reset: bool
) -> _RemovalStatus:
    """Apply a removal to ``local.yaml``; write only on a real change.

    Navigates read-only (no auto-create — unlike :func:`_local_tf_block`, the
    fork/pin write helper) so an absent target is a byte-no-op.
    """
    data = _load_local_data()
    tracked = data.get("tracked_files")
    if not isinstance(tracked, dict):
        return _RemovalStatus.ABSENT
    block = tracked.get(file_id)
    if not isinstance(block, dict):
        return _RemovalStatus.ABSENT
    status = _apply_removal_to_block(
        block, target_kind=target_kind, anchor=anchor, reset=reset
    )
    if status is _RemovalStatus.REMOVED:
        if not block:
            del tracked[file_id]
        _dump_local_data(data)
    return status


def _remove_shared(
    cfg_path: Path,
    file_id: str,
    *,
    target_kind: SpanKind | None,
    anchor: str | None,
    reset: bool,
) -> _RemovalStatus:
    """Apply a removal to ``setforge.yaml`` atomically; write only on a change.

    Navigates with ``.get`` (an absent ``tracked_files`` / ``<id>`` is a
    no-op, never a ``KeyError`` traceback). The write reuses
    :func:`_shared_apply`'s atomic round-trip + symlink/clean-tree preflight,
    but gates the preflight on an actual change — a no-op removal skips it,
    whereas the pin/fork path runs the preflight unconditionally on every
    ``--shared`` invocation.
    """
    data = _RT_YAML.load(cfg_path.read_text(encoding="utf-8"))
    tracked = data.get("tracked_files") if isinstance(data, dict) else None
    if not isinstance(tracked, dict):
        return _RemovalStatus.ABSENT
    block = tracked.get(file_id)
    if not isinstance(block, dict):
        return _RemovalStatus.ABSENT
    status = _apply_removal_to_block(
        block, target_kind=target_kind, anchor=anchor, reset=reset
    )
    if status is _RemovalStatus.REMOVED:
        _shared_write_preflight(cfg_path)
        if not block:
            del tracked[file_id]
        buffer = io.StringIO()
        _RT_YAML.dump(data, buffer)
        atomic_write_text(cfg_path, buffer.getvalue())
    return status


def _report_removal(
    status: _RemovalStatus,
    *,
    verb: str,
    file_id: str,
    anchor: str | None,
    target_kind: SpanKind | None,
    shared: bool,
    reset: bool,
    console: Console,
) -> None:
    """Print the outcome; a wrong-kind target warns (stderr) but exits 0."""
    scope = "shared" if shared else "host-local"
    target_desc = f"{file_id} {anchor!r}" if anchor is not None else file_id
    if status is _RemovalStatus.REMOVED:
        if reset:
            console.print(f"reset {file_id}: cleared disposition + spans ({scope})")
        else:
            assert target_kind is not None
            console.print(f"removed {target_kind.value} {target_desc} ({scope})")
        return
    if status is _RemovalStatus.WRONG_KIND:
        assert target_kind is not None
        other = "forked" if target_kind is SpanKind.PINNED else "pinned"
        other_verb = "unfork" if target_kind is SpanKind.PINNED else "unpin"
        typer.secho(
            f"{target_desc} is {other}, not {target_kind.value}; left unchanged "
            f"— use `{other_verb}` or `reset`.",
            err=True,
            fg=typer.colors.YELLOW,
        )
        return
    # ABSENT
    console.print(f"nothing to {verb} on {target_desc}")


def _override_remove(
    target_kind: SpanKind | None,
    file_id: str,
    anchor: str | None,
    *,
    reset: bool,
    shared: bool,
    config: Path,
) -> None:
    """Shared body for ``unpin`` / ``unfork`` / ``reset``."""
    console = Console()
    cfg_path = _resolve_config_arg(config)
    _tracked_file_or_fail(cfg_path, file_id)  # validate id (BadParameter on typo)
    verb = (
        "reset" if reset else ("unpin" if target_kind is SpanKind.PINNED else "unfork")
    )

    try:
        if shared:
            status = _remove_shared(
                cfg_path, file_id, target_kind=target_kind, anchor=anchor, reset=reset
            )
        else:
            status = _remove_host_local(
                file_id, target_kind=target_kind, anchor=anchor, reset=reset
            )
    except SetforgeError as exc:
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    _report_removal(
        status,
        verb=verb,
        file_id=file_id,
        anchor=anchor,
        target_kind=target_kind,
        shared=shared,
        reset=reset,
        console=console,
    )
    if shared and status is _RemovalStatus.REMOVED:
        _shared_post_write_hint(cfg_path)


@override_app.command("unpin")
def override_unpin(
    file_id: str = typer.Argument(..., metavar="FILE", help="Tracked_file id."),
    anchor: str | None = typer.Argument(
        None,
        help="Optional span anchor (markdown heading or structural dotted path).",
    ),
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    shared: bool = typer.Option(
        False,
        "--shared",
        help="Write the shared setforge.yaml (default: host-local local.yaml).",
    ),
) -> None:
    """Remove a PINNED override (file-level or a span); forks are left intact."""
    _override_remove(
        SpanKind.PINNED, file_id, anchor, reset=False, shared=shared, config=config
    )


@override_app.command("unfork")
def override_unfork(
    file_id: str = typer.Argument(..., metavar="FILE", help="Tracked_file id."),
    anchor: str | None = typer.Argument(
        None,
        help="Optional span anchor (markdown heading or structural dotted path).",
    ),
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    shared: bool = typer.Option(
        False,
        "--shared",
        help="Write the shared setforge.yaml (default: host-local local.yaml).",
    ),
) -> None:
    """Remove a FORKED override (file-level or a span); pins are left intact."""
    _override_remove(
        SpanKind.FORKED, file_id, anchor, reset=False, shared=shared, config=config
    )


@override_app.command("reset")
def override_reset(
    file_id: str = typer.Argument(..., metavar="FILE", help="Tracked_file id."),
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    shared: bool = typer.Option(
        False,
        "--shared",
        help="Write the shared setforge.yaml (default: host-local local.yaml).",
    ),
) -> None:
    """Clear ALL override state (disposition + every span) for a tracked_file."""
    _override_remove(None, file_id, None, reset=True, shared=shared, config=config)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@override_app.command("list")
def override_list(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
) -> None:
    """List each tracked_file's disposition, declared-span count, and drift state."""
    console = Console()
    cfg_path = _resolve_config_arg(config)
    cfg = load_config(cfg_path)
    repo_root = cfg_path.resolve().parent
    # Fold host-local disposition + spans over the shared base so the listing
    # reflects what THIS host would deploy.
    apply_host_local_tracked_file_overrides(cfg)
    report = compare_mod.compare_profile(
        cfg,
        profile,
        repo_root,
        transitions_dir=transitions.transitions_root(),
        ignored=load_ignored_orphans(),
    )

    table = Table(title="Overrides", show_header=True, header_style="bold")
    table.add_column("file")
    table.add_column("disposition")
    table.add_column("spans", justify="right")
    table.add_column("state")

    by_name = {e.name: e for e in report.entries}
    resolved = resolve_profile(cfg, profile)
    for name in resolved.tracked_files:
        tracked_file = cfg.tracked_files[name]
        disposition = (
            tracked_file.disposition.value
            if tracked_file.disposition is not None
            else "-"
        )
        span_count = len(tracked_file.spans)
        entry = by_name.get(name)
        if entry is None:
            state = "-"
        elif entry.status is not CompareStatus.DRIFTED:
            state = "in sync"
        elif entry.drift_is_expected:
            state = "expected drift"
        else:
            state = "unexpected drift"
        table.add_row(name, disposition, str(span_count), state)

    console.print(table)


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


@override_app.command("show")
def override_show(
    file_id: str = typer.Argument(..., metavar="FILE", help="Tracked_file id."),
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    spans: bool = typer.Option(
        False,
        "--spans",
        help="Render the span summary table + virtual-annotated body (stdout only).",
    ),
) -> None:
    """Show a tracked_file's spans, optionally with a virtual-annotated body.

    Without ``--spans`` this prints only the declared-span count. With
    ``--spans`` it adds the span summary table (with an ``ORPHANED`` column)
    and a synthesized annotated body. The annotation is synthesized at render
    time and printed to STDOUT only — the tracked file on disk is never
    touched. Markdown spans render as ``<!-- pinned:ANCHOR (virtual) -->``;
    structural spans as ``#`` (yaml) or ``//`` (jsonc) virtual comments. For
    strict JSON the comment is still only displayed, never persisted, so an
    invalid-on-disk comment is harmless.
    """
    # A wide console so rich never wraps / ellipsises a long anchor (e.g.
    # ``editor.fontSize``) or the ``(virtual)`` comment token in the table.
    console = Console(width=200)
    cfg_path = _resolve_config_arg(config)
    cfg = load_config(cfg_path)
    repo_root = cfg_path.resolve().parent
    apply_host_local_tracked_file_overrides(cfg)
    if file_id not in cfg.tracked_files:
        raise typer.BadParameter(f"unknown tracked_file {file_id!r}")
    tracked_file = cfg.tracked_files[file_id]
    src = resolve_src(tracked_file, repo_root)

    if not spans:
        console.print(f"{file_id}: {len(tracked_file.spans)} span(s) declared")
        return

    text = src.read_text(encoding="utf-8")
    _render_span_table(tracked_file, src, text, console)
    typer.echo("")
    typer.echo("=== annotated body (virtual — not written) ===")
    annotated = _annotate_body(tracked_file, src, text)
    # Emit the synthesized body via ``typer.echo`` (raw stdout, no rich
    # wrapping / markup) so heading '#' runs and the virtual comment tokens
    # survive byte-for-byte — the on-disk file is never touched (stdout-only).
    typer.echo(annotated)


def _span_orphaned(span: SpanEntry, src: Path, text: str) -> bool:
    """Whether ``span``'s anchor fails to resolve in the current source body."""
    if is_heading_anchor(span.anchor):
        try:
            bound_span(text, span.anchor)
        except (AnchorNotFoundError, AnchorAmbiguousError):
            return True
        return False
    model = _load_structural_model(src, text)
    return get_at_path(model, span.anchor) is ABSENT


def _render_span_table(
    tracked_file: TrackedFile, src: Path, text: str, console: Console
) -> None:
    """Render the span summary table with an ORPHANED column."""
    table = Table(title="Spans", show_header=True, header_style="bold")
    table.add_column("anchor")
    table.add_column("kind")
    table.add_column("semantics")
    table.add_column("ORPHANED")
    for span in tracked_file.spans:
        orphaned = "yes" if _span_orphaned(span, src, text) else "no"
        table.add_row(span.anchor, span.kind.value, span.semantics.value, orphaned)
    console.print(table)


def _annotate_body(tracked_file: TrackedFile, src: Path, text: str) -> str:
    """Synthesize a virtual-annotated body (markdown OR structural)."""
    if src.suffix.lower() in {".md", ".markdown"}:
        return _annotate_markdown(tracked_file, text)
    return _annotate_structural(tracked_file, src, text)


def _annotate_markdown(tracked_file: TrackedFile, text: str) -> str:
    """Inject ``<!-- pinned:ANCHOR (virtual) -->`` before each resolved span."""
    lines = text.splitlines(keepends=True)
    # Map span-start line -> comment, resolving each anchor once.
    inserts: dict[int, list[str]] = {}
    for span in tracked_file.spans:
        if not is_heading_anchor(span.anchor):
            continue
        try:
            span_region = bound_span(text, span.anchor)
        except (AnchorNotFoundError, AnchorAmbiguousError):
            continue
        comment = f"<!-- {span.kind.value}:{span.anchor} (virtual) -->\n"
        inserts.setdefault(span_region.start_line, []).append(comment)
    out: list[str] = []
    for idx, line in enumerate(lines):
        for comment in inserts.get(idx, []):
            out.append(comment)
        out.append(line)
    return "".join(out)


def _annotate_structural(tracked_file: TrackedFile, src: Path, text: str) -> str:
    """Append per-format virtual comments naming each structural span.

    Per-format prefix: ``#`` for yaml, ``//`` for jsonc / json. The
    annotation is appended as a trailing block (never spliced into the
    parsed tree) so the displayed body stays a faithful copy of the bytes
    plus an unmistakably-virtual footer; it is STDOUT-only and never written.
    """
    from setforge import jsonc

    prefix = (
        "//" if (jsonc.is_jsonc_file(src) or src.suffix.lower() == ".json") else "#"
    )
    footer_lines = [f"{prefix} virtual span annotations (not written):"]
    for span in tracked_file.spans:
        if is_heading_anchor(span.anchor):
            continue
        footer_lines.append(f"{prefix} {span.kind.value}:{span.anchor} (virtual)")
    body = text if text.endswith("\n") else text + "\n"
    return body + "\n".join(footer_lines) + "\n"
