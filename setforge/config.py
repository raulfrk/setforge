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
from typing import TYPE_CHECKING, Annotated, ClassVar, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from ruamel.yaml import YAML
from ruamel.yaml.scalarint import OctalInt, ScalarInt

from setforge.errors import ConfigError, ProfileNotFound
from setforge.migrations import (
    _meets_floor,
    current_expected_schema_version,
    parse_schema_version,
)
from setforge.overlay_provenance import (
    LocalOverlayError,
    LocalOverlayLoadError,
    ResolvedExtension,
    ResolvedMarketplace,
    ResolvedPlugin,
    resolve_extension_overlay,
    resolve_marketplace_overlay,
    resolve_plugin_overlay,
)

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


def _reject_control_chars_in_path(v: object) -> object:
    s = str(v)
    bad = sorted({c for c in s if c in _FORBIDDEN_PATH_CHARS})
    if bad:
        escaped = ", ".join(f"\\x{ord(c):02x}" for c in bad)
        raise ValueError(
            f"path contains forbidden control character(s) [{escaped}]: {s!r}"
        )
    return v


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


class McpScope(StrEnum):
    """The ``--scope`` value passed to ``claude mcp add`` / ``remove``.

    A closed set mirroring the ``claude mcp`` CLI's own choices:
    ``user`` (host-wide), ``project`` (committed ``.mcp.json``), and
    ``local`` (project-private). Defaults to ``user`` — the host-wide
    registration setforge tracks for tools like Serena.
    """

    USER = "user"
    PROJECT = "project"
    LOCAL = "local"


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

    Range and setuid/setgid policy: see :func:`_validate_mode_policy`.
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


