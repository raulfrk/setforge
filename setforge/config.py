"""Typed configuration schema for setforge.

Pydantic models validate ``setforge.yaml`` and provide the in-memory
contract used by every subcommand. YAML is loaded via ruamel.yaml in
round-trip mode so comments and key order survive subsequent capture
writes that re-serialize the document.
"""

import copy
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    field_validator,
    model_validator,
)
from ruamel.yaml import YAML
from ruamel.yaml.scalarint import OctalInt, ScalarInt

from setforge.errors import ConfigError, ProfileNotFound
from setforge.local_overlay import (
    LocalOverlayError,
    LocalOverlayLoadError,
    ResolvedExtension,
    ResolvedMarketplace,
    ResolvedPlugin,
    resolve_extension_overlay,
    resolve_marketplace_overlay,
    resolve_plugin_overlay,
)
from setforge.migrations import (
    _meets_floor,
    current_expected_schema_version,
    parse_schema_version,
)
from setforge.section_mode import SectionMode as SectionMode
from setforge.spans import SpanEntry, SpanSemantics

if TYPE_CHECKING:
    from setforge.source import (
        ExtensionOverlay,
        MarketplaceOverlay,
        PluginOverlay,
    )

_STRICT = ConfigDict(extra="forbid")
"""Strict model config for the ``setforge.yaml`` schema.

The models stay ``extra="forbid"`` so ``setforge validate`` keeps its
strict typo detection (unknown keys → schema error + "did you mean"
suggestion). Forward-tolerance for the RUNTIME load path is provided one
layer up, in :func:`load_config`: it warns about and STRIPS unknown keys
(``tolerate_unknown=True``, the default) before validating, so a config
from a newer same-major engine still loads. ``validate`` opts out
(``tolerate_unknown=False``) to surface those unknowns as errors instead.
Cross-major refusal is handled by :func:`_guard_schema_version`.
"""

_FORBIDDEN_PATH_CHARS = frozenset(chr(c) for c in range(32)) | frozenset({"\x7f"})


class ReconcilePolicy(StrEnum):
    ADDITIVE = "additive"
    PRUNE = "prune"
    REPORT = "report"


class MarketplaceSourceKind(StrEnum):
    GITHUB = "github"
    PATH = "path"


class ClaudeInstallMode(StrEnum):
    """How ``setforge install`` resolves Claude marketplaces.

    ``REGULAR`` (default): pass marketplace sources to the ``claude`` CLI
    as-is, which fetches GitHub repos over the network on first install.

    ``LOCAL_CLONE``: swap each GitHub-backed ``MarketplaceSource`` to a
    PATH source pointing at a local cache under
    ``~/.cache/setforge/marketplaces/<name>/`` before the
    ``claude plugin marketplace add`` call. Enables offline operation on
    hosts where Claude's marketplace fetch would fail.
    """

    REGULAR = "regular"
    LOCAL_CLONE = "local-clone"


class Disposition(StrEnum):
    """How a tracked file is reconciled under the stored-base 3-way model.

    ``shared`` 3-way merges and captures live edits back to tracked;
    ``forked`` 3-way merges but never captures back; ``pinned`` is never
    merged or captured (the live copy is authoritative — today's
    "host-local", renamed). ``None`` on a tracked file keeps the legacy
    2-way preserve behavior unchanged.
    """

    SHARED = "shared"
    FORKED = "forked"
    PINNED = "pinned"


# SectionMode now lives in the leaf module setforge.section_mode so
# setforge.spans (imported by config below) can carry it on
# SpanEntry.capture_mode without a spans → config → spans import cycle.
# Re-exported here for back-compat: ``from setforge.config import SectionMode``
# stays valid for every existing call site.


