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
from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from setforge import jsonc
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
    OrphanOverlayClass,
    Profile,
    ResolvedProfile,
    TrackedFile,
    _fold_overlay_spans,
    apply_local_overlay,
    collect_orphan_overlays,
    load_config,
    resolve_profile,
)
from setforge.disposition_merge import (
    _load_structural,
    is_structural,
    validate_structural_spans,
)
from setforge.errors import (
    AnchorAmbiguousError,
    AnchorNotFoundError,
    ConfigError,
    SetforgeError,
    ValidationErrorWithContext,
)
from setforge.host_local_inject import resolve_anchor
from setforge.local_config import LocalConfig as _LocalConfig
from setforge.local_overlay import LocalOverlayError, LocalOverlayLoadError
from setforge.markdown_spans import bound_span
from setforge.migrations._local_yaml import guard_local_yaml_schema
from setforge.paths import template_context
from setforge.source import (
    ExtensionOverlay,
    HostLocalSection,
    MarketplaceOverlay,
    PluginOverlay,
    _LocalTrackedFileOverlay,
    load_local_host_local_sections,
    validate_host_local_sections_file_type,
)
from setforge.spans import (
    SpanEntry,
    SpanKind,
    is_heading_anchor,
    validate_spans_file_type,
)
from setforge.structural_merge import (
    get_at_path,
    list_keys_at_path,
    resolve_path_prefix,
)


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

    resolved = _check_profile_resolution(cfg, prof_name, ctx, failures)
    if resolved is None:
        return

    _check_host_local_sections(cfg, resolved, repo_root, ctx, failures)

    _check_spans_file_types(cfg, resolved, repo_root, ctx, failures)

    _check_spans_path_existence(cfg, resolved, repo_root, ctx, failures)

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

    _check_extension_includes(cfg, prof_name, ctx, failures)
    _check_claude_plugins(cfg, prof_name, ctx, failures)
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
    over the mutated ``resolved.claude_plugins`` set as its final step.
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
      ``apply_preserve_user_keys_overlay`` path does NOT wrap) and
      :func:`_check_host_local_sections` (whose
      ``load_local_host_local_sections`` does NOT wrap either).
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
    ctx: str,
    failures: list[ValidationErrorWithContext | str],
) -> ResolvedProfile | None:
    """Check 2: resolve profile (covers missing profiles + cycle detection)."""
    try:
        return resolve_profile(cfg, prof_name)
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


def _check_host_local_sections(
    cfg: Config,
    resolved: ResolvedProfile,
    repo_root: Path,
    ctx: str,
    failures: list[ValidationErrorWithContext | str],
) -> None:
    """Check: local.yaml host_local_sections validates against tracked srcs.

    Two layers:

    1. **File-type gate**: REJECT host_local_sections for non-markdown
       tracked_files (anchor grammar requires .md / .markdown).
    2. **Anchor resolution gate**: resolve every anchor against the
       current tracked source on disk. Surfaces anchor-not-found /
       anchor-ambiguous BEFORE install would attempt the splice
       (offline gate per SPEC 1 acceptance commands).

    No-op when local.yaml is absent or declares no host-local sections
    for tracked_files in this profile.

    Catches mirror ``_check_profile``'s broadened set:
    ValidationError gets routed through the mockup-D formatter so the
    suggestion / snippet / fix-hint UX surfaces; ConfigError stays a
    string failure (existing UX); OSError / UnicodeDecodeError ride
    through the YAML PARSE category. Without these, a malformed
    local.yaml that already had its overlay-apply error reported
    above would still abort the validate run here.
    """
    try:
        overlay = load_local_host_local_sections()
    except ConfigError as exc:
        failures.append(f"{ctx}: {exc}")
        return
    except ValidationError as exc:
        _route_local_yaml_validation_error(_LOCAL_CONFIG_PATH, exc, failures)
        return
    except (OSError, UnicodeDecodeError) as exc:
        failures.append(format_yaml_parse_error(_LOCAL_CONFIG_PATH, 1, 1, str(exc)))
        return
    profile_ids = set(resolved.tracked_files)
    for tf_id, sections_map in overlay.items():
        if tf_id not in profile_ids:
            continue
        tracked_file = cfg.tracked_files[tf_id]
        src = resolve_src(tracked_file, repo_root)
        try:
            validate_host_local_sections_file_type(tf_id, len(sections_map), src)
        except ConfigError as exc:
            failures.append(f"{ctx}: tracked_file {tf_id!r}: {exc}")
            continue
        if not src.exists():
            # _check_tracked_srcs surfaces the missing-src error
            # elsewhere; do not double-report here.
            continue
        text = src.read_text(encoding="utf-8")
        for section_name, section in sections_map.items():
            try:
                resolve_anchor(text, section.anchor)
            except (AnchorNotFoundError, AnchorAmbiguousError) as exc:
                failures.append(
                    f"{ctx}: tracked_file {tf_id!r}: "
                    f"host_local_sections.{section_name}: {exc}"
                )