def _validate_mode_policy(v: object) -> int | None:
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
    allow_outside_home: bool = False

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
        return _validate_mode_policy(v)

    @field_validator("src", "dst", "symlink", mode="before")
    @classmethod
    def _no_control_chars_in_path(cls, v: object) -> object:
        if v is None:
            return v
        return _reject_control_chars_in_path(v)


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
    ``--scope`` value (a closed :class:`McpScope` set) and defaults to
    ``McpScope.USER`` (the host-wide registration Serena uses). Mirrors the
    shape of :class:`ClaudePluginRef` / :class:`MarketplaceSource`.
    """

    model_config = _STRICT

    command: list[str]
    scope: McpScope = McpScope.USER

    @field_validator("command")
    @classmethod
    def _command_non_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("McpServerRef.command must be a non-empty token list")
        return v


class SectionTemplateRef(BaseModel):
    """One shareable host-local section-body template in the library.

    ``src`` is a path relative to the config-repo's ``templates/``
    directory (mirroring :attr:`TrackedFile.src`, which is relative to
    ``tracked/``); the body file is read at install time to seed an
    empty/missing host-local section named in a profile's
    :attr:`Profile.section_slots`. The library is SEED-ONCE: a populated
    host-local section is never overwritten, so template-body edits do
    not propagate to a host that has already adopted the section. This is
    distinct from the disposition (pinned/forked) model and from the
    shared-section three-way reconciler.
    """

    model_config = _STRICT

    src: Path

    @field_validator("src", mode="before")
    @classmethod
    def _no_control_chars_in_path(cls, v: object) -> object:
        return _reject_control_chars_in_path(v)


class PackageKind(StrEnum):
    """Discriminator for the :data:`Package` tagged union."""

    CARGO = "cargo"
    PYTHON = "python"
    GO = "go"
    GITHUB_RELEASE = "github_release"
    LOCAL = "local"
    PLUGIN = "plugin"
    EXTENSION = "extension"


def _reject_path_in_bare_name(value: str, field_name: str) -> str:
    """Path-traversal defense (layer 1): reject ``/`` or ``..`` in a bare name."""
    stripped = value.strip()
    if not stripped or stripped == ".":
        raise ValueError(
            f"{field_name} {stripped!r} must be a non-empty bare filename — "
            f"empty, whitespace-only, and '.' are rejected."
        )
    if "/" in stripped or ".." in stripped:
        raise ValueError(
            f"{field_name} {stripped!r} must be a bare filename — "
            f"'/' and '..' are rejected (path-traversal defense)."
        )
    return value


class CargoPackage(BaseModel):
    model_config = _STRICT

    type: Literal[PackageKind.CARGO] = PackageKind.CARGO
    crate: str


class PythonPackage(BaseModel):
    model_config = _STRICT

    type: Literal[PackageKind.PYTHON] = PackageKind.PYTHON
    package: str
    version: str | None = None


class GoPackage(BaseModel):
    model_config = _STRICT

    type: Literal[PackageKind.GO] = PackageKind.GO
    module: str
    version: str | None = None


class GitHubReleasePackage(BaseModel):
    model_config = _STRICT

    type: Literal[PackageKind.GITHUB_RELEASE] = PackageKind.GITHUB_RELEASE
    repo: str
    tag: str
    asset: str
    binary: str
    install: str
    checksum: str | None = None
    rename: str | None = None
    extract: bool = True
    chmod: str = "+x"

    @field_validator("binary", "rename")
    @classmethod
    def _bare_name(cls, v: str | None, info: ValidationInfo) -> str | None:
        if v is None:
            return None
        return _reject_path_in_bare_name(v, info.field_name or "field")


class LocalPackage(BaseModel):
    model_config = _STRICT

    type: Literal[PackageKind.LOCAL] = PackageKind.LOCAL
    path: str
    binary: str
    install: str
    checksum: str | None = None
    rename: str | None = None
    extract: bool = True
    chmod: str = "+x"

    @field_validator("binary", "rename")
    @classmethod
    def _bare_name(cls, v: str | None, info: ValidationInfo) -> str | None:
        if v is None:
            return None
        return _reject_path_in_bare_name(v, info.field_name or "field")


class PluginPackage(BaseModel):
    model_config = _STRICT

    type: Literal[PackageKind.PLUGIN] = PackageKind.PLUGIN
    plugin: str


class ExtensionPackage(BaseModel):
    model_config = _STRICT

    type: Literal[PackageKind.EXTENSION] = PackageKind.EXTENSION
    extension: str


Package = Annotated[
    CargoPackage
    | PythonPackage
    | GoPackage
    | GitHubReleasePackage
    | LocalPackage
    | PluginPackage
    | ExtensionPackage,
    Field(discriminator="type"),
]


class FileComponent(BaseModel):
    model_config = _STRICT

    src: Path
    dst: str
    mode: int | None = None
    template: bool = False
    symlink: str | None = None

    @field_validator("mode", mode="before")
    @classmethod
    def _validate_mode(cls, v: object) -> int | None:
        return _validate_mode_policy(v)

    @field_validator("src", "dst", "symlink", mode="before")
    @classmethod
    def _no_control_chars_in_path(cls, v: object) -> object:
        if v is None:
            return v
        return _reject_control_chars_in_path(v)


class BundleComponent(BaseModel):
    model_config = _STRICT

    id: str
    depends_on: list[str] = []

    package: str | None = None
    cargo: CargoPackage | None = None
    python: PythonPackage | None = None
    go: GoPackage | None = None
    github_release: GitHubReleasePackage | None = None
    local: LocalPackage | None = None
    plugin: str | None = None
    file: FileComponent | None = None

    _INLINE_FIELDS: ClassVar[tuple[str, ...]] = (
        "cargo",
        "python",
        "go",
        "github_release",
        "local",
        "plugin",
        "file",
    )

    @model_validator(mode="after")
    def _exactly_one_source(self) -> Self:
        set_fields = [
            name
            for name in ("package", *self._INLINE_FIELDS)
            if getattr(self, name) is not None
        ]
        if len(set_fields) != 1:
            found = ", ".join(set_fields) if set_fields else "(none)"
            raise ValueError(
                f"bundle component {self.id!r}: set EXACTLY ONE of package/"
                f"cargo/python/go/github_release/local/plugin/file — found: "
                f"{found}"
            )
        return self


class BundleSpec(BaseModel):
    model_config = _STRICT

    components: list[BundleComponent]


class Extensions(BaseModel):
    model_config = _STRICT

    include: list[str] = []
    exclude: list[str] = []
    reconcile: ReconcilePolicy = ReconcilePolicy.ADDITIVE


class PluginReconcile(BaseModel):
    model_config = _STRICT

    policy: ReconcilePolicy = ReconcilePolicy.ADDITIVE


class ExtensionReconcile(BaseModel):
    model_config = _STRICT

    exclude: list[str] = []
    policy: ReconcilePolicy = ReconcilePolicy.ADDITIVE


class ReconcileSpec(BaseModel):
    model_config = _STRICT

    plugins: PluginReconcile = PluginReconcile()
    extensions: ExtensionReconcile = ExtensionReconcile()


class Profile(BaseModel):
    model_config = _STRICT

    extends: str | None = None
    tracked_files: list[str] = []
    bootstrap: list[Path] = []
    mcp_servers: list[str] = []
    packages: list[str] = []
    bundles: list[str] = []
    reconcile: ReconcileSpec = ReconcileSpec()
    section_slots: dict[str, str] = {}
    """Map a host-local user-section NAME → a template name in the
    top-level :attr:`Config.section_templates` registry.

    On install, an empty/missing host-local section named here is seeded
    once from the selected template body; a populated section is left
    untouched (host owns it). Merged across the ``extends:`` chain by
    dict-union (child keys override parent keys for the same slot name).
    """


class ResolvedProfile(BaseModel):
    """A profile with its ``extends:`` chain fully resolved.

    All list fields are flattened (parent entries first, child entries
    appended, duplicates dropped while preserving first occurrence).
    Scalar fields take the deepest explicit value in the chain.
    """

    model_config = _STRICT

    extends: None = None
    tracked_files: list[str] = []
    bootstrap: list[Path] = []
    mcp_servers: list[str] = []
    packages: list[str] = []
    bundles: list[str] = []
    reconcile: ReconcileSpec = ReconcileSpec()
    section_slots: dict[str, str] = {}


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
    section_templates: dict[str, SectionTemplateRef] = {}
    packages: dict[str, Package] = {}
    bundles: dict[str, BundleSpec] = {}
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


def _merge_dict[V](parent: dict[str, V], child: dict[str, V]) -> dict[str, V]:
    """Union two dicts; child values override parent for the same key.

    Parent-first insertion order is preserved for keys only in the parent,
    then child-only keys append in their own order — matching the
    first-occurrence ordering :func:`_merge_list` gives list fields.
    """
    return {**parent, **child}


def _merge_reconcile(parent: ReconcileSpec, child: ReconcileSpec) -> ReconcileSpec:
    """Policy overrides only if child's nested block+``policy`` are both in
    ``model_fields_set``, else inherits; exclude concatenates+dedups."""
    merged_exclude = _merge_list(parent.extensions.exclude, child.extensions.exclude)

    child_plugins_set = (
        "plugins" in child.model_fields_set
        and "policy" in child.plugins.model_fields_set
    )
    plugins_policy = (
        child.plugins.policy if child_plugins_set else parent.plugins.policy
    )

    child_extensions_set = (
        "extensions" in child.model_fields_set
        and "policy" in child.extensions.model_fields_set
    )
    extensions_policy = (
        child.extensions.policy if child_extensions_set else parent.extensions.policy
    )

    return ReconcileSpec(
        plugins=PluginReconcile(policy=plugins_policy),
        extensions=ExtensionReconcile(exclude=merged_exclude, policy=extensions_policy),
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

    - List fields (``tracked_files``, ``bootstrap``, ``mcp_servers``,
      ``packages``, ``bundles``) are concatenated parent-first and
      deduplicated, preserving first occurrence.
    - The ``reconcile`` block (plugin + extension policies and the
      extension exclude list) is merged child-over-parent for scalar
      policies and concatenated for ``reconcile.extensions.exclude``.
    - A cycle in ``extends:`` raises :class:`ConfigError` with every
      profile name in the cycle.
    """
    if name not in config.profiles:
        raise ProfileNotFound(f"profile not found: {name}")
    chain = resolve_chain(config, name)

    resolved = ResolvedProfile()
    for profile in chain:
        resolved = ResolvedProfile(
            tracked_files=_merge_list(resolved.tracked_files, profile.tracked_files),
            bootstrap=_merge_list(resolved.bootstrap, profile.bootstrap),
            mcp_servers=_merge_list(resolved.mcp_servers, profile.mcp_servers),
            packages=_merge_list(resolved.packages, profile.packages),
            bundles=_merge_list(resolved.bundles, profile.bundles),
            section_slots=_merge_dict(resolved.section_slots, profile.section_slots),
            reconcile=_merge_reconcile(resolved.reconcile, profile.reconcile),
        )
    return resolved


