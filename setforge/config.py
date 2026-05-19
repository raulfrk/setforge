"""Typed configuration schema for setforge.

Pydantic models validate ``setforge.yaml`` and provide the in-memory
contract used by every subcommand. YAML is loaded via ruamel.yaml in
round-trip mode so comments and key order survive subsequent capture
writes that re-serialize the document.
"""

from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

# ruamel.yaml ships py.typed without resolvable annotations; no stub pkg on PyPI.
from ruamel.yaml import YAML  # type: ignore[import-not-found]
from ruamel.yaml.scalarint import OctalInt, ScalarInt  # type: ignore[import-not-found]

from setforge.errors import ConfigError, ProfileNotFound

_STRICT = ConfigDict(extra="forbid")

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


class TrackedFile(BaseModel):
    model_config = _STRICT

    src: Path
    dst: str
    template: bool = False
    preserve_user_sections: bool = False
    preserve_user_sections_mode: SectionMode = SectionMode.KEEP_DEFAULTS
    preserve_user_keys: list[str] = []
    preserve_user_keys_deep: list[str] = []
    mode: int | None = None
    """POSIX file-mode bits (chmod) for the live dst.

    YAML-1.2 octal int literal only (``mode: 0o755``). The validator
    rejects both bare strings and YAML-1.1-style ``0755`` literals,
    which ruamel.yaml parses as the string ``"0755"`` under YAML 1.2.
    Setuid/setgid bits are refused for security; sticky bit (``0o1000``)
    is allowed. When ``None``, deploy preserves the source file's mode
    (today's behavior, zero regression).
    """
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
                f"mode must be YAML-1.2 octal int literal (e.g. 0o755), "
                f"not string or decimal. Got: {v!r}"
            )
        if not (0o0 <= v <= 0o7777):
            raise ValueError(f"mode {oct(v)} out of range 0o0..0o7777")
        if v & 0o6000:
            raise ValueError(
                f"mode {oct(v)} sets setuid/setgid bit — refusing for security."
            )
        return int(v)

    @field_validator("preserve_user_keys")
    @classmethod
    def _well_formed_preserve_paths(cls, v: list[str]) -> list[str]:
        """Reject malformed nested paths in ``preserve_user_keys``.

        Single-segment names (no ``" > "`` separator) are v1 literal
        top-level keys — accepted as-is. Multi-segment paths split on
        ``" > "`` and every segment must be non-empty and not
        whitespace-only. The empty string is rejected outright.
        """
        for path in v:
            if path == "":
                raise ValueError("preserve_user_keys entry cannot be empty string")
            if _PRESERVE_PATH_SEPARATOR not in path:
                continue
            segments = path.split(_PRESERVE_PATH_SEPARATOR)
            for seg in segments:
                if seg == "" or seg.strip() == "":
                    raise ValueError(
                        f"preserve_user_keys path {path!r} has an empty or "
                        f"whitespace-only segment (no leading/trailing "
                        f"{_PRESERVE_PATH_SEPARATOR!r}, no consecutive "
                        f"separators)"
                    )
        return v

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


def load_config(path: Path) -> Config:
    """Parse ``setforge.yaml`` from disk and validate against the schema.

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
    config = Config.model_validate(data)
    _validate_plugin_references(config)
    _warn_on_schema_mismatch(config)
    return config


def _warn_on_schema_mismatch(config: Config) -> None:
    """Emit a one-line yellow stderr warning when schema_version diverges.

    Imported lazily to avoid a circular import — ``setforge.migrations``
    is conceptually a downstream consumer of the loaded ``Config``.
    """
    import sys

    from setforge.migrations import current_expected_schema_version

    if config.schema_version == current_expected_schema_version:
        return
    sys.stderr.write(
        f"\033[33mwarning:\033[0m setforge.yaml declares "
        f"schema_version={config.schema_version!r} "
        f"but this setforge expects {current_expected_schema_version!r}; "
        f"run `setforge migrate --check` for details\n"
    )


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