def _check_spans_file_types(
    cfg: Config,
    resolved: ResolvedProfile,
    repo_root: Path,
    ctx: str,
    failures: list[ValidationErrorWithContext | str],
) -> None:
    """Check: tracked_file span anchors match their source's file type.

    Mirrors the file-type gate of :func:`_check_host_local_sections` (and
    the install-time :func:`setforge.cli._install_helpers._validate_span_file_types`)
    so ``setforge validate --all`` — the offline CI gate — catches a
    wrong-grammar span anchor (a heading anchor on yaml/json, or a dotted-path
    anchor on markdown) BEFORE install would fail with a confusing runtime
    relocation / re-assert miss.

    For STRUCTURAL tracked_files it additionally runs the structural-span
    integrity guards (:func:`setforge.disposition_merge.validate_structural_spans`
    — list-index rejection per Invariant I10 + overlap/nesting rejection per
    Invariant I11). These otherwise fire only at merge / install time
    (a :class:`~setforge.errors.ConfigError` mid-install), so a config with an
    ``a[*]`` index anchor or overlapping pins would pass ``validate`` clean and
    then abort install; surfacing them here keeps the offline gate complete.

    Routes every :class:`~setforge.errors.ConfigError` from
    :func:`setforge.spans.validate_spans_file_type` /
    :func:`setforge.disposition_merge.validate_structural_spans` to a string
    failure (existing UX). No-op for tracked_files without spans.
    """
    for tf_id in resolved.tracked_files:
        tracked_file = cfg.tracked_files[tf_id]
        if not tracked_file.spans:
            continue
        src = resolve_src(tracked_file, repo_root)
        try:
            validate_spans_file_type(tf_id, tracked_file.spans, src)
            if is_structural(src):
                validate_structural_spans(list(tracked_file.spans))
        except ConfigError as exc:
            failures.append(f"{ctx}: tracked_file {tf_id!r}: {exc}")