_BAD_ID_SUBSTRINGS: tuple[str, ...] = ("/", "..")


def _reject_bad_id(kind: str, value: str) -> None:
    if value.startswith(".") or any(bad in value for bad in _BAD_ID_SUBSTRINGS):
        raise ConfigError(
            f"bundle {kind} id {value!r} is unsafe for a file component: "
            "must not contain '/' or '..' or start with '.' — the synthetic "
            "tracked-file id <bundle>.<component> must survive the base-store "
            "traversal guard."
        )


def _resolve_confined_dst(dst: str, *, template: bool) -> Path:
    # .resolve() (not a string prefix) so it collapses ../ AND symlinked
    # parents, catching a ~/../etc or symlink-escape a startswith() would miss.
    from setforge.paths import template_context

    raw = dst
    if template:
        from jinja2 import Template

        raw = Template(raw).render(**template_context())
    return Path(raw).expanduser().resolve()


def _synthetic_tracked_file(fc: "FileComponent") -> TrackedFile:
    return TrackedFile(
        src=fc.src,
        dst=fc.dst,
        mode=fc.mode,
        template=fc.template,
        symlink=fc.symlink,
    )


def _own_prior_injections(config: Config, resolved: ResolvedProfile) -> set[str]:
    # BYTE-IDENTICAL match only: a repeat expand on the same mutated config
    # (install then compare_profile) is an idempotent re-entry, not a collision.
    return {
        synth_id
        for bundle_id in resolved.bundles
        if (bundle := config.bundles.get(bundle_id)) is not None
        for component in bundle.components
        if component.file is not None
        if (synth_id := f"{bundle_id}.{component.id}")
        and config.tracked_files.get(synth_id)
        == _synthetic_tracked_file(component.file)
    }


