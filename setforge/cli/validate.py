"""validate + fetch subcommands — config-shape checks + git-source pull.

``validate`` runs a battery of config-shape checks (schema, profile
chain, Jinja2 templates, tracked srcs, claude_plugins references) for
one profile (``--profile=NAME``) or every profile (``--all``).

``fetch`` is the git-source pull entry point: clone / fetch / dirty-gate
/ checkout-ref. For path-only sources it's a no-op.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import typer
from jinja2 import StrictUndefined, Template, TemplateSyntaxError, UndefinedError
from pydantic import BaseModel, ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from setforge import reconcile_adapter
from setforge import source as source_mod
from setforge.binaries import LOCAL_CONFIG_PATH as _LOCAL_CONFIG_PATH
from setforge.cli import _CONFIG_OPTION, _resolve_config_arg, app
from setforge.cli._help_examples import FETCH_EXAMPLES, VALIDATE_EXAMPLES
from setforge.cli._validate_errors import (
    format_schema_validation_error,
    format_yaml_parse_error,
    suggest_close_match,
)
from setforge.compare import resolve_src
from setforge.config import (
    Config,
    OrphanOverlay,
    OrphanOverlayClass,
    Profile,
    ResolvedProfile,
    TrackedFile,
    apply_local_overlay,
    collect_orphan_overlays,
    load_config,
    resolve_and_expand,
)
from setforge.errors import (
    ConfigError,
    SetforgeError,
    ValidationErrorWithContext,
)
from setforge.local_config import LocalConfig as _LocalConfig
from setforge.migrations._local_yaml import guard_local_yaml_schema, strip_retired_keys
from setforge.overlay_provenance import LocalOverlayError, LocalOverlayLoadError
from setforge.paths import template_context
from setforge.source import (
    ExtensionOverlay,
    MarketplaceOverlay,
    PluginOverlay,
    _LocalTrackedFileOverlay,
)
from setforge.user_section_markers import contains_user_section_marker


def _local_yaml_top_keys() -> list[str]:
    """Return the known top-level keys in ``local.yaml`` for close-match.

    Introspects :class:`setforge.local_config.LocalConfig.model_fields`
    rather than hand-maintaining a parallel tuple — the source of truth
    is the model itself, so adding a new top-level overlay block (e.g.
    ``marketplaces:`` post-local-overlay) automatically extends the candidate
    list with no edit needed here.
    """
    return list(_LocalConfig.model_fields.keys())


def _check_profile(
    cfg: Config,
    prof_name: str,
    repo_root: Path,
    failures: list[ValidationErrorWithContext | str],
) -> None:
    """Run checks 2-6 for a single profile, appending failures in-place."""
    ctx = f"profile {prof_name!r}"

    resolved = _check_profile_resolution(cfg, prof_name, repo_root, ctx, failures)
    if resolved is None:
        return

    # Check 1c: apply the local.yaml plugin / extension
    # / marketplace overlay so its collision / unknown-remove and
    # marketplace cross-ref errors surface at validate time too.
    # Mirrors Check 1b — the install path runs the same applier; the
    # validate path is a defensive offline backstop per SPEC 2 Q8.
    cross_ref_ran = _apply_local_overlay_check(cfg, resolved, prof_name, ctx, failures)

    for tracked_file_name in resolved.tracked_files:
        tracked_file = cfg.tracked_files[tracked_file_name]
        dot_ctx = f"{ctx}: tracked_file {tracked_file_name!r}"
        if not _check_jinja_templates(tracked_file, dot_ctx, failures):
            continue
        _check_tracked_srcs(tracked_file, repo_root, dot_ctx, failures)
        _check_no_markers_remain(tracked_file, repo_root, dot_ctx, failures)

    _check_package_refs(cfg, prof_name, ctx, failures)
    if not cross_ref_ran:
        _check_marketplaces(cfg, resolved, ctx, failures)


def _apply_local_overlay_check(
    cfg: Config,
    resolved: ResolvedProfile,
    prof_name: str,
    ctx: str,
    failures: list[ValidationErrorWithContext | str],
) -> bool:
    """Apply the local.yaml overlay and report cross-ref status to the caller.

    ``apply_local_overlay`` runs ``_validate_overlay_marketplace_cross_ref``
    over the mutated resolved plugin set as its final step.
    That is the SAME cross-ref invariant Check 6
    (``_check_marketplaces``) asserts, so re-running Check 6 after a
    completed overlay would emit a duplicate failure row per offender.

    Returns ``True`` when the cross-ref check ran (whether or not it
    found errors) — the caller skips Check 6 to avoid duplicates.
    Returns ``False`` when the load phase raised BEFORE the cross-ref
    check could run — the caller MUST still run Check 6 as a fallback
    to surface any pre-existing marketplace inconsistencies that the
    malformed overlay would have otherwise masked.

    Error routing:
    - :class:`LocalOverlayLoadError` (sentinel subclass): load-phase
      failure (YAML parse, non-mapping, Pydantic shape) → record under
      ``{ctx}`` and signal cross-ref did NOT run. Note:
      :func:`setforge.config._load_overlay_blocks` wraps raw
      :class:`pydantic.ValidationError` from the strict overlay-load
      schema into :class:`LocalOverlayLoadError` (config.py:782-783),
      so no separate ``except ValidationError`` clause is needed here
      — unlike :func:`_check_profile` (whose
      ``apply_preserve_user_keys_overlay`` path does NOT wrap).
    - :class:`LocalOverlayError`: resolver-phase collision or unknown-
      remove → cross-ref did NOT run; record and fall back to Check 6.
    - bare :class:`ConfigError`: emitted by the cross-ref check itself
      (e.g. plugin references a missing marketplace) → cross-ref ran;
      record and signal so the caller skips Check 6.
    - :class:`OSError` / :class:`UnicodeDecodeError`: unreadable
      local.yaml → route through
      :func:`format_yaml_parse_error`; cross-ref did NOT run, fall
      back to Check 6.
    """
    try:
        apply_local_overlay(cfg, resolved, prof_name)
    except LocalOverlayLoadError as exc:
        # Load failed BEFORE cross-ref ran; surface the error and let
        # the caller run Check 6 as a fallback so pre-existing
        # marketplace inconsistencies are not masked. Per the wrapping
        # invariant in config.py:782-783, this clause covers
        # ValidationError raised by the strict overlay-load schema too.
        failures.append(f"{ctx}: {exc}")
        return False
    except LocalOverlayError as exc:
        # Resolver-phase failure (add ∩ remove or unknown-remove).
        # Mutations did not complete, so the cross-ref check did not
        # run; fall back to Check 6.
        failures.append(f"{ctx}: {exc}")
        return False
    except ConfigError as exc:
        # The marketplace cross-ref check itself raised; the cross-ref
        # ran (and reported), so skip Check 6 to avoid a duplicate row.
        failures.append(f"{ctx}: {exc}")
        return True
    except (OSError, UnicodeDecodeError) as exc:
        # Unreadable local.yaml: route through the
        # YAML PARSE category formatter so the report-all-then-refuse
        # contract holds.
        failures.append(format_yaml_parse_error(_LOCAL_CONFIG_PATH, 1, 1, str(exc)))
        return False
    return True


def _check_profile_resolution(
    cfg: Config,
    prof_name: str,
    repo_root: Path,
    ctx: str,
    failures: list[ValidationErrorWithContext | str],
) -> ResolvedProfile | None:
    """Check 2: resolve profile; expand bundle ``file`` components (runs gates)."""
    try:
        return resolve_and_expand(cfg, prof_name, repo_root)
    except SetforgeError as exc:
        failures.append(f"{ctx}: {exc}")
        return None


def _check_jinja_templates(
    tracked_file: TrackedFile,
    dot_ctx: str,
    failures: list[ValidationErrorWithContext | str],
) -> bool:
    """Check 3: Jinja2 dst template renders with StrictUndefined.

    Returns ``True`` when the template is OK (or absent), ``False`` when a
    syntax/undefined-variable error was recorded — caller should skip
    further checks for this tracked_file.
    """
    if not tracked_file.template:
        return True
    try:
        Template(tracked_file.dst, undefined=StrictUndefined).render(
            **template_context()
        )
    except (TemplateSyntaxError, UndefinedError) as exc:
        failures.append(f"{dot_ctx}: unrenderable dst template: {exc}")
        return False
    return True


def _check_tracked_srcs(
    tracked_file: TrackedFile,
    repo_root: Path,
    dot_ctx: str,
    failures: list[ValidationErrorWithContext | str],
) -> None:
    """Check 4: tracked src exists on disk."""
    src = resolve_src(tracked_file, repo_root)
    if not src.exists():
        failures.append(f"{dot_ctx}: src {tracked_file.src} does not exist")


def _check_no_markers_remain(
    tracked_file: TrackedFile,
    repo_root: Path,
    dot_ctx: str,
    failures: list[ValidationErrorWithContext | str],
) -> None:
    """Check: no residual user-section markers remain in the tracked src.

    User-section markers are retired at schema 2.1 — ``setforge migrate``
    strips them from tracked sources. A tracked file that still carries a
    ``setforge:user-section`` marker line has not been migrated, so this
    offline gate (it runs under ``validate --all`` in CI) refuses it and
    points at the migration. Missing srcs are skipped: ``_check_tracked_srcs``
    already reports those.
    """
    src = resolve_src(tracked_file, repo_root)
    try:
        text = src.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError):
        return
    if contains_user_section_marker(text):
        failures.append(
            f"{dot_ctx}: tracked src {tracked_file.src} still contains "
            "setforge:user-section marker(s); markers are retired at schema "
            "2.1 — run 'setforge migrate --profile=<name>' to strip them."
        )


def _check_package_refs(
    cfg: Config,
    prof_name: str,
    ctx: str,
    failures: list[ValidationErrorWithContext | str],
) -> None:
    """Check 5: profile package refs — non-empty, no duplicates.

    Plugins, extensions, and cargo crates all declare through the
    ``packages`` surface now, so their dedup / empty-ref coverage is one
    walk of the profile's raw ``packages`` list. Walks the raw profile
    (before extends-merging) so duplicates that ``_merge_list`` would
    silently drop during ``resolve_profile`` are still caught.
    """
    raw_packages = cfg.profiles[prof_name].packages
    _check_dedup(
        raw_packages,
        ctx=ctx,
        failures=failures,
        empty_msg="packages contains empty ref",
        dup_label="packages duplicate",
    )


def _check_dedup(
    items: list[str],
    *,
    ctx: str,
    failures: list[ValidationErrorWithContext | str],
    empty_msg: str,
    dup_label: str,
) -> None:
    """Common dedup walk used by Check 5 and Check 5b."""
    seen: set[str] = set()
    reported_dup: set[str] = set()
    empty_reported = False
    for item in items:
        if not item.strip():
            if not empty_reported:
                failures.append(f"{ctx}: {empty_msg}")
                empty_reported = True
        elif item in seen:
            if item not in reported_dup:
                failures.append(f"{ctx}: {dup_label}: {item!r}")
                reported_dup.add(item)
        else:
            seen.add(item)


def _check_marketplaces(
    cfg: Config,
    resolved: ResolvedProfile,
    ctx: str,
    failures: list[ValidationErrorWithContext | str],
) -> None:
    """Check 6: claude_plugins marketplace-reference internal consistency.

    Every plugin referenced in the profile must have its marketplace
    declared in ``cfg.marketplaces``. (Plugin existence in
    ``cfg.claude_plugins`` is already validated by ``load_config`` →
    ``_validate_plugin_references``.)
    """
    marketplace_keys = set(cfg.marketplaces)
    for plugin_ref in reconcile_adapter.plugin_bare_names(cfg, resolved):
        bare_name = plugin_ref.split("@")[0]
        if bare_name in cfg.claude_plugins:
            mp_name = cfg.claude_plugins[bare_name].marketplace
            if mp_name not in marketplace_keys:
                failures.append(
                    f"{ctx}: plugin {bare_name!r} references unknown "
                    f"marketplace {mp_name!r}"
                )


def _check_local_yaml(
    local_yaml_path: Path, failures: list[ValidationErrorWithContext | str]
) -> None:
    """Validate ``~/.config/setforge/local.yaml`` against :class:`_LocalConfig`.

    Loads the file with ``ruamel.yaml.YAML(typ='rt')`` so the resulting
    ``CommentedMap`` preserves ``.lc`` line/column info for the snippet
    + pointer formatter. Absent or empty local.yaml is valid → no-op.
    YAML parse errors surface in the ``YAML PARSE ERROR`` category;
    schema errors in the ``SCHEMA VALIDATION ERROR`` category — never
    collapsed (anti-smell from SPEC 9).

    Each schema error is appended as a
    :class:`ValidationErrorWithContext` carrying file:line/column +
    snippet rows + close-match suggestion + fix hint. The caller
    (:func:`validate`) renders the structured carrier via
    :func:`setforge.cli._validate_errors.format_schema_validation_error`
    after all checks have run — guaranteeing report-all-then-refuse.
    """
    if not local_yaml_path.exists():
        return
    try:
        raw_text = local_yaml_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # Permission denied / unreadable / non-UTF-8 bytes — surface
        # as a YAML PARSE error so the report-all-then-refuse contract
        # holds (otherwise the exception bubbles past _check_local_yaml
        # and aborts the whole validate run before sibling failures
        # are reported).
        failures.append(format_yaml_parse_error(local_yaml_path, 1, 1, str(exc)))
        return
    if not raw_text.strip():
        return
    yaml = YAML(typ="rt")
    try:
        data = yaml.load(raw_text)
    except YAMLError as exc:
        line, col = _extract_yaml_error_position(exc)
        failures.append(format_yaml_parse_error(local_yaml_path, line, col, str(exc)))
        return
    if data is None:
        return
    if not isinstance(data, Mapping):
        failures.append(
            format_yaml_parse_error(
                local_yaml_path, 1, 1, "top-level of local.yaml must be a mapping"
            )
        )
        return
    # Detect-before-validate: refuse a cross-major-newer local.yaml
    # cleanly (one-line "upgrade setforge" + nonzero exit, no traceback)
    # BEFORE the extra="forbid" model would choke on its shape. A
    # malformed schema_version surfaces as a ConfigError, not a Pydantic
    # ValidationError. validate is read-only, so no migration runs here.
    guard_local_yaml_schema(data, local_yaml_path)
    # Strip the retired host_local_sections / OVERLAY spans keys IN MEMORY
    # before the extra="forbid" models validate — mirrors the loader
    # (_load_local_source_config in source.py). The span-declaration surface is
    # retired permanently and version-independently, so a pre-4.0 / 4.0
    # local.yaml still carrying those keys must validate clean: the strict
    # _LocalTrackedFileOverlay no longer declares them, and without this strip
    # a surviving retired key trips extra_forbidden. In-memory only — the
    # on-disk retirement is owned by the span-surface-retire migration.
    strip_retired_keys(data)
    try:
        _LocalConfig.model_validate(dict(data))
    except ValidationError as exc:
        for err in exc.errors():
            failures.append(
                _validation_error_to_context(local_yaml_path, raw_text, data, err)
            )
    _check_local_yaml_tracked_files(local_yaml_path, raw_text, data, failures)
    _check_local_yaml_overlay_blocks(local_yaml_path, raw_text, data, failures)


def _check_local_yaml_tracked_files(
    local_yaml_path: Path,
    raw_text: str,
    data: Mapping[str, object],
    failures: list[ValidationErrorWithContext | str],
) -> None:
    """Strictly validate each ``tracked_files.<id>`` overlay entry.

    The loose ``_LocalConfig.tracked_files: dict[str, object]`` accepts any
    nested shape, so a typo'd per-tracked_file overlay key (e.g.
    ``tracked_files.<id>.not_a_real_field``) escapes the top-level
    :func:`_check_local_yaml` pass. Re-validating each entry against the
    ``extra="forbid"`` :class:`_LocalTrackedFileOverlay` surfaces the nested
    error here — in the SCHEMA VALIDATION ERROR category with line resolution
    — rather than later as an unformatted overlay-apply failure during
    profile resolution.
    """
    raw_tracked_files = data.get("tracked_files")
    if not isinstance(raw_tracked_files, Mapping):
        return
    for tf_id, overlay in raw_tracked_files.items():
        if not isinstance(overlay, Mapping):
            continue
        try:
            _LocalTrackedFileOverlay.model_validate(dict(overlay))
        except ValidationError as exc:
            for err in exc.errors():
                failures.append(
                    _validation_error_to_context(
                        local_yaml_path,
                        raw_text,
                        data,
                        {**err, "loc": ("tracked_files", tf_id, *err["loc"])},
                    )
                )


# The three SPEC 2 per-host overlay blocks, each a strict (``extra="forbid"``)
# ``add`` / ``remove`` model. ``_LocalConfig`` types them loosely as
# ``dict[str, object]`` so a typo'd sub-key (e.g. ``plugins.ad``) escapes the
# top-level :func:`_check_local_yaml` pass — the sibling of the loose
# ``tracked_files`` typing that :func:`_check_local_yaml_tracked_files` guards.
_LOCAL_YAML_OVERLAY_BLOCK_MODELS: dict[str, type[BaseModel]] = {
    "plugins": PluginOverlay,
    "extensions": ExtensionOverlay,
    "marketplaces": MarketplaceOverlay,
}


def _check_local_yaml_overlay_blocks(
    local_yaml_path: Path,
    raw_text: str,
    data: Mapping[str, object],
    failures: list[ValidationErrorWithContext | str],
) -> None:
    """Strictly validate each ``plugins`` / ``extensions`` / ``marketplaces`` block.

    The loose ``_LocalConfig.{plugins,extensions,marketplaces}: dict[str, object]``
    typing accepts any nested shape, so a typo'd overlay sub-key (e.g.
    ``plugins.ad`` instead of ``plugins.add``) escapes the top-level
    :func:`_check_local_yaml` pass. Re-validating each block against its
    strict (``extra="forbid"``) overlay model surfaces the nested error here
    — in the SCHEMA VALIDATION ERROR category with mockup-D line resolution
    + did-you-mean suggestion — rather than only as the unformatted raw
    pydantic string :func:`_apply_local_overlay_check` emits at
    profile-resolution time.

    Sibling of :func:`_check_local_yaml_tracked_files`; the two split because
    the overlay blocks are top-level single models (``('plugins', <leaf>)``)
    whereas ``tracked_files`` is a per-id mapping (``('tracked_files', <id>,
    <leaf>)``). The candidate list per block is dispatched by
    :func:`_candidate_list_for` from ``loc[0]``.
    """
    for block_key, model in _LOCAL_YAML_OVERLAY_BLOCK_MODELS.items():
        block = data.get(block_key)
        if not isinstance(block, Mapping):
            continue
        try:
            model.model_validate(dict(block))
        except ValidationError as exc:
            for err in exc.errors():
                failures.append(
                    _validation_error_to_context(
                        local_yaml_path,
                        raw_text,
                        data,
                        {**err, "loc": (block_key, *err["loc"])},
                    )
                )


def _partition_orphan_ids(
    orphans: list[OrphanOverlay],
    seen_unknown: set[str],
    seen_off: set[str],
    unknown_ids: list[str],
    off_profile_ids: list[str],
) -> None:
    """Classify one profile's ``orphans`` into the unknown / off-profile buckets.

    Dedups across profiles via the ``seen_*`` sets (an id surfaced by an
    earlier profile is not re-appended) and appends first-seen ids to the
    matching ordered list in place.
    """
    for orphan in orphans:
        if orphan.class_ is OrphanOverlayClass.UNKNOWN:
            if orphan.id not in seen_unknown:
                seen_unknown.add(orphan.id)
                unknown_ids.append(orphan.id)
        elif orphan.id not in seen_off:
            seen_off.add(orphan.id)
            off_profile_ids.append(orphan.id)


def _check_orphan_overlays(
    cfg: Config,
    profiles_to_check: list[str],
    local_yaml_path: Path,
    repo_root: Path,
    failures: list[ValidationErrorWithContext | str],
) -> list[str]:
    """Surface ``local.yaml`` overlay ids the apply site silently skips.

    Two classes (see :class:`setforge.config.OrphanOverlayClass`):

    - **Unknown** — id absent from ``cfg.tracked_files`` (a typo / stale
      entry). Appended to ``failures`` as a
      :class:`~setforge.errors.ValidationErrorWithContext` (exit 1), with a
      did-you-mean suggestion drawn from the known tracked_file ids.
    - **Off-profile** — id in ``cfg.tracked_files`` but in none of the
      checked profiles' resolved lists. Returned as a non-fatal note
      string for the caller to print to stderr; never added to
      ``failures`` (exit stays 0).

    The off-profile bucket aggregates across ``profiles_to_check``: under
    ``--all`` an id legitimately used by ANOTHER profile is not flagged.
    For a single ``--profile=X`` the aggregation degenerates to that one
    profile, matching the per-profile spec semantics exactly.

    Returns the off-profile note lines (possibly empty). Reads the
    ``local.yaml`` CommentedMap once to anchor the unknown-id failure's
    line/column on the offending ``tracked_files.<id>`` key.
    """
    unknown_ids: list[str] = []
    in_some_profile: set[str] = set()
    off_profile_ids: list[str] = []
    seen_off: set[str] = set()
    seen_unknown: set[str] = set()
    for prof_name in profiles_to_check:
        try:
            resolved = resolve_and_expand(cfg, prof_name, repo_root)
        except SetforgeError:
            continue  # already surfaced by _check_profile_resolution
        in_some_profile.update(resolved.tracked_files)
        try:
            orphans = collect_orphan_overlays(
                cfg, resolved, local_config_path=local_yaml_path
            )
        except (SetforgeError, ValidationError, OSError, UnicodeDecodeError):
            # A malformed / unparseable / unreadable / schema-mismatched
            # local.yaml is already reported by _check_local_yaml (the
            # dedicated local.yaml pass). The orphan classifier re-parses
            # the same file; skip only THIS profile rather than abandoning
            # the rest of the run — under --all, unknown-id failures from
            # other profiles must still surface.
            continue
        _partition_orphan_ids(
            orphans, seen_unknown, seen_off, unknown_ids, off_profile_ids
        )

    known_ids = list(cfg.tracked_files)
    for tf_id in unknown_ids:
        failures.append(
            _orphan_overlay_unknown_failure(local_yaml_path, tf_id, known_ids)
        )

    # An id off-profile for every checked profile is a real note; one used
    # by SOME checked profile is legitimate and dropped.
    notes = [
        _orphan_overlay_off_profile_note(tf_id)
        for tf_id in off_profile_ids
        if tf_id not in in_some_profile
    ]
    return notes


def _orphan_overlay_unknown_failure(
    local_yaml_path: Path, tf_id: str, known_ids: list[str]
) -> ValidationErrorWithContext:
    """Build the unknown-orphan-overlay failure carrier.

    Resolves the offending ``tracked_files.<id>`` key's line/column from
    the on-disk ``local.yaml`` (best-effort; falls back to ``(1, 1)`` when
    the file can't be re-read or the key isn't locatable), and attaches a
    did-you-mean suggestion via :func:`suggest_close_match` over the known
    tracked_file ids.
    """
    line_1, col_1, snippet_lines = _locate_local_tracked_file_key(
        local_yaml_path, tf_id
    )
    suggestion = suggest_close_match(tf_id, known_ids)
    fix_hint = (
        f"edit {_home_relative(local_yaml_path)}:{line_1} — "
        f"local.yaml references tracked_file {tf_id!r}, which is not declared "
        f"in setforge.yaml. Fix the id or remove the overlay entry."
    )
    return ValidationErrorWithContext(
        file_path=local_yaml_path,
        line=line_1,
        column=col_1,
        snippet_lines=snippet_lines,
        field_value=tf_id,
        fix_hint=fix_hint,
        suggestion=suggestion,
    )


def _orphan_overlay_off_profile_note(tf_id: str) -> str:
    """Render the non-fatal off-profile note line.

    The id is a real tracked_file but is not used by any profile under
    validation — legitimate on a multi-profile host, so it is informational
    only.
    """
    return (
        f"note: local.yaml overlay for tracked_file {tf_id!r} is declared in "
        f"setforge.yaml but not used by the validated profile(s); the overlay "
        f"is skipped (off-profile, not an error)."
    )


def _locate_local_tracked_file_key(
    local_yaml_path: Path, tf_id: str
) -> tuple[int, int, list[str]]:
    """Best-effort (line, col, snippet) of ``tracked_files.<id>`` in local.yaml.

    Re-reads the file in round-trip mode to walk the ``.lc`` tables.
    Falls back to ``(1, 1, [])`` when the file is unreadable, unparseable,
    or the key is not locatable — the failure still surfaces, just without
    a precise pointer.
    """
    try:
        raw_text = local_yaml_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 1, 1, []
    try:
        data = YAML(typ="rt").load(raw_text)
    except YAMLError:
        return 1, 1, []
    if not isinstance(data, Mapping):
        return 1, 1, []
    tracked = data.get("tracked_files")
    if not isinstance(tracked, Mapping) or tf_id not in tracked:
        return 1, 1, []
    line_1, col_1 = _lookup_key_position(tracked, tf_id)
    snippet_lines = _build_snippet(raw_text.splitlines(), line_1)
    return line_1, col_1, snippet_lines


def _extract_yaml_error_position(exc: YAMLError) -> tuple[int, int]:
    """Best-effort (line, col) extraction from a ruamel ``YAMLError``.

    Falls back to ``(1, 1)`` when the exception carries no
    ``problem_mark`` attribute (rare for parser errors but possible for
    constructor / composer errors).
    """
    mark = getattr(exc, "problem_mark", None)
    if mark is None:
        return 1, 1
    # ruamel marks are 0-indexed; mockup D shows 1-indexed line numbers.
    return int(mark.line) + 1, int(mark.column) + 1


def _validation_error_to_context(
    local_yaml_path: Path,
    raw_text: str,
    data: Mapping[str, object],
    err: Mapping[str, object],
) -> ValidationErrorWithContext:
    """Convert one ``Pydantic ValidationError`` entry to a context carrier.

    Walks the error's ``loc`` tuple against ``data``'s ``.lc`` table to
    map the field path to a source line/column — including nested
    overlay-class errors (``len(loc) > 1``) via the error-line-walker. Builds
    a 1-3-line snippet from ``raw_text`` around the offending line.
    When the error is an ``extra_forbidden`` shape, the offending key
    itself is the ``field_value`` and we consult the close-match
    suggester against the overlay-class-specific candidate list
    dispatched by :func:`_candidate_list_for`.
    """
    loc = err.get("loc", ())
    err_type = err.get("type", "")
    msg = err.get("msg", "")
    raw_lines = raw_text.splitlines()

    line_1, col_1, field_value, suggestion = _resolve_error_position(
        data, loc, err_type, msg
    )

    # Snippet: the offending line plus up to 1 line of surrounding
    # context for the schema-error UX (mockup D shows 2-3 lines of
    # context for nested keys; for top-level keys 1 line is enough).
    snippet_lines = _build_snippet(raw_lines, line_1)
    fix_hint = _build_fix_hint(local_yaml_path, line_1, err_type, field_value, msg)
    return ValidationErrorWithContext(
        file_path=local_yaml_path,
        line=line_1,
        column=col_1,
        snippet_lines=snippet_lines,
        field_value=field_value,
        fix_hint=fix_hint,
        suggestion=suggestion,
    )


def _resolve_error_position(
    data: Mapping[str, object],
    loc: tuple[object, ...] | object,
    err_type: object,
    msg: object,
) -> tuple[int, int, str, str | None]:
    """Map a Pydantic error ``loc`` to (line, col, field_value, suggestion).

    Three shapes:

    - Empty / non-tuple loc → ``(1, 1, "", None)`` (top-level placeholder).
    - Single-element loc (``('plgins',)``) → close-match against
      :func:`_local_yaml_top_keys`; line/col anchored to the
      offending top-level key.
    - Nested loc (``('tracked_files', <id>, 'bogus')``,
      ``('plugins', 'add')``) → walk the ``.lc`` tables to surface the
      real nested line/column; candidate list dispatched via
      :func:`_candidate_list_for` per overlay-class shape.

    Falls back to ``(1, 1, "", None)`` when the parent chain can't be
    walked on the ``.lc`` table (intermediate non-Mapping, missing key,
    or :exc:`AttributeError` from a plain ``dict``).
    """
    if not isinstance(loc, tuple) or not loc:
        return 1, 1, "", None
    if len(loc) == 1:
        return _resolve_top_level_local_error(data, loc, err_type, msg)
    return _resolve_nested_local_error(data, loc, err_type, msg)


def _resolve_top_level_local_error(
    data: Mapping[str, object],
    loc: tuple[object, ...],
    err_type: object,
    msg: object,
) -> tuple[int, int, str, str | None]:
    """Resolve a single-element ``loc`` against the top-level CommentedMap."""
    head = str(loc[0])
    candidates = _candidate_list_for(loc)
    if err_type == "extra_forbidden":
        line_1, col_1 = _lookup_key_position(data, head)
        suggestion = suggest_close_match(head, candidates)
        return line_1, col_1, head, suggestion
    line_1, col_1 = _lookup_value_position(data, head)
    field_value = _stringify_field_value(data, head, msg)
    return line_1, col_1, field_value, None


def _resolve_nested_local_error(
    data: Mapping[str, object],
    loc: tuple[object, ...],
    err_type: object,
    msg: object,
) -> tuple[int, int, str, str | None]:
    """Resolve a nested ``loc`` against nested ``.lc`` tables.

    Walks down ``data`` following each step of ``loc[:-1]`` until the
    parent of the leaf is reached, then anchors line/col on the leaf
    via the parent's ``.lc.key(...)`` / ``.lc.value(...)``. Falls back
    to ``(1, 1, "", None)`` when an intermediate step is non-Mapping or
    missing — keeps the formatter from mis-pointing into an unrelated
    region of the file.

    Sibling of :func:`_resolve_nested_setforge_error`
    on the ``setforge.yaml`` side; the two stay separate because the
    candidate-list dispatch differs (per-overlay-class for local.yaml,
    per-Profile/TrackedFile shape for setforge.yaml).
    """
    parent: object = data
    for step in loc[:-1]:
        if isinstance(parent, Mapping) and step in parent:
            parent = parent[step]
        else:
            return 1, 1, "", None
    leaf = str(loc[-1])
    if not isinstance(parent, Mapping):
        return 1, 1, "", None
    candidates = _candidate_list_for(loc)
    if err_type == "extra_forbidden":
        line_1, col_1 = _lookup_key_position(parent, leaf)
        suggestion = suggest_close_match(leaf, candidates) if candidates else None
        return line_1, col_1, leaf, suggestion
    line_1, col_1 = _lookup_value_position(parent, leaf)
    field_value = _stringify_field_value(parent, leaf, msg)
    return line_1, col_1, field_value, None


def _candidate_list_for(loc: tuple[object, ...]) -> list[str]:
    """Return the close-match candidate list for the local.yaml error site.

    Dispatches on ``loc[0]`` to the right overlay-class model's
    ``model_fields.keys()``. Introspection avoids the
    hand-maintained-tuple anti-smell — adding a field to e.g.
    :class:`PluginOverlay` extends the suggestion surface automatically.

    Shapes:

    - Empty / non-tuple ``loc`` → top-level :class:`LocalConfig` keys.
    - ``('plugins', ...)`` → :class:`PluginOverlay.model_fields`.
    - ``('extensions', ...)`` → :class:`ExtensionOverlay.model_fields`.
    - ``('marketplaces', ...)`` → :class:`MarketplaceOverlay.model_fields`.
    - ``('tracked_files', <id>, ...)`` →
      :class:`_LocalTrackedFileOverlay.model_fields`.
    - anything else → top-level keys (fallback).
    """
    if not loc:
        return _local_yaml_top_keys()
    head = loc[0]
    match head:
        case "plugins":
            return list(PluginOverlay.model_fields.keys())
        case "extensions":
            return list(ExtensionOverlay.model_fields.keys())
        case "marketplaces":
            return list(MarketplaceOverlay.model_fields.keys())
        case "tracked_files":
            return list(_LocalTrackedFileOverlay.model_fields.keys())
        case _:
            return _local_yaml_top_keys()


def _lookup_key_position(data: Mapping[str, object], key: str) -> tuple[int, int]:
    """Return 1-indexed (line, col) of ``key`` in the ruamel CommentedMap.

    Returns (1, 1) when ``data`` lacks a ``.lc`` attribute (plain dict
    fallback) or the key is absent.
    """
    lc = getattr(data, "lc", None)
    if lc is None:
        return 1, 1
    try:
        line0, col0 = lc.key(key)
    except (KeyError, AttributeError):
        return 1, 1
    return int(line0) + 1, int(col0) + 1


def _lookup_value_position(data: Mapping[str, object], key: str) -> tuple[int, int]:
    """Return 1-indexed (line, col) of the VALUE of ``key`` in the map."""
    lc = getattr(data, "lc", None)
    if lc is None:
        return 1, 1
    try:
        line0, col0 = lc.value(key)
    except (KeyError, AttributeError):
        return 1, 1
    return int(line0) + 1, int(col0) + 1


def _stringify_field_value(data: Mapping[str, object], key: str, msg: object) -> str:
    """Render the offending value as the underline target.

    Falls back to a short token from ``msg`` when ``data[key]`` isn't a
    scalar (mappings / lists don't render usefully under ``^^^^``).
    """
    value = data.get(key)
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    # Non-scalar: use a short literal from the message tail (e.g.
    # "Input should be a mapping" → ``mapping``).
    msg_str = str(msg).rsplit(maxsplit=1)[-1] if msg else ""
    return msg_str or key


def _build_snippet(raw_lines: list[str], line_1: int) -> list[str]:
    """Return up to 3 snippet lines centered on ``line_1`` (1-indexed)."""
    if not raw_lines:
        return [""]
    idx = max(line_1 - 1, 0)
    start = max(idx - 1, 0)
    end = min(idx + 1, len(raw_lines))
    return raw_lines[start : end + 1]


def _home_relative(path: Path) -> str:
    """Return ``path`` with the user's home prefix collapsed to ``~``.

    Mirrors the rendering convention used by the ``Fix:`` action lines
    (mockup D) — keeps the on-screen prefix short without making the
    underlying ``Path`` lossy. Uses :meth:`Path.relative_to` to anchor
    the match at the home boundary (avoids the theoretical
    ``/tmp/home/raul/...`` false-match a substring ``str.replace``
    would hit).
    """
    try:
        rel = path.relative_to(Path.home())
    except ValueError:
        return str(path)
    rel_str = str(rel)
    if rel_str == ".":
        # ``path`` is exactly ``Path.home()`` — render as bare ``~``.
        return "~"
    return f"~/{rel_str}"


def _build_fix_hint(
    local_yaml_path: Path, line_1: int, err_type: object, field_value: str, msg: object
) -> str:
    """Render the ``Fix:`` action line per mockup D.

    Different error types get tailored language — ``extra_forbidden``
    gets "unknown key", value-shape errors get the Pydantic message.
    The "remove or rename" phrasing is intentionally site-neutral so
    the same hint works for top-level keys AND nested overlay-class
    keys without flagging a nested key as
    "top-level".
    """
    home_path = _home_relative(local_yaml_path)
    if err_type == "extra_forbidden":
        return (
            f"edit {home_path}:{line_1} — unknown key {field_value!r} "
            "(remove or rename to a known key)"
        )
    return f"edit {home_path}:{line_1} — {msg}"


def render_setforge_yaml_validation_error(
    config_path: Path, exc: ValidationError
) -> list[str]:
    failures: list[ValidationErrorWithContext | str] = []
    _route_setforge_yaml_validation_error(config_path, exc, failures)
    return _render_failures(failures).split("\n")


def _route_setforge_yaml_validation_error(
    config_path: Path,
    exc: ValidationError,
    failures: list[ValidationErrorWithContext | str],
) -> None:
    """Route ``ValidationError`` from ``load_config`` through did-you-mean formatters.

    Re-loads ``setforge.yaml`` with ``YAML(typ="rt")`` (lazy — only when
    a ValidationError fired) so the resulting ``CommentedMap`` carries
    ``.lc`` line/column info for each error's ``loc`` path. Each
    Pydantic error becomes one ``ValidationErrorWithContext`` carrier
    appended to ``failures``; the caller renders them through the
    existing :func:`_render_failures` mechanism.

    The candidate list for close-match suggestions is introspected from
    :attr:`setforge.config.Config.model_fields` (top-level keys) or the
    nested model's ``model_fields`` for nested ``extra_forbidden`` errors.

    If the re-read fails — race window where the file became
    unreadable (:class:`OSError`) or syntactically invalid
    (:class:`ruamel.yaml.error.YAMLError`) between ``load_config``'s
    parse and this routing — fall back to the top-level placeholder
    rather than letting either exception replace the original
    :class:`ValidationError`.
    """
    try:
        raw_text = config_path.read_text(encoding="utf-8")
        raw_lines = raw_text.splitlines()
        yaml_rt = YAML(typ="rt")
        raw = yaml_rt.load(raw_text)
    except (OSError, YAMLError):
        # Either the re-read failed (race window) or the file became
        # syntactically invalid between ``load_config``'s parse and
        # this routing. Fall back to the top-level placeholder so the
        # original ValidationError still surfaces — mirrors the
        # ``_check_local_yaml`` resilience pattern.
        raw = None
        raw_lines = []
    if raw is None or not isinstance(raw, Mapping):
        # Malformed top-level shape (or unreadable / unparseable
        # re-read) — fall back to top-level (1, 1) placeholder; the
        # error message carries the diagnostic.
        for err in exc.errors():
            failures.append(_build_top_level_fallback(config_path, err))
        return
    for err in exc.errors():
        failures.append(
            _setforge_yaml_error_to_context(config_path, raw_lines, raw, err)
        )


def _setforge_yaml_error_to_context(
    config_path: Path,
    raw_lines: list[str],
    raw: Mapping[str, object],
    err: Mapping[str, object],
) -> ValidationErrorWithContext:
    """Convert one Pydantic error from ``load_config`` to a did-you-mean carrier.

    Sibling of :func:`_validation_error_to_context` for the engine
    config side. Walks the error's ``loc`` against ``raw``'s nested
    ``.lc`` tables; picks the candidate list for close-match from the
    appropriate Pydantic model at that nesting depth.
    """
    loc_raw = err.get("loc", ())
    loc = loc_raw if isinstance(loc_raw, tuple) else ()
    err_type = err.get("type", "")
    msg = err.get("msg", "")
    line_1, col_1, field_value, suggestion = _resolve_setforge_yaml_error_position(
        raw, loc, err_type, msg
    )
    snippet_lines = _build_snippet(raw_lines, line_1)
    fix_hint = _build_setforge_fix_hint(config_path, line_1, err_type, field_value, msg)
    return ValidationErrorWithContext(
        file_path=config_path,
        line=line_1,
        column=col_1,
        snippet_lines=snippet_lines,
        field_value=field_value,
        fix_hint=fix_hint,
        suggestion=suggestion,
    )


def _resolve_setforge_yaml_error_position(
    raw: Mapping[str, object],
    loc: tuple[object, ...],
    err_type: object,
    msg: object,
) -> tuple[int, int, str, str | None]:
    """Map a setforge.yaml Pydantic ``loc`` to (line, col, field_value, suggestion).

    Handles three shapes:

    - Empty loc → (1, 1, "", None) (top-level shape error).
    - Single-element loc (``('proffiles',)``) → close-match against
      :attr:`Config.model_fields.keys()`.
    - Nested loc (``('profiles', 'p', 'tipo')`` /
      ``('tracked_files', 'd', 'srcc')``) → walk the ``.lc`` tables to
      locate the offending nested key, candidate list from the matching
      nested Pydantic model's ``model_fields``.

    .. note::

        Sibling of :func:`_resolve_error_position` (local.yaml side) by
        design. The two stay separate because the candidate-list
        dispatch differs (setforge.yaml top-level uses ``Config`` /
        ``Profile`` / ``TrackedFile`` shapes; local.yaml uses
        ``LocalConfig`` + 4 overlay-class candidate lists). Unifying
        would entangle the dispatch tables; keep them split.
    """
    if not loc:
        return 1, 1, "", None
    if len(loc) == 1:
        return _resolve_top_level_setforge_error(raw, loc, err_type, msg)
    return _resolve_nested_setforge_error(raw, loc, err_type, msg)


def _resolve_top_level_setforge_error(
    raw: Mapping[str, object],
    loc: tuple[object, ...],
    err_type: object,
    msg: object,
) -> tuple[int, int, str, str | None]:
    """Resolve a single-element ``loc`` against the top-level CommentedMap."""
    head = str(loc[0])
    candidates = list(Config.model_fields.keys())
    if err_type == "extra_forbidden":
        line_1, col_1 = _lookup_key_position(raw, head)
        suggestion = suggest_close_match(head, candidates)
        return line_1, col_1, head, suggestion
    line_1, col_1 = _lookup_value_position(raw, head)
    field_value = _stringify_field_value(raw, head, msg)
    return line_1, col_1, field_value, None


def _resolve_nested_setforge_error(
    raw: Mapping[str, object],
    loc: tuple[object, ...],
    err_type: object,
    msg: object,
) -> tuple[int, int, str, str | None]:
    """Resolve a nested ``loc`` against the nested ``.lc`` tables.

    Sibling of :func:`_resolve_nested_local_error` (local.yaml side).
    Handles ``profiles.<name>.<key>`` and ``tracked_files.<id>.<key>``
    shapes — the common cases for setforge.yaml typo close-match
    suggestions.
    """
    # Walk down to the parent of the leaf so we can call
    # ``.lc.key(leaf)`` on it. Only mapping shapes are exercised today;
    # integer-keyed list traversal (e.g. ``loc=('profiles', 'p',
    # 'extensions', 'include', 0)``) is not yet wired up and returns
    # the ``(1, 1, '', None)`` fallback. Extension to CommentedSeq
    # subscripts is intentionally deferred — current acceptance does
    # not exercise list-indexed loc shapes.
    parent: object = raw
    for step in loc[:-1]:
        if isinstance(parent, Mapping) and step in parent:
            parent = parent[step]
        else:
            return 1, 1, "", None
    leaf = str(loc[-1])
    if not isinstance(parent, Mapping):
        return 1, 1, "", None
    candidates = _candidates_for_nested_loc(loc)
    if err_type == "extra_forbidden":
        line_1, col_1 = _lookup_key_position(parent, leaf)
        suggestion = suggest_close_match(leaf, candidates) if candidates else None
        return line_1, col_1, leaf, suggestion
    line_1, col_1 = _lookup_value_position(parent, leaf)
    field_value = _stringify_field_value(parent, leaf, msg)
    return line_1, col_1, field_value, None


def _candidates_for_nested_loc(loc: tuple[object, ...]) -> list[str]:
    """Return the close-match candidate list for the nested error site.

    Maps the loc shape to the Pydantic model whose ``model_fields`` are
    the valid keys at that depth:

    - ``('profiles', <name>, <key>)`` → :attr:`Profile.model_fields`.
    - ``('tracked_files', <id>, <key>)`` → :attr:`TrackedFile.model_fields`.
    - Anything else → empty list (no suggestion fires).
    """
    if len(loc) < 3:
        return []
    head = str(loc[0])
    if head == "profiles":
        return list(Profile.model_fields.keys())
    if head == "tracked_files":
        return list(TrackedFile.model_fields.keys())
    return []


def _build_setforge_fix_hint(
    config_path: Path,
    line_1: int,
    err_type: object,
    field_value: str,
    msg: object,
) -> str:
    """Render the ``Fix:`` action line for a setforge.yaml error.

    Sibling of :func:`_build_fix_hint`; uses repo-relative path
    (display root is the directory of ``setforge.yaml`` itself) and
    "unknown key" wording for ``extra_forbidden``.
    """
    home_path = _home_relative(config_path)
    if err_type == "extra_forbidden":
        return (
            f"edit {home_path}:{line_1} — unknown key {field_value!r} "
            "(remove or rename to a known key)"
        )
    return f"edit {home_path}:{line_1} — {msg}"


def _build_top_level_fallback(
    config_path: Path, err: Mapping[str, object]
) -> ValidationErrorWithContext:
    """Fallback carrier when the rt re-load returns a non-Mapping root.

    Surfaces the Pydantic message at (1, 1) without snippet/pointer
    detail — the top-level shape is broken at a level the snippet UX
    cannot meaningfully render against.
    """
    msg = str(err.get("msg", ""))
    return ValidationErrorWithContext(
        file_path=config_path,
        line=1,
        column=1,
        snippet_lines=[""],
        field_value=msg or "value",
        fix_hint=f"edit {config_path} — {msg}",
        suggestion=None,
    )


def _render_failures(failures: list[ValidationErrorWithContext | str]) -> str:
    """Render every failure carrier to its final string form.

    String failures (legacy ``f"{ctx}: {msg}"`` from the existing
    ``_check_profile`` path) flow through unchanged.
    :class:`ValidationErrorWithContext` carriers are rendered via
    :func:`format_schema_validation_error`.
    """
    rendered: list[str] = []
    for failure in failures:
        if isinstance(failure, ValidationErrorWithContext):
            rendered.append(
                format_schema_validation_error(
                    path=failure.file_path,
                    line=failure.line,
                    col=failure.column,
                    snippet_lines=failure.snippet_lines,
                    field_value=failure.field_value,
                    fix_hint=failure.fix_hint,
                    suggestion=failure.suggestion,
                )
            )
        else:
            rendered.append(failure)
    return "\n".join(rendered)


@app.command("validate", epilog=VALIDATE_EXAMPLES)
def validate(
    profile: str | None = typer.Option(
        None, "--profile", "-p", help="Validate a specific profile."
    ),
    all_profiles: bool = typer.Option(
        False, "--all", help="Validate every profile in the YAML."
    ),
    config: Path = _CONFIG_OPTION,
) -> None:
    """Config-shape validation; no filesystem comparison or live target paths."""
    config = _resolve_config_arg(config)
    if profile is not None and all_profiles:
        typer.secho(
            "error: --profile and --all are mutually exclusive",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(2)
    if profile is None and not all_profiles:
        typer.secho(
            "error: one of --profile or --all is required",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(2)

    failures: list[ValidationErrorWithContext | str] = []

    # Check 1: Pydantic schema validation + cross-field checks in load_config.
    # ValidationError → did-you-mean close-match UX; SetforgeError
    # (cycle / missing-profile / file-not-found / etc.) keeps its existing
    # bail-on-first routing — these are cross-field violations that don't
    # have a useful "Did you mean" suggestion path.
    try:
        # tolerate_unknown=False keeps validate a strict linter: an unknown
        # key raises ValidationError (routed to the did-you-mean formatter)
        # rather than being warned-and-stripped as on the runtime path.
        cfg = load_config(config, tolerate_unknown=False)
    except ValidationError as exc:
        _route_setforge_yaml_validation_error(config, exc, failures)
        typer.echo(_render_failures(failures))
        typer.echo(f"=== validation FAILED: {len(failures)} errors ===")
        typer.echo("no changes will be made until the errors are resolved")
        raise typer.Exit(1) from exc
    except SetforgeError as exc:
        typer.echo(f"schema: {exc}")
        raise typer.Exit(1) from exc

    repo_root = config.resolve().parent

    if all_profiles:
        profiles_to_check: list[str] = list(cfg.profiles)
    else:
        assert profile is not None  # guarded above; narrow for mypy
        profiles_to_check = [profile]

    for prof_name in profiles_to_check:
        _check_profile(cfg, prof_name, repo_root, failures)

    # Check 7: host-local local.yaml schema + parse errors
    # with mockup-D UX. Collect into the same failures list so the
    # report-all-then-refuse contract holds across all check categories.
    _check_local_yaml(_LOCAL_CONFIG_PATH, failures)

    # Check 8: orphan local.yaml overlay entries. Unknown ids → failures
    # (exit 1, did-you-mean); off-profile ids → non-fatal stderr notes
    # (exit stays 0). The apply site stays silent; validate is the surface.
    off_profile_notes = _check_orphan_overlays(
        cfg, profiles_to_check, _LOCAL_CONFIG_PATH, repo_root, failures
    )
    for note in off_profile_notes:
        typer.secho(note, err=True, fg=typer.colors.YELLOW)

    if failures:
        typer.echo(_render_failures(failures))
        typer.echo(f"=== validation FAILED: {len(failures)} errors ===")
        typer.echo("no changes will be made until the errors are resolved")
        raise typer.Exit(1)

    typer.echo("ok")


@app.command(epilog=FETCH_EXAMPLES)
def fetch() -> None:
    """Clone/fetch the configured git source and check out its pinned ref.

    Resolves the active source via the 4-layer precedence (CLI ``--source``
    > ``SETFORGE_SOURCE`` env > host-local ``local.yaml`` > CWD-fallback).
    For a :class:`setforge.source.PathSource` this is a no-op. For a
    :class:`setforge.source.GitSource`: (1) clone to ``clone_dest`` if
    missing; (2) fetch ``origin``; (3) verify ``tracked/`` is clean
    (refuses to clobber user edits); (4) check out the pinned ``ref``
    (branch or SHA; default ``main``). Auth delegates to the user's
    git/SSH/credential-helper config.
    """
    resolved_source = source_mod.get_resolved_source()
    msg = source_mod.fetch_source(resolved_source)
    typer.echo(msg)
