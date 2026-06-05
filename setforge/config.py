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
from typing import TYPE_CHECKING, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    computed_field,
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
from setforge.migrations import current_expected_schema_version, parse_schema_version
from setforge.preserved_keys import KeyOrigin, ResolvedPreservedKey, resolve_overlay
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

_PRESERVE_PATH_SEPARATOR: str = " > "
"""Segment separator for nested-path entries in ``TrackedFile.preserve_user_keys``.

Mirrors :data:`setforge.jsonc.PATH_SEPARATOR` — re-declared here so the
config schema does not depend on the JSONC module at import time.
"""


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


class SectionMode(StrEnum):
    """How capture treats marker bodies in tracked_files with
    ``preserve_user_sections: true``.

    ``keep_defaults`` (default, non-destructive): capture re-splices the
    tracked file's existing marker bodies into the live content before
    writing tracked, so global defaults baked into tracked survive every
    sync. Falls back to ``strip`` semantics when there's no existing
    tracked file (no defaults to preserve).

    ``strip`` (opt-in, destructive): capture wipes marker bodies entirely.
    Use only when markers are pure host-local placeholders that must
    never persist into the tracked source.
    """

    KEEP_DEFAULTS = "keep_defaults"
    STRIP = "strip"


def _check_well_formed_preserve_paths(paths: list[object]) -> None:
    """Reject empty strings and malformed nested paths in preserve_user_keys.

    Mirrors the historical ``@field_validator`` checks that lived on
    the ``preserve_user_keys`` field before it became a computed_field.
    Single-segment names are accepted as-is; multi-segment paths split
    on ``" > "`` and every segment must be non-empty and not
    whitespace-only.
    """
    for path in paths:
        if path == "":
            raise ValueError("preserve_user_keys entry cannot be empty string")
        if not isinstance(path, str):
            continue
        if _PRESERVE_PATH_SEPARATOR not in path:
            continue
        for seg in path.split(_PRESERVE_PATH_SEPARATOR):
            if seg == "" or seg.strip() == "":
                raise ValueError(
                    f"preserve_user_keys path {path!r} has an empty or "
                    f"whitespace-only segment (no leading/trailing "
                    f"{_PRESERVE_PATH_SEPARATOR!r}, no consecutive "
                    f"separators)"
                )