def _seed_real_dst_owners(
    config: Config, resolved: ResolvedProfile, own_injections: set[str]
) -> dict[Path, str]:
    # A malformed Jinja dst is skipped (not raised) here: that's
    # _check_jinja_templates' concern in validate; raising here would crash
    # validate early instead of reporting cleanly, and a template that
    # won't render deploys nothing so it can't mask a real collision.
    from jinja2 import TemplateError

    dst_owner: dict[Path, str] = {}
    for name in resolved.tracked_files:
        if name in own_injections:
            continue
        real = config.tracked_files.get(name)
        if real is None:
            continue
        try:
            resolved_dst = _resolve_confined_dst(real.dst, template=real.template)
        except TemplateError:
            continue
        dst_owner.setdefault(resolved_dst, name)
    return dst_owner


def _gate_component_paths(
    synthetic_id: str,
    fc: "FileComponent",
    tracked_root: Path,
    home: Path,
    dst_owner: dict[Path, str],
) -> Path:
    from jinja2 import TemplateError

    try:
        resolved_dst = _resolve_confined_dst(fc.dst, template=fc.template)
    except TemplateError as exc:
        raise ConfigError(
            f"bundle file component {synthetic_id!r} dst {fc.dst!r} is not a "
            f"renderable Jinja2 template: {exc}"
        ) from exc
    if resolved_dst != home and home not in resolved_dst.parents:
        raise ConfigError(
            f"bundle file component {synthetic_id!r} dst {fc.dst!r} resolves to "
            f"{resolved_dst} — outside $HOME ({home}); refusing (dst must stay "
            "under the home directory)."
        )

    prior = dst_owner.get(resolved_dst)
    if prior is not None:
        raise ConfigError(
            f"bundle file component {synthetic_id!r} and {prior!r} both target "
            f"{resolved_dst} — duplicate deploy dst; last-writer would silently "
            "clobber. Give them distinct dst paths."
        )

    resolved_src = (tracked_root / fc.src).resolve()
    if resolved_src != tracked_root and tracked_root not in resolved_src.parents:
        raise ConfigError(
            f"bundle file component {synthetic_id!r} src {fc.src} resolves to "
            f"{resolved_src} — outside {tracked_root}; a src outside tracked/ "
            "bypasses the gitleaks secrets sweep."
        )
    return resolved_dst


def validate_bundle_file_components(
    config: Config, resolved: ResolvedProfile, repo_root: Path
) -> None:
    tracked_root = (repo_root / "tracked").resolve()
    home = Path.home().resolve()

    own_injections = _own_prior_injections(config, resolved)
    dst_owner = _seed_real_dst_owners(config, resolved, own_injections)
    real_ids = set(config.tracked_files) - own_injections
    seen_synthetic: set[str] = set()

    for bundle_id in resolved.bundles:
        bundle = config.bundles.get(bundle_id)
        if bundle is None:
            continue
        for component in bundle.components:
            fc = component.file
            if fc is None:
                continue
            _reject_bad_id("bundle", bundle_id)
            _reject_bad_id("component", component.id)
            synthetic_id = f"{bundle_id}.{component.id}"

            if synthetic_id in own_injections:
                seen_synthetic.add(synthetic_id)
                continue

            colliding = None
            if synthetic_id in real_ids:
                colliding = "an existing tracked-file id"
            elif synthetic_id in seen_synthetic:
                colliding = "another bundle file component's synthetic id"
            if colliding is not None:
                raise ConfigError(
                    f"bundle file component {bundle_id}.{component.id!r} mints "
                    f"synthetic tracked-file id {synthetic_id!r}, which collides "
                    f"with {colliding} {synthetic_id!r} — rename the component "
                    "or the colliding entry."
                )
            seen_synthetic.add(synthetic_id)

            resolved_dst = _gate_component_paths(
                synthetic_id, fc, tracked_root, home, dst_owner
            )
            dst_owner[resolved_dst] = synthetic_id


