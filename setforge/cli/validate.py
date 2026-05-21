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
from typing import Final

import typer
from jinja2 import StrictUndefined, Template, TemplateSyntaxError, UndefinedError
from pydantic import ValidationError

# ruamel.yaml ships py.typed without resolvable annotations; mirrors the
# pragma used in setforge.config and setforge.binaries.
from ruamel.yaml import YAML  # type: ignore[import-not-found]
from ruamel.yaml.error import YAMLError  # type: ignore[import-not-found]

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
    Profile,
    ResolvedProfile,
    TrackedFile,
    apply_local_overlay,
    apply_preserve_user_keys_overlay,
    load_config,
    resolve_profile,
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
from setforge.local_overlay import LocalOverlayError
from setforge.paths import template_context
from setforge.preserved_keys import PreserveUserKeysOverlayError
from setforge.source import (
    Source,
    load_local_host_local_sections,
    validate_host_local_sections_file_type,
)

_LOCAL_YAML_TOP_KEYS: Final[tuple[str, ...]] = (
    "source",
    "binaries",
    "claude",
    "tracked_files",
    "plugins",
    "extensions",
    "marketplaces",
    "orphan_ignore",
)
"""Known top-level keys in ``local.yaml``.

Mirrors the keys consumed by :mod:`setforge.source` (``source:``,
``tracked_files:``, ``plugins:``, ``extensions:``, ``marketplaces:``)
and :mod:`setforge.binaries` (``binaries:``, ``claude:``,
``orphan_ignore:``). Used as the close-match candidate list for
typo'd top-level keys (mockup D).
"""



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

    # Check 1b (setforge-lgvp): apply the local.yaml preserve_user_keys
    # overlay so collision (add ∩ remove) and unknown-remove errors
    # surface during ``setforge validate``. Without this, the errors
    # only fire on ``install`` / ``compare`` (cf. setforge/cli/install.py
    # and setforge/cli/compare.py). Catch the overlay-specific
    # ConfigError subclass and append to ``failures`` so the existing
    # echo path renders the canonical "in both add and remove" /
    # "not in profile chain" phrases that the e2e suite keys on.
    try:
        apply_preserve_user_keys_overlay(cfg, prof_name)
    except PreserveUserKeysOverlayError as exc:
        failures.append(f"{ctx}: {exc}")

    _check_host_local_sections(cfg, resolved, repo_root, ctx, failures)

    # Check 1c (setforge-5z11): apply the local.yaml plugin / extension
    # / marketplace overlay so its collision / unknown-remove and
    # marketplace cross-ref errors surface at validate time too.
    # Mirrors Check 1b — the install path runs the same applier; the
    # validate path is a defensive offline backstop per SPEC 2 Q8.
    try:
        apply_local_overlay(cfg, resolved, prof_name)
    except LocalOverlayError as exc:
        failures.append(f"{ctx}: {exc}")
    except ConfigError as exc:
        # The marketplace cross-ref check raises a bare ConfigError;
        # surface it under the same {ctx} prefix as the overlay errors
        # so the validate report-all-then-refuse contract holds.
        failures.append(f"{ctx}: {exc}")

    for tracked_file_name in resolved.tracked_files:
        tracked_file = cfg.tracked_files[tracked_file_name]
        dot_ctx = f"{ctx}: tracked_file {tracked_file_name!r}"
        if not _check_jinja_templates(tracked_file, dot_ctx, failures):
            continue
        _check_tracked_srcs(tracked_file, repo_root, dot_ctx, failures)

    _check_extension_includes(cfg, prof_name, ctx, failures)
    _check_claude_plugins(cfg, prof_name, ctx, failures)
    _check_marketplaces(cfg, resolved, ctx, failures)


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
    """
    try:
        overlay = load_local_host_local_sections()
    except ConfigError as exc:
        failures.append(f"{ctx}: {exc}")
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
    try:
        _LocalConfig.model_validate(dict(data))
    except ValidationError as exc:
        for err in exc.errors():
            failures.append(
                _validation_error_to_context(local_yaml_path, raw_text, data, err)
            )


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
    map the field path to a source line/column. Builds a 1-3-line
    snippet from ``raw_text`` around the offending line. When the
    error is the top-level ``extra_forbidden`` shape, the offending
    key itself is the ``field_value`` and we consult the close-match
    suggester against :data:`_LOCAL_YAML_TOP_KEYS`.
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

    Falls back to (1, 1, "", None) when the loc can't be resolved on
    the ``.lc`` table — including nested ``extra_forbidden`` errors
    (``len(loc) > 1``) whose offending site sits inside a nested
    CommentedMap. Walking arbitrary nested ``.lc`` tables to surface
    accurate nested line/columns is out of scope for setforge-tmln (the
    A6 overlay-classes spec deferred to a follow-up bd covers the full
    nested-shape UX); bailing early keeps the close-match suggestion
    against ``_LOCAL_YAML_TOP_KEYS`` from mis-firing for non-top-level
    keys.
    """
    if not isinstance(loc, tuple) or not loc:
        return 1, 1, "", None
    # Nested errors (e.g. ``loc=('source','unknown_subkey')`` for an
    # extra_forbidden inside the ``source:`` block) can't be located on
    # the top-level CommentedMap and the top-level close-match candidate
    # list does not apply. Bail to the (1, 1) fallback per the docstring
    # contract; full nested-shape UX is the A6 follow-up bd.
    if len(loc) > 1:
        return 1, 1, "", None
    head = str(loc[0])
    # For ``extra_forbidden`` at the top level, Pydantic puts the
    # offending key in ``loc`` (e.g. ``('unknown_key',)``); mockup D's
    # close-match path kicks in here.
    if err_type == "extra_forbidden":
        line_1, col_1 = _lookup_key_position(data, head)
        suggestion = suggest_close_match(head, list(_LOCAL_YAML_TOP_KEYS))
        return line_1, col_1, head, suggestion
    # Other error types: position points at the value, not the key.
    line_1, col_1 = _lookup_value_position(data, head)
    field_value = _stringify_field_value(data, head, msg)
    return line_1, col_1, field_value, None


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
    """
    home_path = _home_relative(local_yaml_path)
    if err_type == "extra_forbidden":
        return (
            f"edit {home_path}:{line_1} — unknown key {field_value!r} "
            "(remove or rename to a known top-level key)"
        )
    return f"edit {home_path}:{line_1} — {msg}"


def _route_setforge_yaml_validation_error(
    config_path: Path,
    exc: ValidationError,
    failures: list[ValidationErrorWithContext | str],
) -> None:
    """Route ``ValidationError`` from ``load_config`` through tmln formatters.

    Re-loads ``setforge.yaml`` with ``YAML(typ="rt")`` (lazy — only when
    a ValidationError fired) so the resulting ``CommentedMap`` carries
    ``.lc`` line/column info for each error's ``loc`` path. Each
    Pydantic error becomes one ``ValidationErrorWithContext`` carrier
    appended to ``failures``; the caller renders them through the
    existing :func:`_render_failures` mechanism (setforge-5twm).

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
    """Convert one Pydantic error from ``load_config`` to a tmln carrier.

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

        TODO: superseded by setforge-b1lg's nested ``.lc`` walker
        extension — b1lg will unify this with
        :func:`_resolve_error_position` (local.yaml side). The local
        port here keeps 5twm shippable in Wave 1.
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

    TODO: superseded by setforge-b1lg's nested ``.lc`` walker — this
    local port handles ``profiles.<name>.<key>`` and
    ``tracked_files.<id>.<key>`` shapes (the common cases for 5twm
    acceptance) only.
    """
    # Walk down to the parent of the leaf so we can call
    # ``.lc.key(leaf)`` on it. Only mapping shapes are exercised today;
    # integer-keyed list traversal (e.g. ``loc=('profiles', 'p',
    # 'extensions', 'include', 0)``) is not yet wired up and returns
    # the ``(1, 1, '', None)`` fallback. setforge-b1lg's unified
    # ``.lc`` walker will extend this to CommentedSeq subscripts.
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
    # ValidationError → tmln close-match UX (setforge-5twm); SetforgeError
    # (cycle / missing-profile / file-not-found / etc.) keeps its existing
    # bail-on-first routing — these are cross-field violations that don't
    # have a useful "Did you mean" suggestion path.
    try:
        cfg = load_config(config)
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

    # Check 7 (setforge-tmln): host-local local.yaml schema + parse errors
    # with mockup-D UX. Collect into the same failures list so the
    # report-all-then-refuse contract holds across all check categories.
    _check_local_yaml(_LOCAL_CONFIG_PATH, failures)

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