class TrackedFile(BaseModel):
    model_config = _STRICT

    src: Path
    dst: str
    template: bool = False
    preserve_user_sections: bool = False
    preserve_user_sections_mode: SectionMode = SectionMode.KEEP_DEFAULTS
    preserve_user_keys_resolved: list[ResolvedPreservedKey] = Field(
        default_factory=list
    )
    """Resolved preserve_user_keys list with per-key provenance tags.

    Seeded from the YAML ``preserve_user_keys:`` list at load time
    (each entry tagged :attr:`KeyOrigin.FROM_PROFILE` with
    ``source_profile=None`` — the profile context is filled in at
    profile-resolution time by
    :func:`setforge.config.apply_preserve_user_keys_overlay`). May be
    overwritten in-bulk (NOT in-place — Pydantic v2 frozen-on-copy
    semantics) by the loader once the local.yaml overlay is known.

    Mockup B's compare/install formatters read this list to emit
    per-key provenance lines. The legacy :attr:`preserve_user_keys`
    surface remains available as a :func:`computed_field` derived
    view for every existing consumer (yaml_merge / jsonc / merge /
    sync / revert / etc.) — back-compat is automatic.
    """
    preserve_user_keys_deep: list[str] = []
    """Paths whose live → tracked overlay does a *deep* merge instead of
    the shallow whole-leaf replace of ``preserve_user_keys``. Tracked
    sub-keys absent on the live side survive. Live-only sub-keys are
    added. List values at sub-paths are whole-replaced (live wins). Type
    mismatches at deep terminals raise ``MergeTypeMismatch``.

    Mutually exclusive with ``preserve_user_keys`` per-path: a path may
    appear in at most one of the two lists. ``[*]`` / ``[]`` list
    suffixes are not supported on this list — use the shallow list for
    list-targeted paths.
    """
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

    ``None`` ⇒ the legacy 2-way preserve path, byte-for-byte unchanged.
    When set, the file is reconciled by the stored-base 3-way merge per
    :class:`Disposition`. Mutually exclusive with the legacy
    ``preserve_*`` family (see
    :meth:`_disposition_excludes_legacy_preserve`) — a file uses one
    model or the other, never both.
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

    @model_validator(mode="before")
    @classmethod
    def _seed_preserve_user_keys_resolved(cls, data: object) -> object:
        """Convert YAML ``preserve_user_keys: [...]`` input into seeded
        ``preserve_user_keys_resolved`` entries.

        Each entry from the input list becomes a
        :class:`ResolvedPreservedKey` tagged
        :attr:`KeyOrigin.FROM_PROFILE` with ``source_profile=None``.
        The profile context (which profile in the chain declared the
        key) is filled in at profile-resolution time by
        :func:`apply_preserve_user_keys_overlay`, which rebuilds the
        list with the leaf-profile name + applies any local.yaml
        overlay.

        Refuses the malformed shape where BOTH
        ``preserve_user_keys`` and ``preserve_user_keys_resolved``
        appear in the input mapping — the YAML surface is single-shape
        only (``preserve_user_keys``); ``preserve_user_keys_resolved``
        is the loader-populated derived shape.
        """
        if not isinstance(data, dict):
            return data
        has_input = "preserve_user_keys" in data
        has_resolved = "preserve_user_keys_resolved" in data
        if has_resolved:
            # Resolved wins — drop any computed-field round-trip
            # artifact under the same name (model_dump() includes the
            # computed field; re-validating that dump must round-trip
            # cleanly without rejecting the seed-from-input path).
            if has_input:
                new_data = dict(data)
                new_data.pop("preserve_user_keys")
                return new_data
            return data
        if not has_input:
            return data
        raw_list = data["preserve_user_keys"]
        if not isinstance(raw_list, list):
            # Let Pydantic surface the standard "Input should be a valid
            # list" message rather than pre-empting it here.
            return data
        _check_well_formed_preserve_paths(raw_list)
        seeded = [
            ResolvedPreservedKey(str(item), KeyOrigin.FROM_PROFILE, None)
            for item in raw_list
        ]
        # Replace the input key with the seeded resolved list; the
        # original `preserve_user_keys` no longer maps onto a real
        # model field (it is a computed_field below).
        new_data = dict(data)
        new_data.pop("preserve_user_keys")
        new_data["preserve_user_keys_resolved"] = seeded
        return new_data

    @computed_field  # type: ignore[prop-decorator]
    @property
    def preserve_user_keys(self) -> list[str]:
        """Effective preserve_user_keys list (back-compat derived view).

        Returns ``[k.key for k in preserve_user_keys_resolved if k.origin
        != REMOVED_VIA_LOCAL]`` — the set of keys whose live values
        deploy/install should overlay onto tracked. Every pre-overlay
        consumer (yaml_merge / jsonc / merge / sync / revert / etc.)
        reads this property; the underlying provenance lives on
        :attr:`preserve_user_keys_resolved` for mockup-B-aware
        formatters in compare/deploy.
        """
        return [
            k.key
            for k in self.preserve_user_keys_resolved
            if k.origin != KeyOrigin.REMOVED_VIA_LOCAL
        ]

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

    @model_validator(mode="after")
    def _no_preserve_path_overlap(self) -> Self:
        overlap = set(self.preserve_user_keys) & set(self.preserve_user_keys_deep)
        if overlap:
            raise ValueError(
                f"path(s) declared in both preserve_user_keys and "
                f"preserve_user_keys_deep: {sorted(overlap)}"
            )
        deep_heads = set(self.preserve_user_keys_deep)
        for path in self.preserve_user_keys:
            if _PRESERVE_PATH_SEPARATOR not in path:
                continue
            head = path.split(_PRESERVE_PATH_SEPARATOR, 1)[0]
            if head in deep_heads:
                raise ValueError(
                    f"preserve_user_keys path {path!r} starts with "
                    f"{head!r}, which is declared whole-subtree in "
                    f"preserve_user_keys_deep; the two semantics conflict. "
                    f"Drop one or rename the head."
                )
        return self

    @model_validator(mode="after")
    def _disposition_excludes_legacy_preserve(self) -> Self:
        """A file uses EITHER the disposition model OR legacy ``preserve_*``.

        The two are distinct reconciliation models — whole-file stored-base
        3-way (disposition) versus per-key / per-section live-preserve
        (``preserve_*``). Allowing both on one tracked_file would make the
        deploy path ambiguous, so the combination is refused at load time.
        """
        if self.disposition is None:
            return self
        offenders: list[str] = []
        if self.preserve_user_sections:
            offenders.append("preserve_user_sections")
        if self.preserve_user_keys:  # computed view; excludes REMOVED_VIA_LOCAL
            offenders.append("preserve_user_keys")
        if self.preserve_user_keys_deep:
            offenders.append("preserve_user_keys_deep")
        if self.preserve_user_sections_mode is not SectionMode.KEEP_DEFAULTS:
            offenders.append("preserve_user_sections_mode")
        if offenders:
            raise ValueError(
                f"disposition: {self.disposition.value!r} is mutually exclusive "
                f"with legacy preserve field(s): {sorted(offenders)}. A file uses "
                f"either the disposition model or preserve_*, not both."
            )
        return self

    @field_validator("preserve_user_keys_deep")
    @classmethod
    def _no_list_suffix_on_deep(cls, v: list[str]) -> list[str]:
        for path in v:
            if path.endswith("[*]") or path.endswith("[]"):
                raise ValueError(
                    f"preserve_user_keys_deep does not support [*] / [] "
                    f"list suffixes (got {path!r}); use preserve_user_keys "
                    f"for list-targeted paths."
                )
        return v

    @field_validator("mode", mode="before")
    @classmethod
    def _validate_mode(cls, v: object) -> int | None:
        """Reject every shape EXCEPT YAML-1.2 octal (``0o755``) or a plain int.

        ruamel.yaml round-trip semantics for the value before Pydantic
        sees it:

        - ``mode: 0o755`` -> :class:`OctalInt(493)` (the intended form).
        - ``mode: 0755``  -> :class:`ScalarInt(755)` (NOT 0o755! The
          leading zero is silently stripped under YAML 1.2 — a
          well-known footgun for users migrating from YAML 1.1).
        - ``mode: "0755"`` -> ``str("0755")``.
        - ``mode: 755`` -> plain ``int(755)`` (decimal — almost
          certainly a typo; 755 = 0o1363, not 0o755).

        The validator accepts :class:`OctalInt` (the canonical form)
        and the exact ``int`` type (a Pydantic-caller passing the
        Python literal ``0o755`` == 493 — same value, different
        provenance). Every other shape — including :class:`ScalarInt`
        subclasses that are NOT :class:`OctalInt`, ``str``, ``bool`` —
        is rejected with a message pointing at ``0o755``.
        ``bool`` deserves special mention: Python's
        ``isinstance(True, int)`` is True, so without an explicit
        check ``mode: true`` would silently mean ``0o1``.
        """
        if v is None:
            return None
        if isinstance(v, bool):
            raise ValueError(
                f"mode must be YAML-1.2 octal int literal (e.g. 0o755), "
                f"not bool. Got: {v!r}"
            )
        if isinstance(v, ScalarInt) and not isinstance(v, OctalInt):
            raise ValueError(
                f"mode {int(v)} appears to use YAML-1.1-style leading-zero "
                f"octal (e.g. 0755) which YAML 1.2 silently parses as "
                f"decimal. If you meant the permission bits commonly "
                f"written as 'octal 755', use the YAML-1.2 literal 0o755. "
                f"If you literally meant the integer {int(v)}, use "
                f"0o{int(v):o}."
            )
        if type(v) is not int and not isinstance(v, OctalInt):
            raise ValueError(
                f"mode must be a YAML-1.2 octal int literal (e.g. 0o755); "
                f"strings, floats, and other types are rejected. Got: {v!r}"
            )
        if not (0o0 <= v <= 0o7777):
            raise ValueError(f"mode {oct(v)} out of range 0o0..0o7777")
        if v & 0o6000:
            raise ValueError(
                f"mode {oct(v)} sets setuid/setgid bit — refusing for security."
            )
        return int(v)

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
    tracked_files: dict[str, TrackedFile]
    marketplaces: dict[str, MarketplaceSource] = {}
    claude_plugins: dict[str, ClaudePluginRef] = {}
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
    _warn_on_schema_mismatch(config)
    return config