def expand_bundle_file_components(
    config: Config, resolved: ResolvedProfile, repo_root: Path
) -> None:
    # Gates run first: name-collision must precede the overwrite below, or a
    # real tracked-file body would be silently clobbered before it's checked.
    validate_bundle_file_components(config, resolved, repo_root)
    for bundle_id in resolved.bundles:
        bundle = config.bundles.get(bundle_id)
        if bundle is None:
            continue
        for component in bundle.components:
            fc = component.file
            if fc is None:
                continue
            synthetic_id = f"{bundle_id}.{component.id}"
            config.tracked_files[synthetic_id] = _synthetic_tracked_file(fc)
            if synthetic_id not in resolved.tracked_files:
                resolved.tracked_files.append(synthetic_id)


def resolve_and_expand(config: Config, name: str, repo_root: Path) -> ResolvedProfile:
    resolved = resolve_profile(config, name)
    expand_bundle_file_components(config, resolved, repo_root)
    return resolved


def load_config(path: Path, *, tolerate_unknown: bool = True) -> Config:
    """Parse ``setforge.yaml`` from disk and validate against the schema.

    ``tolerate_unknown`` (default ``True``) is the forward-tolerant runtime
    path: unknown keys are warned about and stripped before validation, so
    a config from a newer same-major engine still loads. ``setforge
    validate`` passes ``False`` so an unknown key surfaces as a strict
    schema error with a "did you mean" suggestion instead.

    Raises :class:`ConfigError` on file-not-found, YAML parse errors, or
    cross-field violations (e.g. a profile plugin package referencing
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
    _validate_package_references(config)
    _validate_section_slot_references(config)
    _warn_on_schema_mismatch(config)
    return config


def _validate_tolerant(data: object) -> Config:
    """Validate ``data`` forward-tolerantly: ignore (warn about) unknown keys
    AND unknown enum values on existing fields.

    Lets Pydantic decide what is genuinely extra — running ``model_validate``
    once and inspecting the error set. This correctly accounts for keys an
    alias or a ``mode="before"`` validator legitimately consumes, which a raw
    key-vs-``model_fields`` diff cannot
    see. Two forward-compat classes are tolerated:

    - ``extra_forbidden`` — a newer-version field or a typo. Strip exactly
      those locations, warn, and retry.
    - ``enum`` — a newer minor added a VALUE to an existing field (e.g. a
      future ``reconcile_policy: layered``). Per COMPATIBILITY.md the schema
      may grow enum members within a major, so an older engine must not crash
      on one. Strip the offending key so the field reverts to its default, warn,
      and retry. If stripping does not clear the error (a *required* enum
      field with no default, or an enum nested in a list entry that this
      reader cannot safely default), refuse cleanly via :class:`ConfigError`
      ("upgrade setforge") rather than leaking a raw Pydantic traceback.

    Any error that is neither class means a real validation failure and
    propagates unchanged.
    """
    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        errors = exc.errors()
        extra_locs = [e["loc"] for e in errors if e.get("type") == "extra_forbidden"]
        enum_locs = [e["loc"] for e in errors if e.get("type") == "enum"]
        if len(extra_locs) + len(enum_locs) != len(errors) or not (
            extra_locs or enum_locs
        ):
            raise  # mixed with real errors (or none tolerable) — a genuine failure
        if extra_locs:
            _warn_unknown_keys([_format_loc(loc) for loc in extra_locs])
        if enum_locs:
            _warn_unknown_enum_values([_format_loc(loc) for loc in enum_locs])
        stripped = _strip_extra_locs(data, [*extra_locs, *enum_locs])
        try:
            return Config.model_validate(stripped)
        except ValidationError as retry_exc:
            # Stripping could not clear every error: a required enum field with
            # no safe default, or an enum nested in a list entry the reader
            # cannot default. Refuse cleanly instead of leaking the traceback.
            raise ConfigError(
                "this setforge does not understand a value in setforge.yaml "
                f"({_format_loc(retry_exc.errors()[0]['loc'])}) — it was likely "
                "added by a newer setforge; upgrade setforge to read this config"
            ) from retry_exc


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


def _warn_unknown_enum_values(fields: list[str]) -> None:
    """Warn (one line per field) for every stripped, unknown enum VALUE.

    A newer minor may add a member to an existing enum field (e.g. a future
    ``disposition: layered``). COMPATIBILITY.md permits that within a major,
    so the field reverts to its default here rather than crashing. Text is
    always written; only the color is TTY-gated, so the warning survives
    CliRunner / Docker e2e / CI capture.
    """
    import sys

    color = sys.stderr.isatty()
    prefix = "\033[33mwarning:\033[0m" if color else "warning:"
    for field_path in fields:
        sys.stderr.write(
            f"{prefix} ignoring unrecognized value for setforge.yaml key "
            f"{field_path!r} (a newer-version enum member); reverting it to "
            f"this setforge's default — upgrade setforge to honor it\n"
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


def refuse_unmigrated_host_local_leak(
    config: Config, *, verb: str, profile: str
) -> None:
    """Refuse a mutating verb on an un-migrated host that still carries
    host-local content in ``local.yaml``.

    The host-local declaration surface (``host_local_sections`` / overlay
    ``spans``) was retired from ``local.yaml``: host-local content now lives in
    the reconcile store, folded in by the schema migration that also bumps the
    major. A host that upgraded the ENGINE past that major but has NOT yet run
    ``setforge migrate`` still carries the retired surface in ``local.yaml``,
    and the runtime load path silently STRIPS it — so a ``sync`` / ``install``
    on that host would bake the un-folded host-local body into the SHARED
    tracked source (a silent host-local leak). ``COMPATIBILITY.md`` documents
    the migrate-before-operate ordering; this gate ENFORCES it for the two
    mutating verbs.

    Refuse cleanly (a traceback-free :class:`~setforge.errors.ConfigError` the
    CLI handler renders as one line + nonzero exit) when BOTH hold:

    - the engine's expected schema MAJOR is newer than the config's declared
      major (an un-migrated OLDER config — the cross-major-NEWER case is
      already refused in :func:`_guard_schema_version`, and a same-or-newer
      major has nothing left to fold), AND
    - ``local.yaml`` still declares the retired host-local surface (i.e. the
      migration's fold reader still projects at least one host-local section).

    An older-major config with NO retired surface proceeds untouched, so the
    deliberate ``migrate --pin`` older-schema workflow stays a nag (see
    :func:`_warn_on_schema_mismatch`), not a refuse. Once ``setforge migrate``
    has folded + stripped the surface the second condition is false and this
    gate is silent.
    """
    # Lazy import to dodge a load-time cycle with setforge.source.
    from setforge import source as source_mod

    expected_major = parse_schema_version(current_expected_schema_version)[0]
    declared_major = parse_schema_version(config.schema_version)[0]
    if declared_major >= expected_major:
        return
    if not source_mod.load_local_host_local_sections(source_mod.LOCAL_CONFIG_PATH):
        return
    raise ConfigError(
        f"{verb} refuses to run: setforge.yaml declares schema_version "
        f"{config.schema_version!r} but this setforge expects "
        f"{current_expected_schema_version!r}, and local.yaml still declares "
        "host-local content (host_local_sections / overlay spans) that has not "
        "been folded into the reconcile store. Proceeding could bake that "
        "host-local content into the shared tracked source. Run "
        f"`setforge migrate --profile={profile}` to fold host-local content "
        "into the store before install/sync."
    )


@dataclass(frozen=True, slots=True)
class HostLocalTrackedFileOverride:
    """One tracked_file's resolved overlay-fields overlay state.

    Carried by the mapping returned from
    :func:`apply_host_local_tracked_file_overrides` so compare's
    provenance-tag renderer can read which of the three fields were
    actually overridden host-locally without re-loading local.yaml.

    ``None`` on a field means "no override; the profile-side value
    wins". The mapping never contains an entry where all three are
    ``None`` (the resolver short-circuits the empty-overlay case so
    callers can treat presence as "at least one override applied").
    """

    mode: int | None
    dst: Path | None
    symlink_target: Path | None


def apply_host_local_tracked_file_overrides(
    config: Config,
    *,
    local_config_path: Path | None = None,
) -> dict[str, HostLocalTrackedFileOverride]:
    """Apply the local.yaml host-local ``mode`` / ``dst`` / ``symlink_target``
    overlay.

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

    Returns a mapping ``{tracked_file_id: HostLocalTrackedFileOverride}``
    of which overrides actually applied — used by compare to render
    ``[host-local mode=...]`` / ``[host-local dst=...]`` /
    ``[host-local symlink → ...]`` provenance tags without re-loading
    local.yaml.

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
    model_validator fires on the merged model). Parse-time invariants on
    the overlay itself (mutual-exclusion, mode bounds, ``$VAR`` in dst)
    surface earlier in
    :func:`setforge.source.load_local_tracked_file_overlays`, not here.
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
    """Verify every profile's plugin refs exist in the top-level
    ``Config.claude_plugins`` registry.

    A profile declares plugins via ``packages`` entries of kind
    :class:`PluginPackage`; each such plugin name must resolve in the
    registry. Collects every offender across every profile into a single
    :class:`ConfigError` message so the user fixes all references in
    one round-trip, not one error per re-run.
    """
    registry = set(config.claude_plugins)
    offenders: list[tuple[str, str]] = []
    for profile_name, profile in config.profiles.items():
        # A PluginPackage's plugin must live in the registry; batched here
        # so the adapter's read-time raise is only a backstop.
        for ref in profile.packages:
            pkg = config.packages.get(ref)
            if isinstance(pkg, PluginPackage) and pkg.plugin not in registry:
                offenders.append((profile_name, pkg.plugin))
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