def _check_spans_path_existence(
    cfg: Config,
    resolved: ResolvedProfile,
    repo_root: Path,
    ctx: str,
    failures: list[ValidationErrorWithContext | str],
) -> None:
    """Check: every structural span's dotted path exists in its tracked src.

    A PINNED or FORKED span whose dotted path no longer resolves in the
    tracked source is a silent value leak: ``sync`` would absorb host
    values into the config repo and ``install`` would lose them, with no
    error on either path. This offline gate names every dead span path —
    kind-agnostic (the leak is identical for pinned and forked) — with the
    FIRST MISSING PREFIX segment so the fix is obvious, via
    :func:`setforge.structural_merge.resolve_path_prefix` (the diagnostics
    sibling of :func:`~setforge.structural_merge.get_at_path`).

    A FORKED span that resolves to a non-scalar (mapping or list) fails
    with a distinct row: forked spans take a scalar path (the scalar
    three-way merge refuses every non-scalar operand, so a forked subtree
    or list would silently degrade to whole-replace at merge time).
    PINNED subtrees stay legal (whole-replace re-assert).

    Scope and suppression rules:

    * The checked span list is the local.yaml-overlay-FOLDED view
      (:func:`_overlay_folded_spans`) — host-local span declarations are
      validated exactly like tracked-side ones, on a local copy so
      ``--all`` never double-applies the fold.
    * Reads ONLY the tracked src (plus the local.yaml overlay block, which
      is config, not host state) — never the base store, spans sidecar, or
      any live file. ``validate`` stays a stateless offline gate.
    * Missing src → silent skip (:func:`_check_tracked_srcs` reports it;
      same double-report suppression as :func:`_check_host_local_sections`).
    * Non-structural (markdown) srcs route to
      :func:`_check_markdown_span_anchors` (exact heading resolution);
      heading-shaped anchors on structural srcs → skip. OVERLAY spans
      carry an identity, not a path → skip.
    * Unparseable structural src → exactly ONE failure row for the file,
      then continue with the remaining tracked_files (report-all contract).
    * List-suffix anchors (``[*]`` / ``[]``) → skip;
      :func:`~setforge.disposition_merge.validate_structural_spans` already
      rejects them (Invariant I10) in :func:`_check_spans_file_types`.
    """
    try:
        overlays = source_mod.load_local_tracked_file_overlays(
            source_mod.LOCAL_CONFIG_PATH
        )
    except (ConfigError, ValidationError, OSError):
        # A malformed local.yaml is reported by Check 7 (_check_local_yaml);
        # fold nothing here rather than double-report.
        overlays = {}
    for tf_id in resolved.tracked_files:
        tracked_file = cfg.tracked_files[tf_id]
        spans = _overlay_folded_spans(tf_id, tracked_file, overlays)
        if not spans:
            continue
        src = resolve_src(tracked_file, repo_root)
        if not src.exists():
            # _check_tracked_srcs surfaces the missing-src error
            # elsewhere; do not double-report here.
            continue
        if not is_structural(src):
            _check_markdown_span_anchors(spans, src, tf_id, ctx, failures)
            continue
        try:
            model = _load_structural(
                src.read_text(encoding="utf-8"), jsonc.is_jsonc_file(src)
            )
        except Exception as exc:
            # Broad on purpose: parser errors are library-specific
            # (json-five / ruamel raise disjoint hierarchies); any failure
            # to load means the same thing here — an unparseable src.
            failures.append(f"{ctx}: tracked_file {tf_id!r}: unparseable src: {exc}")
            continue
        for span in spans:
            if span.kind is SpanKind.OVERLAY or is_heading_anchor(span.anchor):
                continue
            _check_span_path(span, model, tf_id, ctx, failures)


def _overlay_folded_spans(
    tf_id: str,
    tracked_file: TrackedFile,
    overlays: dict[str, _LocalTrackedFileOverlay],
) -> list[SpanEntry]:
    """Return ``tracked_file.spans`` with the local.yaml overlay folded.

    The fold lands on a LOCAL list, never mutating ``cfg`` —
    ``validate --all`` iterates profiles over ONE loaded :class:`Config`,
    so the in-place fold :func:`setforge.config.apply_host_local_tracked_file_overrides`
    performs at install time would double-apply on the second profile.
    Reuses :func:`setforge.config._fold_overlay_spans` (host-local wins
    each anchor) and re-validates the merged dicts through
    :class:`~setforge.spans.SpanEntry`, like the install-time fold (which
    revalidates at the whole-:class:`TrackedFile` level instead). Each
    merged dict is the dump of an already-validated span, so the
    revalidation is a guard, not an expected failure path.
    """
    overlay = overlays.get(tf_id)
    if overlay is None or not overlay.spans:
        return list(tracked_file.spans)
    merged = _fold_overlay_spans(
        tf_id=tf_id,
        tracked_spans=tracked_file.spans,
        overlay_spans=overlay.spans,
        prefer_shared_anchors=frozenset(),
    )
    return [SpanEntry.model_validate(d) for d in merged]


def _check_markdown_span_anchors(
    spans: list[SpanEntry],
    src: Path,
    tf_id: str,
    ctx: str,
    failures: list[ValidationErrorWithContext | str],
) -> None:
    """Resolve PINNED / FORKED markdown span headings against the tracked src.

    The markdown counterpart of the structural dotted-path walk: each
    heading anchor must resolve EXACTLY ONCE in the tracked source, offline
    (mirroring :func:`_check_host_local_sections`'s anchor-resolution gate).
    ABSENT and AMBIGUOUS surface as distinct rows —
    :func:`setforge.markdown_spans.bound_span`'s two error types carry
    distinct messages. No fuzzy relocation here; that is install's job.
    OVERLAY spans are skipped (their body lives in local.yaml; the
    dead-anchor leak this check guards against does not apply). An
    unreadable src yields exactly ONE row, mirroring the structural
    unparseable-src contract.
    """
    md_spans = [
        s
        for s in spans
        if s.kind is not SpanKind.OVERLAY and is_heading_anchor(s.anchor)
    ]
    if not md_spans:
        return
    try:
        text = src.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        failures.append(f"{ctx}: tracked_file {tf_id!r}: unreadable src: {exc}")
        return
    for span in md_spans:
        try:
            bound_span(text, span.anchor)
        except (AnchorNotFoundError, AnchorAmbiguousError) as exc:
            failures.append(
                f"{ctx}: tracked_file {tf_id!r}: {span.kind.value} span "
                f"{span.anchor!r}: {exc}"
            )