def _validate_tolerant(data: object) -> Config:
    """Validate ``data`` forward-tolerantly: ignore (warn about) unknown keys.

    Lets Pydantic decide what is genuinely extra — running ``model_validate``
    once and inspecting the error set. This correctly accounts for keys an
    alias or a ``mode="before"`` validator legitimately consumes (e.g.
    ``preserve_user_keys``), which a raw key-vs-``model_fields`` diff cannot
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
        _warn_unknown_keys([_format_loc(loc) for loc in extra_locs])
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

    Running BEFORE ``model_validate`` is what keeps a malformed or
    future-major config from leaking a raw Pydantic traceback. A malformed
    ``schema_version`` raises a clean :class:`ConfigError` via
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


def apply_preserve_user_keys_overlay(
    config: Config,
    profile_name: str,
    *,
    local_config_path: Path | None = None,
) -> None:
    """Apply the local.yaml ``preserve_user_keys`` overlay (mockup B).

    For every tracked_file in ``config.tracked_files``, rebuild
    :attr:`TrackedFile.preserve_user_keys_resolved` against the
    resolved chain's leaf ``profile_name`` and any matching entry in
    the local.yaml ``tracked_files.<id>.preserve_user_keys`` overlay.
    When ``local.yaml`` is absent, or the overlay block is empty for a
    given tracked_file, the resolved list collapses to identity:
    every YAML-declared key tagged :attr:`KeyOrigin.FROM_PROFILE`
    against ``profile_name`` (anti-smell: must NOT special-case the
    empty-overlay path).

    The :mod:`setforge.source` import is lazy at the call boundary so
    a circular import (config <-> source <-> config) cannot arise at
    module-load time per the SPEC 8 discipline. Raises
    :class:`PreserveUserKeysOverlayError` (a :class:`ConfigError`
    subclass) when the overlay is contradictory or references an
    unknown key — surface the violation immediately rather than
    silently producing a wrong resolved list.
    """
    # Lazy-import to avoid a config <-> source cycle and to keep this
    # path off the import-time graph for every command (compare /
    # install / sync etc. each import setforge.config at boot).
    from setforge.source import (
        LOCAL_CONFIG_PATH as _LOCAL_CONFIG_PATH,
    )
    from setforge.source import (
        load_local_tracked_file_overlays,
    )

    path = local_config_path if local_config_path is not None else _LOCAL_CONFIG_PATH
    overlays = load_local_tracked_file_overlays(path)
    for tf_id, tracked_file in config.tracked_files.items():
        # Today's resolved list (seeded from YAML input by the pre-validator)
        # carries source_profile=None; the overlay applier re-stamps every
        # FROM_PROFILE entry with the real profile_name and overlays the
        # local.yaml add/remove block.
        profile_keys = [
            k.key
            for k in tracked_file.preserve_user_keys_resolved
            if k.origin == KeyOrigin.FROM_PROFILE
        ]
        overlay = overlays.get(tf_id)
        overlay_add: list[str] = []
        overlay_remove: list[str] = []
        if overlay is not None and overlay.preserve_user_keys is not None:
            overlay_add = list(overlay.preserve_user_keys.add)
            overlay_remove = list(overlay.preserve_user_keys.remove)
        resolved = resolve_overlay(
            profile_keys=profile_keys,
            profile_name=profile_name,
            overlay_add=overlay_add,
            overlay_remove=overlay_remove,
        )
        # Anti-smell: do NOT mutate Pydantic models in-place. Pydantic v2
        # makes field assignment validating-by-default; build a fresh
        # model and rebind in-place on the Config's mapping so existing
        # ``config.tracked_files[id]`` callers see the new resolved list.
        config.tracked_files[tf_id] = tracked_file.model_copy(
            update={"preserve_user_keys_resolved": resolved}
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
      :attr:`TrackedFile.disposition`. Attempting to add a disposition
      to a file that carries a legacy ``preserve_*`` field raises
      :class:`pydantic.ValidationError` because the dump-and-revalidate
      path re-runs :func:`TrackedFile._disposition_excludes_legacy_preserve`
      against the merged shape.
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
        # field-level rules, or add a disposition to a file that carries
        # legacy preserve_* fields (_disposition_excludes_legacy_preserve
        # would never re-fire). The dump-and-revalidate path re-runs
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