def _validate_package_references(config: Config) -> None:
    package_registry = set(config.packages)
    bundle_registry = set(config.bundles)
    offenders: list[str] = []
    for profile_name, profile in config.profiles.items():
        for ref in profile.packages:
            if ref.strip() and ref not in package_registry:
                offenders.append(f"{profile_name}.packages[{ref}]")
        for ref in profile.bundles:
            if ref.strip() and ref not in bundle_registry:
                offenders.append(f"{profile_name}.bundles[{ref}]")
    if offenders:
        details = ", ".join(offenders)
        raise ConfigError(
            f"profile packages/bundles reference undeclared name(s): "
            f"{details} (add to top-level packages:/bundles:)"
        )


def _validate_section_slot_references(config: Config) -> None:
    """Verify every ``profile.section_slots`` value names a declared template.

    Each slot maps a host-local section NAME (the key) to a template name
    (the value) that MUST exist in the top-level
    :attr:`Config.section_templates` registry. Collects every offender
    across every profile into a single :class:`ConfigError` so the user
    fixes all references in one round-trip (mirrors
    :func:`_validate_plugin_references`).
    """
    from setforge.cli._validate_errors import suggest_close_match

    registry = sorted(config.section_templates)
    registry_set = set(registry)
    offenders: list[str] = []
    for profile_name, profile in config.profiles.items():
        for slot_name, template_name in profile.section_slots.items():
            if template_name not in registry_set:
                hint = suggest_close_match(template_name, registry)
                did_you_mean = f" (did you mean {hint!r}?)" if hint else ""
                offenders.append(
                    f"{profile_name}.{slot_name}→{template_name}{did_you_mean}"
                )
    if offenders:
        details = ", ".join(offenders)
        raise ConfigError(
            f"profile section_slots reference undeclared template(s): "
            f"{details} (add to top-level section_templates:)"
        )