def _check_span_path(
    span: SpanEntry,
    model: object,
    tf_id: str,
    ctx: str,
    failures: list[ValidationErrorWithContext | str],
) -> None:
    """Resolve one PINNED / FORKED span's dotted path against ``model``.

    Appends at most one failure row: path-not-found (with the first missing
    prefix, plus a did-you-mean built from the sibling keys at the deepest
    resolvable prefix when one is close enough) or
    forked-span-on-a-non-scalar. See :func:`_check_spans_path_existence`
    for the full contract.
    """
    try:
        resolved_prefix, missing = resolve_path_prefix(model, span.anchor)
    except ValueError:
        # List-suffix anchor: validate_structural_spans already
        # reported it (I10); do not double-report here.
        return
    if missing is not None:
        row = (
            f"{ctx}: tracked_file {tf_id!r}: {span.kind.value} span "
            f"{span.anchor!r}: path not found (missing at {missing!r}); "
            f"add the key to the tracked src or remove the span"
        )
        leaf = missing.rsplit(".", 1)[-1]
        match = suggest_close_match(leaf, list_keys_at_path(model, resolved_prefix))
        if match is not None:
            row += f" (did you mean {match!r}?)"
        failures.append(row)
        return
    if span.kind is not SpanKind.FORKED:
        return
    value = get_at_path(model, span.anchor)
    if isinstance(value, Mapping | list):
        shape = "mapping" if isinstance(value, Mapping) else "list"
        failures.append(
            f"{ctx}: tracked_file {tf_id!r}: forked span "
            f"{span.anchor!r}: path resolves to a {shape}; "
            f"forked spans take a scalar path"
        )


def _check_extension_includes(
    cfg: Config,
    prof_name: str,
    ctx: str,
    failures: list[ValidationErrorWithContext | str],
) -> None:
    """Check 5: extension include list — non-empty IDs, no duplicates.

    Walks the raw profile (before extends-merging) so duplicates that
    ``_merge_list`` would silently drop are still caught.
    """
    raw_include = cfg.profiles[prof_name].extensions.include
    _check_dedup(
        raw_include,
        ctx=ctx,
        failures=failures,
        empty_msg="extensions.include contains empty ID",
        dup_label="extensions.include duplicate",
    )