def _check_yaml_octal_mode(value: object, source_label: str) -> int | None:
    """Validate a ``mode`` value's TYPE shape; return plain ``int`` or ``None``.

    The bool/ScalarInt/OctalInt cascade shared by
    :func:`TrackedFile._validate_mode` and
    :func:`setforge.source._LocalTrackedFileOverlay._validate_mode_octal_only`.
    ``source_label`` prefixes every reject message so each consumer keeps
    its pre-extraction error text (``"mode"`` for :class:`TrackedFile`;
    the class-qualified backticked label for the overlay).

    ruamel.yaml round-trip semantics for the value before Pydantic
    sees it:

    - ``mode: 0o755`` -> :class:`OctalInt(493)` (the intended form).
    - ``mode: 0755``  -> :class:`ScalarInt(755)` (NOT 0o755! The
      leading zero is silently stripped under YAML 1.2 — a
      well-known footgun for users migrating from YAML 1.1).
    - ``mode: "0755"`` -> ``str("0755")``.
    - ``mode: 755`` -> plain ``int(755)`` (decimal — almost
      certainly a typo; 755 = 0o1363, not 0o755).

    Accepts :class:`OctalInt` (the canonical form) and the exact
    ``int`` type (a Pydantic-caller passing the Python literal
    ``0o755`` == 493 — same value, different provenance). Every other
    shape — including :class:`ScalarInt` subclasses that are NOT
    :class:`OctalInt`, ``str``, ``bool`` — is rejected with a message
    pointing at ``0o755``. ``bool`` deserves special mention: Python's
    ``isinstance(True, int)`` is True, so without an explicit check
    ``mode: true`` would silently mean ``0o1``.

    Range and setuid/setgid policy are NOT enforced here — each
    consumer applies its own bounds with its own message
    (:func:`TrackedFile._validate_mode` inline; the overlay in its
    ``model_validator``).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(
            f"{source_label} must be YAML-1.2 octal int literal (e.g. 0o755), "
            f"not bool. Got: {value!r}"
        )
    if isinstance(value, ScalarInt) and not isinstance(value, OctalInt):
        raise ValueError(
            f"{source_label} {int(value)} appears to use YAML-1.1-style "
            f"leading-zero octal (e.g. 0755) which YAML 1.2 silently parses "
            f"as decimal. If you meant the permission bits commonly written "
            f"as 'octal 755', use the YAML-1.2 literal 0o755. If you "
            f"literally meant the integer {int(value)}, use 0o{int(value):o}."
        )
    if type(value) is not int and not isinstance(value, OctalInt):
        raise ValueError(
            f"{source_label} must be a YAML-1.2 octal int literal "
            f"(e.g. 0o755); strings, floats, and other types are rejected. "
            f"Got: {value!r}"
        )
    return int(value)


class TrackedFile(BaseModel):
    model_config = _STRICT

    src: Path
    dst: str
    template: bool = False
    mode: int | None = None
    """POSIX file-mode bits (chmod) for the live dst.

    YAML-1.2 octal int literal only (``mode: 0o755``). The validator
    rejects both bare strings and YAML-1.1-style ``0755`` literals,
    which ruamel.yaml parses as the string ``"0755"`` under YAML 1.2.
    Setuid/setgid bits are refused for security; sticky bit (``0o1000``)
    is allowed. When ``None``, deploy preserves the source file's mode
    (today's behavior, zero regression).
    """
    symlink: str | None = None
    """When set, deploy creates a symbolic link at ``dst`` whose target is
    this raw user string (e.g. ``~/.config/foo/bar``); the tracked
    content is written to ``Path(symlink).expanduser()`` so the target
    actually carries the deployed bytes.

    The string is stored *verbatim* (no :func:`Path.expanduser`,
    no :func:`Path.resolve`) so the on-disk symlink target survives
    cross-host portability: ``~/foo`` remains ``~/foo`` in
    ``os.readlink(dst)`` rather than baking in ``/home/<user>/foo``.

    The model validator refuses a self-loop where the (expanded)
    target equals the (expanded) ``dst`` — config-time guard against
    a tracked_file pointing at itself.
    """
    disposition: Disposition | None = None
    """File-level reconciliation policy (opt-in).

    ``None`` ⇒ the file is deployed from tracked verbatim (no stored-base
    merge). When set, the file is reconciled by the stored-base 3-way
    merge per :class:`Disposition`. Sub-file preservation is expressed via
    :attr:`spans` (the schema-2.0 unified span model that superseded the
    legacy ``preserve_*`` family).
    """
    spans: list[SpanEntry] = []
    """Shared (tracked-side) sub-file span intents (pinned / forked regions).

    The tracked-side counterpart of
    :attr:`setforge.source._LocalTrackedFileOverlay.spans`. Carries
    ``semantics: shared`` span intents that propagate across hosts;
    host-local span intents live in ``local.yaml`` instead. Each entry is
    a :class:`~setforge.spans.SpanEntry` (markdown heading-text anchor +
    kind + semantics). Resolved offsets and baseline bytes are derived
    state in the spans sidecar, never duplicated here (Invariant I12).
    """

    @model_validator(mode="after")
    def _symlink_no_self_loop(self) -> Self:
        """Refuse ``symlink:`` whose expanded target equals expanded ``dst``.

        Cross-host portability requires the symlink string itself stay
        raw (``~/foo``, never ``/home/<user>/foo``), so the equality
        comparison happens on :func:`Path.expanduser` of both sides —
        a user who writes ``dst: ~/x`` and ``symlink: ~/x`` MUST be
        refused regardless of whether the strings are textually equal
        (``$HOME/x`` vs ``~/x`` resolve to the same path).
        """
        if self.symlink is None:
            return self
        target = Path(self.symlink).expanduser()
        dst = Path(self.dst).expanduser()
        if target == dst:
            raise ValueError(
                f"symlink target {self.symlink!r} equals dst {self.dst!r} "
                f"after expansion — refusing self-loop."
            )
        return self

    @field_validator("mode", mode="before")
    @classmethod
    def _validate_mode(cls, v: object) -> int | None:
        """Reject every shape EXCEPT YAML-1.2 octal (``0o755``) or a plain int.

        The type cascade (bool / ScalarInt / OctalInt — see
        :func:`_check_yaml_octal_mode` for the ruamel.yaml round-trip
        semantics it guards) is shared with
        :class:`setforge.source._LocalTrackedFileOverlay`; the range and
        setuid/setgid policy below stays here because the overlay
        enforces its own bounds in its ``model_validator``.
        """
        mode = _check_yaml_octal_mode(v, "mode")
        if mode is None:
            return None
        if not (0o0 <= mode <= 0o7777):
            raise ValueError(f"mode {oct(mode)} out of range 0o0..0o7777")
        if mode & 0o6000:
            raise ValueError(
                f"mode {oct(mode)} sets setuid/setgid bit — refusing for security."
            )
        return mode

    @field_validator("src", "dst", mode="before")
    @classmethod
    def _no_control_chars_in_path(cls, v: object) -> object:
        """Reject paths containing C0 control characters or DEL.

        Tab and newline corrupt unified-diff field separators; DEL
        and other C0 controls are similarly hostile to most tooling.
        Cleaner to fail at config load than to silently emit malformed
        transitions or ``patch``-rejected diffs.
        """
        s = str(v)
        bad = sorted({c for c in s if c in _FORBIDDEN_PATH_CHARS})
        if bad:
            escaped = ", ".join(f"\\x{ord(c):02x}" for c in bad)
            raise ValueError(
                f"path contains forbidden control character(s) [{escaped}]: {s!r}"
            )
        return v


class MarketplaceSource(BaseModel):
    model_config = _STRICT

    source: MarketplaceSourceKind
    repo: str | None = None
    path: Path | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "MarketplaceSource":
        if (self.repo is None) == (self.path is None):
            raise ValueError("MarketplaceSource: exactly one of repo/path required")
        return self


class ClaudePluginRef(BaseModel):
    model_config = _STRICT

    marketplace: str


class McpServerRef(BaseModel):
    """A single MCP-server registry entry referenced by a profile.

    ``command`` holds the tokens that follow the ``--`` separator on a
    ``claude mcp add --scope <scope> <name> -- <tokens...>`` invocation —
    the program plus its arguments. It is a token LIST (never a joined
    string) so the install path can pass it to ``subprocess.run`` with
    ``shell=False``; an empty list is rejected since a server with no
    command cannot be registered. ``scope`` selects the ``claude mcp add``
    ``--scope`` value and defaults to ``"user"`` (the host-wide registration
    Serena and friends use). Mirrors the shape of
    :class:`ClaudePluginRef` / :class:`MarketplaceSource`.
    """

    model_config = _STRICT

    command: list[str]
    scope: str = "user"

    @field_validator("command")
    @classmethod
    def _command_non_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("McpServerRef.command must be a non-empty token list")
        return v


class Extensions(BaseModel):
    model_config = _STRICT

    include: list[str] = []
    exclude: list[str] = []
    reconcile: ReconcilePolicy = ReconcilePolicy.ADDITIVE


class Profile(BaseModel):
    model_config = _STRICT

    extends: str | None = None
    tracked_files: list[str] = []
    extensions: Extensions = Extensions()
    claude_plugins: list[str] = []
    plugins_reconcile: ReconcilePolicy = ReconcilePolicy.ADDITIVE
    bootstrap: list[Path] = []
    mcp_servers: list[str] = []
    cargo_binaries: list[str] = []


class ResolvedProfile(BaseModel):
    """A profile with its ``extends:`` chain fully resolved.

    All list fields are flattened (parent entries first, child entries
    appended, duplicates dropped while preserving first occurrence).
    Scalar fields take the deepest explicit value in the chain.
    """

    model_config = _STRICT

    extends: None = None
    tracked_files: list[str] = []
    extensions: Extensions = Extensions()
    claude_plugins: list[str] = []
    plugins_reconcile: ReconcilePolicy = ReconcilePolicy.ADDITIVE
    bootstrap: list[Path] = []
    mcp_servers: list[str] = []
    cargo_binaries: list[str] = []


class Config(BaseModel):
    model_config = _STRICT

    version: int = 1
    schema_version: str = "1.0"
    """User-declared schema version for ``setforge migrate`` compatibility checks.

    Defaults to ``"1.0"`` when absent so every pre-versioning
    ``setforge.yaml`` continues to load unchanged. The matching value
    setforge expects for the running build lives in
    :data:`setforge.migrations.current_expected_schema_version`; the
    ``setforge migrate --check`` command compares the two and surfaces
    the chain of migrations needed when they diverge. The field is
    intentionally a free-form string (e.g. ``"1.0"``, ``"1.1"``,
    ``"2.0"``) rather than the integer ``version`` field, which
    enumerates the YAML file format itself and is owned by the engine.
    """
    minimum_version: str | None = None
    """Optional operator-declared ``MAJOR.MINOR`` schema floor.

    When set, an engine whose
    :data:`setforge.migrations.current_expected_schema_version` is BELOW this
    value hard-refuses every config-reading command (see
    :func:`_guard_schema_version`) — overriding the same-major forward-tolerance
    that :func:`load_config` otherwise grants. It is the operator's attestation
    that all hosts are upgraded, making a same-major schema contraction (e.g.
    the ``migrate --finalize`` tracked-marker strip) safe. ``None`` ⇒ no floor.

    The gate reads this value from the raw YAML mapping in
    :func:`_guard_schema_version`, BEFORE model validation, so a floor that an
    older engine would strip as an unknown key still fires on engines that know
    the field.
    """
    tracked_files: dict[str, TrackedFile]
    marketplaces: dict[str, MarketplaceSource] = {}
    claude_plugins: dict[str, ClaudePluginRef] = {}
    mcp_servers: dict[str, McpServerRef] = {}
    profiles: dict[str, Profile]


def _merge_list[T](parent: list[T], child: list[T]) -> list[T]:
    """Concatenate parent + child, preserving first-occurrence order."""
    seen: set[T] = set()
    merged: list[T] = []
    for item in (*parent, *child):
        if item in seen:
            continue
        seen.add(item)
        merged.append(item)
    return merged


def _merge_extensions(parent: Extensions, child: Extensions) -> Extensions:
    """Merge two Extensions blocks. Lists concatenate; ``reconcile``
    overrides only when explicitly set in child (per ``model_fields_set``)."""
    merged_include = _merge_list(parent.include, child.include)
    merged_exclude = _merge_list(parent.exclude, child.exclude)
    reconcile = (
        child.reconcile if "reconcile" in child.model_fields_set else parent.reconcile
    )
    return Extensions(
        include=merged_include,
        exclude=merged_exclude,
        reconcile=reconcile,
    )


def resolve_chain(config: Config, name: str) -> list[Profile]:
    """Walk ``extends:`` from leaf to root, return profiles root-first."""
    chain: list[Profile] = []
    visited: list[str] = []
    current: str | None = name
    while current is not None:
        if current in visited:
            visited.append(current)
            raise ConfigError(f"profile cycle: {' → '.join(visited)}")
        if current not in config.profiles:
            raise ProfileNotFound(f"profile not found: {current}")
        visited.append(current)
        chain.append(config.profiles[current])
        current = config.profiles[current].extends
    chain.reverse()
    return chain


def resolve_profile(config: Config, name: str) -> ResolvedProfile:
    """Walk the ``extends:`` chain and produce a fully-merged profile.

    - List fields (``tracked_files``, ``claude_plugins``, ``bootstrap``,
      ``extensions.include``, ``extensions.exclude``) are concatenated
      parent-first and deduplicated, preserving first occurrence.
    - Scalar fields (``plugins_reconcile``, ``extensions.reconcile``)
      are overridden by the child only when explicitly set in that
      child's ``model_fields_set``; otherwise they inherit.
    - A cycle in ``extends:`` raises :class:`ConfigError` with every
      profile name in the cycle.
    """
    if name not in config.profiles:
        raise ProfileNotFound(f"profile not found: {name}")
    chain = resolve_chain(config, name)

    resolved = ResolvedProfile()
    for profile in chain:
        fields_set = profile.model_fields_set
        resolved = ResolvedProfile(
            tracked_files=_merge_list(resolved.tracked_files, profile.tracked_files),
            claude_plugins=_merge_list(resolved.claude_plugins, profile.claude_plugins),
            bootstrap=_merge_list(resolved.bootstrap, profile.bootstrap),
            mcp_servers=_merge_list(resolved.mcp_servers, profile.mcp_servers),
            cargo_binaries=_merge_list(resolved.cargo_binaries, profile.cargo_binaries),
            extensions=_merge_extensions(resolved.extensions, profile.extensions),
            plugins_reconcile=(
                profile.plugins_reconcile
                if "plugins_reconcile" in fields_set
                else resolved.plugins_reconcile
            ),
        )
    return resolved


def load_config(path: Path, *, tolerate_unknown: bool = True) -> Config:
    """Parse ``setforge.yaml`` from disk and validate against the schema.

    ``tolerate_unknown`` (default ``True``) is the forward-tolerant runtime
    path: unknown keys are warned about and stripped before validation, so
    a config from a newer same-major engine still loads. ``setforge
    validate`` passes ``False`` so an unknown key surfaces as a strict
    schema error with a "did you mean" suggestion instead.

    Raises :class:`ConfigError` on file-not-found, YAML parse errors, or
    cross-field violations (e.g. profile ``claude_plugins`` referencing
    a name absent from the top-level ``claude_plugins:`` registry).
    Pydantic validation errors are propagated unchanged so the caller
    sees the full field-level message.

    When the loaded :attr:`Config.schema_version` does not match
    :data:`setforge.migrations.current_expected_schema_version`, a
    single yellow warning is written to stderr pointing the user at
    ``setforge migrate --check``. The mismatch is NOT a hard error —
    the user may have explicitly pinned an older schema via
    ``setforge migrate --pin=X.Y``; raising would block every other
    subcommand on a soft signal.
    """
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    yaml = YAML(typ="rt")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.load(fh)
    if data is None:
        raise ConfigError(f"config file is empty: {path}")
    _guard_schema_version(data, path)
    config = (
        _validate_tolerant(data) if tolerate_unknown else Config.model_validate(data)
    )
    _validate_plugin_references(config)
    _validate_mcp_references(config)
    _warn_on_schema_mismatch(config)
    return config


_RECONCILIATION_DIRECTIVE_KEYS: Final[tuple[str, ...]] = (
    "disposition",
    "spans",
)


def _has_reconciliation_directive(mapping: Mapping[str, object]) -> bool:
    """Return whether a raw tracked-file mapping declares a reconciliation directive.

    True when ``disposition`` or ``spans`` is present with a truthy value, so a
    falsy / empty value does not count. Dropping an unknown field BESIDE such a
    directive is the riskier case (it may carry merge semantics this engine does
    not implement), so the forward-tolerant strip surfaces a louder warning.
    """
    return any(bool(mapping.get(key)) for key in _RECONCILIATION_DIRECTIVE_KEYS)


def _tracked_file_mapping_for_loc(
    data: object, loc: tuple[object, ...]
) -> Mapping[str, object] | None:
    """Return the ``tracked_files.<id>`` mapping a loc points into, or None.

    A loc shaped ``("tracked_files", <id>, <key>)`` resolves to the ``<id>``
    tracked-file mapping; any other shape or a missing path returns None, so the
    caller treats it as an ordinary unknown key.
    """
    if len(loc) != 3 or loc[0] != "tracked_files":
        return None
    if not isinstance(data, Mapping):
        return None
    tracked = data.get("tracked_files")
    if not isinstance(tracked, Mapping):
        return None
    entry = tracked.get(loc[1])
    return entry if isinstance(entry, Mapping) else None


def _partition_reconciliation_adjacent(
    data: object, locs: Sequence[tuple[object, ...]]
) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    """Split stripped ``extra_forbidden`` locs into reconciliation-adjacent vs ordinary.

    A loc is reconciliation-adjacent when it removes a key from a
    ``tracked_files.<id>`` mapping that itself declares a reconciliation directive
    (:func:`_has_reconciliation_directive`); dropping an unknown field beside such
    a directive may silently discard merge semantics, so it earns the escalated
    warning. Everything else is an ordinary unknown key.
    """
    adjacent: list[tuple[object, ...]] = []
    ordinary: list[tuple[object, ...]] = []
    for loc in locs:
        tf_mapping = _tracked_file_mapping_for_loc(data, loc)
        if tf_mapping is not None and _has_reconciliation_directive(tf_mapping):
            adjacent.append(loc)
        else:
            ordinary.append(loc)
    return adjacent, ordinary


def _warn_reconciliation_adjacent_strip(fields: list[str]) -> None:
    """Warn (one line per field) when a reconciliation-adjacent unknown key is stripped.

    Louder than :func:`_warn_unknown_keys`: the dropped key sits on a tracked
    file that declares a reconciliation directive, so it may carry merge
    semantics this engine does not implement. Text is always written; only the
    color is TTY-gated, so the warning survives CliRunner / Docker e2e capture.
    """
    import sys

    color = sys.stderr.isatty()
    prefix = "\033[33mwarning:\033[0m" if color else "warning:"
    for field_path in fields:
        sys.stderr.write(
            f"{prefix} dropping unrecognized key {field_path!r} from a tracked "
            f"file that declares a reconciliation directive "
            f"(disposition/spans); it may carry merge semantics this "
            f"setforge does not implement, so this file's reconciliation may be "
            f"INCOMPLETE on this engine — upgrade setforge to act on it\n"
        )


def _validate_tolerant(data: object) -> Config:
    """Validate ``data`` forward-tolerantly: ignore (warn about) unknown keys.

    Lets Pydantic decide what is genuinely extra — running ``model_validate``
    once and inspecting the error set. This correctly accounts for keys an
    alias or a ``mode="before"`` validator legitimately consumes, which a raw
    key-vs-``model_fields`` diff cannot
    see. If EVERY error is ``extra_forbidden`` (a newer-version field or a
    typo), strip exactly those locations, warn, and retry. Any non-extra
    error means a real validation failure and propagates unchanged.
    """
    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        errors = exc.errors()
        extra_locs = [e["loc"] for e in errors if e.get("type") == "extra_forbidden"]
        if not extra_locs or len(extra_locs) != len(errors):
            raise  # mixed with real errors (or none extra) — a genuine failure
        adjacent, ordinary = _partition_reconciliation_adjacent(data, extra_locs)
        if ordinary:
            _warn_unknown_keys([_format_loc(loc) for loc in ordinary])
        if adjacent:
            _warn_reconciliation_adjacent_strip([_format_loc(loc) for loc in adjacent])
        return Config.model_validate(_strip_extra_locs(data, extra_locs))


def _format_loc(loc: tuple[object, ...]) -> str:
    """Render a Pydantic error ``loc`` tuple as a dotted config path."""
    return ".".join(str(part) for part in loc)


def _strip_extra_locs(data: object, locs: Sequence[tuple[object, ...]]) -> object:
    """Return a deep copy of ``data`` with each ``loc`` path removed.

    ``loc`` is a Pydantic error location (``("tracked_files", "a", "tipo")``).
    The copy feeds a retry ``model_validate`` and is discarded, so ruamel
    formatting need not survive. A loc whose final segment is a list index
    (rather than a mapping key) is left in place; the retry then re-raises
    that error, so an unknown key nested inside a LIST is not
    forward-tolerated. That is acceptable given the schema shape — unknown
    keys land in dict-valued mappings — but it is a real bound on tolerance,
    not merely a defensive no-op.
    """
    cleaned = copy.deepcopy(data)
    for loc in locs:
        *parents, last = loc
        node: object = cleaned
        for part in parents:
            if isinstance(node, Mapping) and part in node:
                node = node[part]
            else:
                node = None
                break
        if isinstance(node, MutableMapping) and last in node:
            del node[last]
    return cleaned


def _guard_schema_version(data: object, path: Path) -> None:
    """Refuse a cross-major-newer config cleanly, BEFORE model validation.

    Reads ``schema_version`` from the raw mapping (default ``"1.0"`` on
    absence), parses it semantically, and compares MAJORS against the
    build's :data:`current_expected_schema_version`:

    - newer MAJOR → :class:`ConfigError` ("upgrade setforge") — a clean,
      non-zero, traceback-free refusal. The engine never best-effort reads
      a config whose major it does not understand.
    - same major (newer minor or older) / older major → proceed. The
      strip-and-retry in :func:`_validate_tolerant` (warning via
      :func:`_warn_unknown_keys`) makes same-major forward reads safe;
      :func:`_warn_on_schema_mismatch` nags on older.

    It then enforces an optional operator-declared ``minimum_version`` floor:
    when present and the build's
    :data:`current_expected_schema_version` is BELOW it (a FULL major.minor
    compare via :func:`~setforge.migrations._meets_floor`), refuse cleanly —
    even in the same-major window the cross-major check above tolerates. The
    floor is read from the RAW mapping here, before model validation, so it
    fires even though :func:`_validate_tolerant` would strip the field as
    unknown on engines that predate it.

    Running BEFORE ``model_validate`` is what keeps a malformed or
    future-major config from leaking a raw Pydantic traceback. A malformed
    ``schema_version`` (or ``minimum_version``) raises a clean
    :class:`ConfigError` via
    :func:`~setforge.migrations.parse_schema_version`.
    """
    raw = data.get("schema_version") if isinstance(data, Mapping) else None
    detected = str(raw) if raw is not None else "1.0"
    detected_major = parse_schema_version(detected)[0]
    expected_major = parse_schema_version(current_expected_schema_version)[0]
    if detected_major > expected_major:
        raise ConfigError(
            f"{path}: schema_version {detected!r} requires a newer setforge "
            f"(this build supports schema "
            f"{current_expected_schema_version!r}); upgrade setforge to "
            f">= {detected_major}.0 to read this config"
        )
    raw_floor = data.get("minimum_version") if isinstance(data, Mapping) else None
    _refuse_below_floor(raw_floor, path)


def _refuse_below_floor(raw_floor: object, path: Path) -> None:
    """Raise :class:`ConfigError` when the build is below ``raw_floor``.

    ``raw_floor`` is the raw ``minimum_version`` value (``None`` ⇒ no floor ⇒
    no-op). When present, compare the build's
    :data:`current_expected_schema_version` against it with a FULL major.minor
    tuple compare (:func:`~setforge.migrations._meets_floor`, ``>=`` boundary)
    and refuse below it. Shared by :func:`_guard_schema_version` (the
    ``load_config`` path) and :func:`guard_minimum_version` (the raw-read path
    used by ``migrate``), so the two enforce an identical floor.
    """
    if raw_floor is None:
        return
    floor = str(raw_floor)
    if not _meets_floor(current_expected_schema_version, floor):
        raise ConfigError(
            f"{path}: minimum_version {floor!r} requires a newer setforge "
            f"(this build supports schema "
            f"{current_expected_schema_version!r}); upgrade setforge to a "
            f"build supporting schema >= {floor} to operate on this config "
            f"(or lower minimum_version in {path})"
        )


def guard_minimum_version(cfg_path: Path) -> None:
    """Enforce the ``minimum_version`` floor from a config file path.

    Verbs that inspect the schema via
    :func:`~setforge.migrations.detect_current_schema` rather than
    :func:`load_config` (notably ``migrate --check`` / ``--apply`` / ``--pin``)
    bypass the floor baked into :func:`_guard_schema_version`. Call this on
    those paths so a below-floor engine refuses there too — and, for
    ``--apply``, BEFORE any mutation. No-op when the file is absent / empty or
    declares no floor; a malformed ``minimum_version`` raises a clean
    :class:`ConfigError`.
    """
    if not cfg_path.exists():
        return
    yaml = YAML(typ="rt")
    with cfg_path.open("r", encoding="utf-8") as fh:
        data = yaml.load(fh)
    raw_floor = data.get("minimum_version") if isinstance(data, Mapping) else None
    _refuse_below_floor(raw_floor, cfg_path)


def _warn_unknown_keys(unknown: list[str]) -> None:
    """Warn (one line per key) for every stripped, schema-undeclared key.

    Covers BOTH forward-compat (a newer config's added fields) AND typos (a
    misspelled key). Text is always written; only the color is TTY-gated,
    so the warning survives CliRunner / Docker e2e / CI capture.
    """
    import sys

    color = sys.stderr.isatty()
    prefix = "\033[33mwarning:\033[0m" if color else "warning:"
    for field_path in unknown:
        sys.stderr.write(
            f"{prefix} ignoring unknown setforge.yaml key {field_path!r} "
            f"(unrecognized by this setforge — a newer-version field or a typo)\n"
        )


def _warn_on_schema_mismatch(config: Config) -> None:
    """Emit a one-line stderr warning when schema_version diverges.

    The warning is non-fatal: the user may have explicitly pinned an
    older schema via ``setforge migrate --pin=X.Y``, so raising would
    block every other subcommand on a soft signal. The yellow color is
    suppressed when stderr is not a TTY (CliRunner / piped capture), so
    captured output stays ANSI-free.
    """
    import sys

    if config.schema_version == current_expected_schema_version:
        return
    color = sys.stderr.isatty()
    prefix = "\033[33mwarning:\033[0m" if color else "warning:"
    sys.stderr.write(
        f"{prefix} setforge.yaml declares "
        f"schema_version={config.schema_version!r} "
        f"but this setforge expects {current_expected_schema_version!r}; "
        f"run `setforge migrate --check` for details\n"
    )


@dataclass(frozen=True, slots=True)
class HostLocalTrackedFileOverride:
    """One tracked_file's resolved overlay-fields overlay state.

    Carried by the mapping returned from
    :func:`apply_host_local_tracked_file_overrides` so compare's
    provenance-tag renderer can read which of the four fields were
    actually overridden host-locally without re-loading local.yaml.

    ``None`` on a field means "no override; the profile-side value
    wins". The mapping never contains an entry where all four are
    ``None`` (the resolver short-circuits the empty-overlay case so
    callers can treat presence as "at least one override applied").
    """

    mode: int | None
    dst: Path | None
    symlink_target: Path | None
    disposition: Disposition | None


@dataclass(frozen=True, slots=True)
class SharedSpanCollision:
    """One same-anchor host-local-vs-shared span intent collision.

    A *shared* span carries pure intent (anchor/kind/semantics) on the
    tracked-side :attr:`TrackedFile.spans`; a host-local span on the SAME
    anchor (declared in ``local.yaml``) shadows it silently in the
    :func:`apply_host_local_tracked_file_overrides` fold. This record is
    what :func:`detect_shared_span_collisions` returns so the install path
    can surface the collision under ``--reconcile-user-sections`` (rather
    than dropping the shared intent silently). ``anchor`` is the markdown
    heading-text OR structural dotted-path string the two spans share —
    the detector is file-type-agnostic (it compares anchors verbatim).
    """

    tracked_file_id: str
    anchor: str


def detect_shared_span_collisions(
    config: Config,
    *,
    local_config_path: Path | None = None,
) -> list[SharedSpanCollision]:
    """Return same-anchor host-local↔shared span collisions per tracked_file.

    A collision exists when a tracked_file declares a ``shared`` span on an
    anchor AND ``local.yaml`` declares a host-local span on the SAME anchor
    for the same tracked_file. The host-local span would silently win the
    per-anchor fold in :func:`apply_host_local_tracked_file_overrides`,
    dropping the shared intent without a trace — this detector is the
    surface the install path consults so the drop is opt-in-visible under
    ``--reconcile-user-sections``.

    Only ``shared``-semantics tracked-side spans participate: a host-local
    span shadowing a host-local tracked-side span is a plain config dup,
    not a cross-repo intent collision, and is excluded. Host-local-only
    spans (no shared counterpart) are likewise never reported.

    Deterministic order: tracked_files are walked in ``config.tracked_files``
    insertion order, anchors within a file in the tracked-side span order.
    Pure + read-only — reads ``local.yaml`` but mutates nothing. No-op
    (empty list) when ``local.yaml`` is absent or declares no overlapping
    span. Lazy-imports :mod:`setforge.source` to dodge the config ↔ source
    cycle.
    """
    from setforge.source import LOCAL_CONFIG_PATH, load_local_tracked_file_overlays

    path = local_config_path if local_config_path is not None else LOCAL_CONFIG_PATH
    overlays = load_local_tracked_file_overlays(path)
    collisions: list[SharedSpanCollision] = []
    for tf_id, tracked_file in config.tracked_files.items():
        overlay = overlays.get(tf_id)
        if overlay is None or not overlay.spans:
            continue
        host_local_anchors = {span.anchor for span in overlay.spans}
        for span in tracked_file.spans:
            if (
                span.semantics is SpanSemantics.SHARED
                and span.anchor in host_local_anchors
            ):
                collisions.append(
                    SharedSpanCollision(tracked_file_id=tf_id, anchor=span.anchor)
                )
    return collisions


def _fold_overlay_spans(
    *,
    tf_id: str,
    tracked_spans: list[SpanEntry],
    overlay_spans: list[SpanEntry],
    prefer_shared_anchors: frozenset[tuple[str, str]],
) -> list[dict[str, object]]:
    """Fold host-local overlay spans over tracked-side shared spans per anchor.

    Host-local (``local.yaml``) spans win each anchor by default — the
    silent host-local-wins fold. ``prefer_shared_anchors`` flips the winner
    for the listed ``(tf_id, anchor)`` pairs: the tracked-side SHARED span
    keeps that anchor instead, i.e. the ``--auto=use-tracked`` / interactive
    "adopt the shared intent" resolution for a detected same-anchor
    collision (see :func:`detect_shared_span_collisions`). Anchors NOT in
    the set keep the host-local default; host-local-only anchors (no shared
    counterpart) are always added.

    Returns the merged spans as plain dicts (``model_dump``) so the
    ``TrackedFile.model_validate`` revalidation re-runs the
    :class:`~setforge.spans.SpanEntry` validators against the combined
    shape.
    """
    merged_spans = {span.anchor: span for span in tracked_spans}
    for span in overlay_spans:
        if (tf_id, span.anchor) in prefer_shared_anchors:
            continue
        merged_spans[span.anchor] = span
    return [span.model_dump() for span in merged_spans.values()]


def apply_host_local_tracked_file_overrides(
    config: Config,
    *,
    local_config_path: Path | None = None,
    prefer_shared_anchors: frozenset[tuple[str, str]] = frozenset(),
) -> dict[str, HostLocalTrackedFileOverride]:
    """Apply the local.yaml host-local ``mode`` / ``dst`` / ``symlink_target`` /
    ``disposition`` / ``spans`` overlay.

    For every entry in ``local.yaml``'s ``tracked_files.<id>`` overlay
    block that declares one of the overlay fields, rebuild the
    matching :class:`TrackedFile` with the override applied:

    - ``mode`` (int) overrides :attr:`TrackedFile.mode` verbatim.
    - ``dst`` (Path) overrides :attr:`TrackedFile.dst` (the string
      template) with ``str(overlay.dst)``; the existing
      :func:`resolve_dst` Jinja2 + ``expanduser`` pipeline runs
      against the override exactly as it would against a tracked-side
      ``dst:`` value.
    - ``symlink_target`` (Path) overrides :attr:`TrackedFile.symlink`
      with ``str(overlay.symlink_target)``; downstream
      :func:`setforge.deploy.deploy_symlinked_file` consumes the
      override transparently and writes a symlink at the resolved
      dst pointing at the raw user string (cross-host portability
      invariant preserved).
    - ``disposition`` (:class:`Disposition`) overrides
      :attr:`TrackedFile.disposition`. The merged shape is re-validated
      via the dump-and-revalidate path, so every ``TrackedFile``
      invariant re-runs against the overridden disposition.
    - ``spans`` (list of :class:`~setforge.spans.SpanEntry`) are folded
      over :attr:`TrackedFile.spans` per anchor by
      :func:`_fold_overlay_spans`. Host-local spans win each shared anchor
      by default (the silent host-local-wins fold); host-local-only
      anchors (no shared counterpart) are always added. ``prefer_shared_anchors``
      (below) flips the winner for selected anchors.

    ``prefer_shared_anchors`` is the set of ``(tracked_file_id, anchor)``
    pairs whose tracked-side SHARED span must keep the anchor instead of
    the host-local default — the ``--auto=use-tracked`` / interactive
    "adopt the shared intent" resolution for a same-anchor host-local↔shared
    collision (see :func:`detect_shared_span_collisions`). The empty default
    preserves today's silent host-local-wins for every anchor; only the
    reconcile path passes a non-empty set.

    Returns a mapping ``{tracked_file_id: HostLocalTrackedFileOverride}``
    of which overrides actually applied — used by compare to render
    ``[host-local mode=...]`` / ``[host-local dst=...]`` /
    ``[host-local symlink → ...]`` / ``[host-local disposition=...]``
    provenance tags without re-loading local.yaml.

    No-op (empty mapping) when ``local.yaml`` is absent or no
    tracked_file declares any overlay field — preserves
    today's behavior for hosts that have not adopted the overlay.
    Lazy-imports :mod:`setforge.source` to dodge the config <->
    source cycle.

    Raises :class:`pydantic.ValidationError` from
    :func:`TrackedFile.model_validate` when the merged shape
    violates a TrackedFile invariant — e.g. an overlay whose
    ``symlink_target`` equals the tracked-side ``dst`` after
    :func:`Path.expanduser` (the ``_symlink_no_self_loop``
    model_validator fires on the merged model), or a ``disposition``
    override on a file that already declares a ``preserve_*`` field.
    Parse-time invariants on the overlay itself (mutual-exclusion, mode
    bounds, ``$VAR`` in dst, invalid disposition value) surface earlier
    in :func:`setforge.source.load_local_tracked_file_overlays`,
    not here.
    """
    from setforge.source import LOCAL_CONFIG_PATH, load_local_tracked_file_overlays

    path = local_config_path if local_config_path is not None else LOCAL_CONFIG_PATH
    overlays = load_local_tracked_file_overlays(path)
    applied: dict[str, HostLocalTrackedFileOverride] = {}
    for tf_id, overlay in overlays.items():
        if (
            overlay.mode is None
            and overlay.dst is None
            and overlay.symlink_target is None
            and overlay.disposition is None
            and not overlay.spans
        ):
            continue
        if tf_id not in config.tracked_files:
            # The overlay references a tracked_file the resolved
            # profile does not include. Stay silent here — the
            # SPEC 8 preserve_user_keys path also tolerates orphan
            # overlay entries (the validate CLI is the unambiguous
            # surface for unknown-tracked_file diagnostics).
            continue
        tracked_file = config.tracked_files[tf_id]
        updates: dict[str, object] = {}
        if overlay.mode is not None:
            updates["mode"] = overlay.mode
        if overlay.dst is not None:
            updates["dst"] = str(overlay.dst)
        if overlay.symlink_target is not None:
            updates["symlink"] = str(overlay.symlink_target)
        if overlay.disposition is not None:
            updates["disposition"] = overlay.disposition.value
        if overlay.spans:
            updates["spans"] = _fold_overlay_spans(
                tf_id=tf_id,
                tracked_spans=tracked_file.spans,
                overlay_spans=overlay.spans,
                prefer_shared_anchors=prefer_shared_anchors,
            )
        # Build a fresh model via model_validate(dump | updates) rather
        # than model_copy(update=...) — model_copy bypasses field +
        # model validators, which would let a hostile overlay set
        # symlink == dst (the _symlink_no_self_loop check would never
        # re-fire) or push a mode value past TrackedFile's stricter
        # field-level rules. The dump-and-revalidate path re-runs
        # every TrackedFile invariant against the merged shape so the
        # overlay-fields override layer cannot weaken the contract.
        merged = {**tracked_file.model_dump(), **updates}
        config.tracked_files[tf_id] = TrackedFile.model_validate(merged)
        applied[tf_id] = HostLocalTrackedFileOverride(
            mode=overlay.mode,
            dst=overlay.dst,
            symlink_target=overlay.symlink_target,
            disposition=overlay.disposition,
        )
    return applied


class OrphanOverlayClass(StrEnum):
    """How a ``local.yaml`` overlay entry fails to match the resolved profile.

    ``UNKNOWN`` — the id appears NOWHERE in :attr:`Config.tracked_files`
    (the full registry); almost always a typo or a stale entry. Surfaced
    as a hard ``validate`` failure (with a did-you-mean suggestion).

    ``OFF_PROFILE`` — the id IS in :attr:`Config.tracked_files` but not in
    THIS profile's resolved ``tracked_files`` list; a legitimate state on a
    multi-profile host. Surfaced as a non-fatal note only.
    """

    UNKNOWN = "unknown"
    OFF_PROFILE = "off_profile"


@dataclass(frozen=True, slots=True)
class OrphanOverlay:
    """One ``local.yaml`` overlay entry the apply site silently skipped.

    Returned by :func:`collect_orphan_overlays` for the read-only
    diagnosis verbs (``validate`` / ``compare``) so a stale or typo'd
    overlay entry becomes discoverable without changing the silent
    install/sync/override apply path. ``class_`` carries the trailing
    underscore because ``class`` is a Python keyword.
    """

    id: str
    class_: OrphanOverlayClass


def collect_orphan_overlays(
    config: Config,
    resolved: ResolvedProfile,
    *,
    local_config_path: Path | None = None,
) -> list[OrphanOverlay]:
    """Classify the ``local.yaml`` overlay ids the apply site skipped.

    :func:`apply_host_local_tracked_file_overrides` silently ``continue``s
    on any overlay id missing from the resolved profile (SPEC-8 precedent —
    install/sync/override must not warn on every run). This pure sibling
    re-loads the same overlay block and classifies each id that would NOT
    apply to ``resolved`` into one of two :class:`OrphanOverlayClass`
    buckets:

    - ``UNKNOWN`` — the id is absent from ``config.tracked_files`` (the
      full registry): a typo or stale entry.
    - ``OFF_PROFILE`` — the id is in ``config.tracked_files`` but not in
      ``resolved.tracked_files``: a legitimate multi-profile host.

    An overlay entry that declares no overlay fields is skipped here for
    the same reason the apply site short-circuits it — it never mutates a
    TrackedFile, so it is not a meaningful orphan. Returns the orphans in
    ``local.yaml`` declaration order; an in-profile overlay is omitted.

    Read-only: never mutates ``config`` or ``resolved``. Lazy-imports
    :mod:`setforge.source` to dodge the config <-> source cycle, mirroring
    the apply site.
    """
    from setforge.source import LOCAL_CONFIG_PATH, load_local_tracked_file_overlays

    path = local_config_path if local_config_path is not None else LOCAL_CONFIG_PATH
    overlays = load_local_tracked_file_overlays(path)
    profile_ids = set(resolved.tracked_files)
    orphans: list[OrphanOverlay] = []
    for tf_id, overlay in overlays.items():
        if (
            overlay.mode is None
            and overlay.dst is None
            and overlay.symlink_target is None
            and overlay.disposition is None
            and not overlay.spans
        ):
            continue
        if tf_id not in config.tracked_files:
            orphans.append(OrphanOverlay(id=tf_id, class_=OrphanOverlayClass.UNKNOWN))
        elif tf_id not in profile_ids:
            orphans.append(
                OrphanOverlay(id=tf_id, class_=OrphanOverlayClass.OFF_PROFILE)
            )
    return orphans


def _validate_plugin_references(config: Config) -> None:
    """Verify every ``profile.claude_plugins`` entry exists in the
    top-level ``Config.claude_plugins`` registry.

    Collects every offender across every profile into a single
    :class:`ConfigError` message so the user fixes all references in
    one round-trip, not one error per re-run.
    """
    registry = set(config.claude_plugins)
    offenders: list[tuple[str, str]] = []
    for profile_name, profile in config.profiles.items():
        for bare_name in profile.claude_plugins:
            # Skip empty/whitespace refs — Check 5b in _check_profile
            # catches those with a dedicated "empty ref" message.
            if not bare_name.strip():
                continue
            if bare_name not in registry:
                offenders.append((profile_name, bare_name))
    if offenders:
        details = ", ".join(f"{profile}.{name}" for profile, name in offenders)
        raise ConfigError(
            f"profile claude_plugins reference undeclared plugin(s): "
            f"{details} (add to top-level claude_plugins:)"
        )


def _validate_mcp_references(config: Config) -> None:
    """Verify every ``profile.mcp_servers`` entry exists in the top-level
    ``Config.mcp_servers`` registry.

    Mirrors :func:`_validate_plugin_references`: collects every offender
    across every profile into a single :class:`ConfigError` so the user
    fixes all references in one round-trip. Empty / whitespace refs are
    skipped (no dedicated empty-ref check exists for MCP names yet, but a
    blank entry would never match the registry, so skipping it here keeps
    the error message focused on genuine typos).
    """
    registry = set(config.mcp_servers)
    offenders: list[tuple[str, str]] = []
    for profile_name, profile in config.profiles.items():
        for bare_name in profile.mcp_servers:
            if not bare_name.strip():
                continue
            if bare_name not in registry:
                offenders.append((profile_name, bare_name))
    if offenders:
        details = ", ".join(f"{profile}.{name}" for profile, name in offenders)
        raise ConfigError(
            f"profile mcp_servers reference undeclared server(s): "
            f"{details} (add to top-level mcp_servers:)"
        )


def _parse_overlay_plugin_pid(pid: str) -> tuple[str, str | None]:
    """Split a local.yaml overlay plugin ref into ``(bare_name, marketplace)``.

    ``add`` entries use the ``name@marketplace`` shape per SPEC 2
    mockup; ``remove`` entries are bare names. Returns ``(name, None)``
    when no ``@`` separator is present so the same parser drives both
    list shapes. Empty / whitespace-only refs raise
    :class:`setforge.local_overlay.LocalOverlayError` (the resolver-
    phase sentinel) so a typo'd YAML list entry surfaces under the same
    error-routing arm as add ∩ remove collisions — load-phase failures
    are :class:`LocalOverlayLoadError`; this is a resolver-phase failure
    so Check 6 still runs as a fallback in the validate CLI.
    """
    cleaned = pid.strip()
    if not cleaned:
        raise LocalOverlayError(
            "local.yaml plugins overlay: empty / whitespace plugin reference"
        )
    if "@" not in cleaned:
        return cleaned, None
    name, mp = cleaned.split("@", 1)
    if not name or not mp:
        raise LocalOverlayError(
            f"local.yaml plugins overlay: malformed plugin ref {pid!r} "
            "(expected 'name@marketplace')"
        )
    return name, mp


@dataclass(frozen=True, slots=True)
class LocalOverlayResolution:
    """Resolved provenance lists for one apply_local_overlay() run.

    Carries the three :class:`setforge.local_overlay` resolved lists so
    callers (compare's overlay-block renderer, install's per-line
    provenance) can display ``[from local.yaml]`` / SPEC-2 remove tags
    without re-running the resolvers. Callers that need to suppress the
    footer summary on a no-op overlay walk should use
    :func:`setforge.local_overlay.has_local_overlay` on the relevant
    field — gating is data-driven (LOCAL_ADD / LOCAL_REMOVE membership),
    not configuration-shape-driven.
    """

    plugins: list["ResolvedPlugin"]
    extensions: list["ResolvedExtension"]
    marketplaces: list["ResolvedMarketplace"]


def _load_overlay_blocks(
    local_config_path: Path | None,
) -> tuple["PluginOverlay", "ExtensionOverlay", "MarketplaceOverlay"]:
    """Load the three SPEC 2 overlay blocks from ``local.yaml``.

    Lazy-imports :mod:`setforge.source` to dodge the config <-> source
    cycle and keep this path off the import-time graph for every
    command (compare / install / sync etc. each import
    :mod:`setforge.config` at boot). Returns the three overlay structs
    (plugins, extensions, marketplaces) — each is a typed Pydantic
    model with ``.add`` / ``.remove`` lists or mappings.

    Load-phase failures (YAML parse error, non-mapping top level,
    Pydantic shape error in an overlay block) are re-raised as
    :class:`setforge.local_overlay.LocalOverlayLoadError` — a sentinel
    subclass of :class:`ConfigError` — so the validate CLI can
    distinguish them from cross-ref-phase failures (which keep raising
    bare :class:`ConfigError`). The distinction matters because a
    load-phase failure means the cross-ref check did NOT run, and the
    standalone Check 6 must still execute as a fallback.
    """
    from setforge.source import (
        LOCAL_CONFIG_PATH as _LOCAL_CONFIG_PATH,
    )
    from setforge.source import (
        _load_local_source_config,
    )

    path = local_config_path if local_config_path is not None else _LOCAL_CONFIG_PATH
    try:
        local = _load_local_source_config(path)
    except ConfigError as exc:
        raise LocalOverlayLoadError(str(exc)) from exc
    except ValidationError as exc:
        raise LocalOverlayLoadError(str(exc)) from exc
    return local.plugins, local.extensions, local.marketplaces


def _resolve_provenance_lists(
    config: Config,
    resolved: ResolvedProfile,
    profile_name: str,
    plugin_overlay: "PluginOverlay",
    extension_overlay: "ExtensionOverlay",
    marketplace_overlay: "MarketplaceOverlay",
) -> tuple[
    "list[ResolvedPlugin]", "list[ResolvedExtension]", "list[ResolvedMarketplace]"
]:
    """Resolve the three provenance lists for the SPEC 2 overlay.

    Each :mod:`setforge.local_overlay` resolver raises
    :class:`setforge.local_overlay.LocalOverlayError` (a
    :class:`ConfigError`) on collision or unknown-remove — the caller
    surfaces those errors under the validate report-all-then-refuse
    contract.
    """
    resolved_plugins = resolve_plugin_overlay(
        profile_plugins=list(resolved.claude_plugins),
        profile_name=profile_name,
        overlay=plugin_overlay,
    )
    resolved_extensions = resolve_extension_overlay(
        profile_extensions=list(resolved.extensions.include),
        profile_name=profile_name,
        overlay=extension_overlay,
    )
    profile_marketplaces = _profile_referenced_marketplaces(config, resolved)
    resolved_marketplaces = resolve_marketplace_overlay(
        profile_marketplaces=profile_marketplaces,
        profile_name=profile_name,
        overlay=marketplace_overlay,
    )
    return resolved_plugins, resolved_extensions, resolved_marketplaces


def apply_local_overlay(
    config: Config,
    resolved: ResolvedProfile,
    profile_name: str,
    *,
    local_config_path: Path | None = None,
) -> LocalOverlayResolution:
    """Apply the local.yaml plugin/extension/marketplace overlay (SPEC 2).

    Loads overlay blocks (:func:`_load_overlay_blocks`), resolves
    provenance lists (:func:`_resolve_provenance_lists`), mutates
    ``config`` and ``resolved`` in place, then runs
    :func:`_validate_overlay_marketplace_cross_ref`. See each helper's
    docstring for phase-specific behavior. Returns a
    :class:`LocalOverlayResolution` carrying the three resolved lists
    so compare/install formatters can read provenance without
    re-running the resolvers.
    """
    plugin_overlay, extension_overlay, marketplace_overlay = _load_overlay_blocks(
        local_config_path
    )
    resolved_plugins, resolved_extensions, resolved_marketplaces = (
        _resolve_provenance_lists(
            config,
            resolved,
            profile_name,
            plugin_overlay,
            extension_overlay,
            marketplace_overlay,
        )
    )
    _apply_marketplace_mutations(config, marketplace_overlay)
    _apply_plugin_mutations(config, resolved, plugin_overlay)
    _apply_extension_mutations(resolved, extension_overlay)
    _validate_overlay_marketplace_cross_ref(config, resolved)
    return LocalOverlayResolution(
        plugins=resolved_plugins,
        extensions=resolved_extensions,
        marketplaces=resolved_marketplaces,
    )


def _profile_referenced_marketplaces(
    config: Config, resolved: ResolvedProfile
) -> list[str]:
    """Return marketplaces referenced by the resolved profile, in stable order.

    Walks ``resolved.claude_plugins`` and looks up each bare name in
    ``config.claude_plugins`` to find its marketplace; the set is
    deduplicated while preserving first-occurrence order. Bare names
    absent from ``cfg.claude_plugins`` are silently skipped — the
    caller's plugin reconcile path raises a clearer error for unknown
    plugin refs.
    """
    seen: set[str] = set()
    out: list[str] = []
    for bare_name in resolved.claude_plugins:
        ref = config.claude_plugins.get(bare_name)
        if ref is None:
            continue
        mp = ref.marketplace
        if mp in seen:
            continue
        seen.add(mp)
        out.append(mp)
    return out


def _apply_marketplace_mutations(config: Config, overlay: "MarketplaceOverlay") -> None:
    """Apply ``marketplaces.add`` / ``marketplaces.remove`` to ``config.marketplaces``.

    Coerces every ``_MarketplaceLocalDecl`` to a
    :class:`MarketplaceSource` via the same field shape — the model's
    ``_exactly_one`` validator gives identical guarantees. Removes drop
    the matching name from the dict. Mutates in place so downstream
    consumers (reconcile, validate) see the merged set.
    """
    for name, decl in overlay.add.items():
        config.marketplaces[name] = MarketplaceSource(
            source=decl.source, repo=decl.repo, path=decl.path
        )
    for name in overlay.remove:
        config.marketplaces.pop(name, None)


def _apply_plugin_mutations(
    config: Config, resolved: ResolvedProfile, overlay: "PluginOverlay"
) -> None:
    """Apply ``plugins.add`` / ``plugins.remove`` to the resolved profile.

    ``add`` entries use ``name@marketplace`` shape: synthesize a
    :class:`ClaudePluginRef` in ``cfg.claude_plugins`` (or replace an
    existing one when the user explicitly re-routed to a new
    marketplace via local.yaml) and append the bare name to
    ``resolved.claude_plugins``. ``remove`` entries are bare names;
    drop matching entries from ``resolved.claude_plugins`` in place.

    A bare-name ``add`` without ``@`` synthesizes nothing (the user
    must pair the bare name with a marketplace by other means); we
    still add it to ``resolved.claude_plugins`` so the cross-ref check
    surfaces the missing registry entry as a single error message
    rather than silently dropping the add.
    """
    existing = list(resolved.claude_plugins)
    removed = set(overlay.remove)
    pruned = [name for name in existing if name not in removed]
    for raw in overlay.add:
        bare_name, marketplace = _parse_overlay_plugin_pid(raw)
        if marketplace is not None:
            config.claude_plugins[bare_name] = ClaudePluginRef(marketplace=marketplace)
        if bare_name not in pruned:
            pruned.append(bare_name)
    resolved.claude_plugins = pruned


def _apply_extension_mutations(
    resolved: ResolvedProfile, overlay: "ExtensionOverlay"
) -> None:
    """Apply ``extensions.add`` / ``extensions.remove`` to resolved.extensions.include.

    Mutates the include list in place so downstream
    :func:`setforge.vscode_extensions.reconcile` sees the merged set.
    Excludes are profile-only — local.yaml has no extensions.exclude
    overlay per SPEC 2.
    """
    existing = list(resolved.extensions.include)
    removed = set(overlay.remove)
    pruned = [ext_id for ext_id in existing if ext_id not in removed]
    for ext_id in overlay.add:
        if ext_id not in pruned:
            pruned.append(ext_id)
    resolved.extensions = resolved.extensions.model_copy(update={"include": pruned})


def _validate_overlay_marketplace_cross_ref(
    config: Config, resolved: ResolvedProfile
) -> None:
    """Verify every resolved plugin's marketplace exists in cfg.marketplaces.

    Fires at BOTH ``setforge validate`` (offline) AND
    ``setforge install`` (defensive backstop) per SPEC 2's Q8 decision.
    The error message lists the offending plugin + missing marketplace
    name + the available-marketplaces set so the user can fix the
    right side (cf. SPEC 2 mockup validate-failure shape).
    """
    available = sorted(config.marketplaces)
    available_set = set(available)
    offenders: list[tuple[str, str]] = []
    for bare_name in resolved.claude_plugins:
        ref = config.claude_plugins.get(bare_name)
        if ref is None:
            offenders.append((bare_name, "<unknown plugin>"))
            continue
        if ref.marketplace not in available_set:
            offenders.append((bare_name, ref.marketplace))
    if not offenders:
        return
    lines = [
        f"local.yaml plugins overlay: plugin {plugin!r} references "
        f"marketplace {marketplace!r}, which is not declared"
        for plugin, marketplace in offenders
    ]
    suffix = (
        f". Available marketplaces (profile + local.marketplaces.add): "
        f"{', '.join(available) if available else '(none)'}. "
        f"Fix: either correct the @-suffix or add the marketplace "
        f"under marketplaces.add in local.yaml."
    )
    raise ConfigError("; ".join(lines) + suffix)