def _parse_overlay_plugin_pid(pid: str) -> tuple[str, str | None]:
    """Split a local.yaml overlay plugin ref into ``(bare_name, marketplace)``.

    ``add`` entries use the ``name@marketplace`` shape per SPEC 2
    mockup; ``remove`` entries are bare names. Returns ``(name, None)``
    when no ``@`` separator is present so the same parser drives both
    list shapes. Empty / whitespace-only refs raise
    :class:`setforge.overlay_provenance.LocalOverlayError` (the resolver-
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

    Carries the three :class:`setforge.overlay_provenance` resolved lists so
    callers (compare's overlay-block renderer, install's per-line
    provenance) can display ``[from local.yaml]`` / SPEC-2 remove tags
    without re-running the resolvers. Callers that need to suppress the
    footer summary on a no-op overlay walk should use
    :func:`setforge.overlay_provenance.has_local_overlay` on the relevant
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
    :class:`setforge.overlay_provenance.LocalOverlayLoadError` — a sentinel
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

    Each :mod:`setforge.overlay_provenance` resolver raises
    :class:`setforge.overlay_provenance.LocalOverlayError` (a
    :class:`ConfigError`) on collision or unknown-remove — the caller
    surfaces those errors under the validate report-all-then-refuse
    contract.
    """
    from setforge import reconcile_adapter

    resolved_plugins = resolve_plugin_overlay(
        profile_plugins=reconcile_adapter.plugin_bare_names(config, resolved),
        profile_name=profile_name,
        overlay=plugin_overlay,
    )
    resolved_extensions = resolve_extension_overlay(
        profile_extensions=reconcile_adapter.extensions_input(config, resolved).include,
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
    _apply_extension_mutations(config, resolved, extension_overlay)
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

    Walks the resolved profile's plugin bare-names (via
    ``reconcile_adapter.plugin_bare_names``) and looks up each in
    ``config.claude_plugins`` to find its marketplace; the set is
    deduplicated while preserving first-occurrence order. Bare names
    absent from ``cfg.claude_plugins`` are silently skipped — the
    caller's plugin reconcile path raises a clearer error for unknown
    plugin refs.
    """
    from setforge import reconcile_adapter

    seen: set[str] = set()
    out: list[str] = []
    for bare_name in reconcile_adapter.plugin_bare_names(config, resolved):
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


def _mint_package_ref(
    config: Config, resolved: ResolvedProfile, ref: str, body: Package
) -> None:
    """Register ``ref -> body`` in ``config.packages`` and append it to the refs.

    Mints the top-level ``packages`` entry when absent. An existing entry with
    an EQUAL body is reused (idempotent), mirroring the migration's
    idempotent-collision rule (:func:`_mint_package`); an existing entry with a
    DIFFERENT body (e.g. a base ``CargoPackage`` already occupying the name an
    overlay wants for a plugin/extension) is a collision across the flat
    ``packages`` namespace and raises :class:`ConfigError` naming the clash,
    rather than silently reusing the wrong-typed entry (which the adapter's
    type-filtered projection would then drop, hiding the error). Appends ``ref``
    to ``resolved.packages`` only when it is not already present, so a re-add of
    a profile plugin/extension does not duplicate.
    """
    existing = config.packages.get(ref)
    if existing is None:
        config.packages[ref] = body
    elif existing != body:
        raise ConfigError(
            f"local.yaml overlay add {ref!r} collides with an existing "
            f"top-level packages entry of a different type/body "
            f"({existing!r} vs {body!r}). The packages namespace is flat, so "
            f"give the overlay add a distinct name (or remove the clashing "
            f"base entry) and re-run (nothing was changed)."
        )
    if ref not in resolved.packages:
        resolved.packages.append(ref)


def _apply_plugin_mutations(
    config: Config, resolved: ResolvedProfile, overlay: "PluginOverlay"
) -> None:
    """Apply ``plugins.add`` / ``plugins.remove`` to the resolved profile.

    ``add`` entries use ``name@marketplace`` shape: synthesize a
    :class:`ClaudePluginRef` in ``cfg.claude_plugins`` (or replace an
    existing one when the user explicitly re-routed to a new
    marketplace via local.yaml), mint a matching
    :class:`PluginPackage` in ``cfg.packages`` when absent, and append
    the bare name to ``resolved.packages``. ``remove`` entries are bare
    names; drop from ``resolved.packages`` every ref whose top-level
    entry is a :class:`PluginPackage` for a removed name.

    A bare-name ``add`` without ``@`` still mints a :class:`PluginPackage`
    (via :func:`_mint_package_ref`) and appends it to ``resolved.packages``,
    but synthesizes no ``claude_plugins`` marketplace entry — so the
    cross-ref check surfaces the missing registry entry as a single error
    message rather than silently dropping the add.
    """
    removed = set(overlay.remove)
    resolved.packages = [
        ref
        for ref in resolved.packages
        if not (
            isinstance(pkg := config.packages.get(ref), PluginPackage)
            and pkg.plugin in removed
        )
    ]
    for raw in overlay.add:
        bare_name, marketplace = _parse_overlay_plugin_pid(raw)
        if marketplace is not None:
            config.claude_plugins[bare_name] = ClaudePluginRef(marketplace=marketplace)
        _mint_package_ref(config, resolved, bare_name, PluginPackage(plugin=bare_name))


def _apply_extension_mutations(
    config: Config, resolved: ResolvedProfile, overlay: "ExtensionOverlay"
) -> None:
    """Apply ``extensions.add`` / ``extensions.remove`` to resolved.packages.

    ``add`` entries mint an :class:`ExtensionPackage` in ``cfg.packages``
    when absent and append the id to ``resolved.packages`` so downstream
    :func:`setforge.vscode_extensions.reconcile` sees the merged set.
    ``remove`` entries drop every ref whose top-level entry is an
    :class:`ExtensionPackage` for a removed id. Excludes are
    profile-only — local.yaml has no extensions.exclude overlay.
    """
    removed = set(overlay.remove)
    resolved.packages = [
        ref
        for ref in resolved.packages
        if not (
            isinstance(pkg := config.packages.get(ref), ExtensionPackage)
            and pkg.extension in removed
        )
    ]
    for ext_id in overlay.add:
        _mint_package_ref(config, resolved, ext_id, ExtensionPackage(extension=ext_id))


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
    from setforge import reconcile_adapter

    available = sorted(config.marketplaces)
    available_set = set(available)
    offenders: list[tuple[str, str]] = []
    for bare_name in reconcile_adapter.plugin_bare_names(config, resolved):
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