def _check_claude_plugins(
    cfg: Config,
    prof_name: str,
    ctx: str,
    failures: list[ValidationErrorWithContext | str],
) -> None:
    """Check 5b: claude_plugins list — non-empty refs, no duplicates.

    Same raw-profile rationale as Check 5: ``_merge_list`` dedupes during
    ``resolve_profile``, so duplicates would be silently swallowed by the
    resolved list. Walk the raw list to catch them at config time.
    """
    raw_plugins = cfg.profiles[prof_name].claude_plugins
    _check_dedup(
        raw_plugins,
        ctx=ctx,
        failures=failures,
        empty_msg="claude_plugins contains empty ref",
        dup_label="claude_plugins duplicate",
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
    for plugin_ref in resolved.claude_plugins:
        bare_name = plugin_ref.split("@")[0]
        if bare_name in cfg.claude_plugins:
            mp_name = cfg.claude_plugins[bare_name].marketplace
            if mp_name not in marketplace_keys:
                failures.append(
                    f"{ctx}: plugin {bare_name!r} references unknown "
                    f"marketplace {mp_name!r}"
                )


def _route_local_yaml_validation_error(
    local_yaml_path: Path,
    exc: ValidationError,
    failures: list[ValidationErrorWithContext | str],
) -> None:
    """Route a ``ValidationError`` raised by the local.yaml overlay loader.

    Sibling of :func:`_route_setforge_yaml_validation_error` for the
    local.yaml side. Re-loads local.yaml with
    ``YAML(typ="rt")`` so the resulting ``CommentedMap`` carries
    ``.lc`` line/column info for each error's ``loc`` path. Each
    Pydantic error becomes one :class:`ValidationErrorWithContext`
    carrier appended to ``failures`` via
    :func:`_validation_error_to_context`.

    Race-window resilience mirrors the setforge.yaml-side routing:
    when the rt re-read fails (file became unreadable / unparseable
    between the overlay load and this routing) or returns a non-Mapping
    root, fall back to top-level ``(1, 1)`` placeholders so the
    original :class:`ValidationError` still surfaces.
    """
    try:
        raw_text = local_yaml_path.read_text(encoding="utf-8")
        yaml_rt = YAML(typ="rt")
        data = yaml_rt.load(raw_text)
    except (OSError, UnicodeDecodeError, YAMLError):
        # Either the re-read failed (race window) or the file became
        # syntactically invalid between the overlay-loader parse and
        # this routing. Fall back to a top-level placeholder so the
        # original ValidationError still surfaces.
        for err in exc.errors():
            failures.append(_build_local_yaml_top_level_fallback(local_yaml_path, err))
        return
    if data is None or not isinstance(data, Mapping):
        # Empty document or malformed top-level shape — placeholder
        # carriers preserve the report-all-then-refuse contract.
        for err in exc.errors():
            failures.append(_build_local_yaml_top_level_fallback(local_yaml_path, err))
        return
    for err in exc.errors():
        failures.append(
            _validation_error_to_context(local_yaml_path, raw_text, data, err)
        )


def _build_local_yaml_top_level_fallback(
    local_yaml_path: Path, err: Mapping[str, object]
) -> ValidationErrorWithContext:
    """Fallback carrier when the rt re-load fails for local.yaml.

    Surfaces the Pydantic message at ``(1, 1)`` without snippet/pointer
    detail. Mirrors :func:`_build_top_level_fallback` on the
    setforge.yaml side but renders the home-relative path.
    """
    msg = str(err.get("msg", ""))
    home_path = _home_relative(local_yaml_path)
    return ValidationErrorWithContext(
        file_path=local_yaml_path,
        line=1,
        column=1,
        snippet_lines=[""],
        field_value=msg or "value",
        fix_hint=f"edit {home_path} — {msg}",
        suggestion=None,
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
    try:
        _LocalConfig.model_validate(dict(data))
    except ValidationError as exc:
        for err in exc.errors():
            failures.append(
                _validation_error_to_context(local_yaml_path, raw_text, data, err)
            )
    _check_local_yaml_tracked_files(local_yaml_path, raw_text, data, failures)


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


def _check_orphan_overlays(
    cfg: Config,
    profiles_to_check: list[str],
    local_yaml_path: Path,
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
            resolved = resolve_profile(cfg, prof_name)
        except SetforgeError:
            # A broken profile chain is already surfaced by
            # _check_profile_resolution; skip the orphan pass for it.
            continue
        in_some_profile.update(resolved.tracked_files)
        try:
            orphans = collect_orphan_overlays(
                cfg, resolved, local_config_path=local_yaml_path
            )
        except (SetforgeError, ValidationError, OSError, UnicodeDecodeError):
            # A malformed / unparseable / unreadable / schema-mismatched
            # local.yaml is already reported by _check_local_yaml (the
            # dedicated local.yaml pass). The orphan classifier re-parses
            # the same file; swallow its load failure here rather than
            # aborting the whole validate run before report-all-then-refuse
            # completes.
            return []
        for orphan in orphans:
            if orphan.class_ is OrphanOverlayClass.UNKNOWN:
                if orphan.id not in seen_unknown:
                    seen_unknown.add(orphan.id)
                    unknown_ids.append(orphan.id)
            elif orphan.id not in seen_off:
                seen_off.add(orphan.id)
                off_profile_ids.append(orphan.id)

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
    - ``('tracked_files', <id>, 'host_local_sections', ...)``
      → :class:`HostLocalSection.model_fields` (sub-block keys).
    - ``('tracked_files', <id>, ...)`` (other) →
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
            if len(loc) >= 3 and loc[2] == "host_local_sections":
                return list(HostLocalSection.model_fields.keys())
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
        None, "--profile", help="Validate a specific profile."
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
        cfg, profiles_to_check, _LOCAL_CONFIG_PATH, failures
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
